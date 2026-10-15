# DECISIONS.md

1. **Indexes.**
   - `employees.emp_code` (unique): enforces the business rule that emp_code is unique. All employee lookups and joins go through this field.
   - `attendance_logs.(emp_code, date)` (unique compound): two guarantees in one — uniqueness prevents duplicate attendance records for the same employee-day, and the compound index serves the punch-in duplicate check, the PATCH lookup, and the punch-out "find open record" query.
   - `attendance_logs.(date DESC, emp_code ASC)`: matches the sort order of `GET /attendance` exactly, so the list query is a pure IXSCAN without a sort stage.
   - `attendance_logs.date`: single-field index used by the analytics aggregations that filter only by date range (leaderboard, trend).
   - `employees.(department, joined_on)`: supports the department summary and trend queries that filter employees by department and date-range the `joined_on` field.
   - Considered and rejected: a sparse index on `punch_out: null` to speed up the open-record lookup for punch-out. Not worth the overhead because the `(emp_code, date)` index already narrows to one employee's records and punch-out is a rare concurrent operation.

2. **Punch-in race.**
   Two identical punch-in requests arrive simultaneously. Both look up the employee (OK, read-only). Both try `insert_one` with the same `{emp_code, date}`. MongoDB's unique compound index on `(emp_code, date)` means only one insert can physically succeed; the other gets a `DuplicateKeyError` at the storage engine level before it even writes a document. The winning request gets 201; the losing request catches `DuplicateKeyError` and returns 409. No check-then-insert, no application-level lock needed.

3. **Ties.**
   The leaderboard uses `$rank` inside `$setWindowFields`. If two employees have the same `total_late_minutes` at the cutoff, they share the same rank and the next rank is skipped — standard competition ranking, matching the spec's own example of "1, 2, 2, 4". The `$match {"rank": {"$lte": limit}}` filter includes all employees up to and including that rank value, so the response can contain more rows than `limit`. For example, with limit=2 and employees A=120 min (rank 1), B=90 min (rank 2), C=90 min (rank 2): all three are returned. The secondary sort on `emp_code` ascending gives deterministic ordering within a tied group.

4. **Headcount.**
   The department summary aggregation starts from the `employees` collection (not `attendance_logs`), groups by department, and builds the headcount there — before any join to logs. The subsequent `$lookup` into `attendance_logs` uses `preserveNullAndEmptyArrays: true`, so a department whose employees have zero logs in the month still produces one row with its headcount intact. Employees with `joined_on` after `month_end` are filtered out by the initial `$match` on `joined_on <= month_end` (R9).

5. **One thing to change at 100x the data.**
   The department trend's per-day headcount `$lookup` into `employees` runs once per calendar day in the range (up to 92 lookups), each scanning the employees collection. At 100x scale with thousands of employees and departments this becomes a bottleneck. I would materialise a daily headcount snapshot collection (a background job or change-stream worker) keyed on `{department, date}` so the trend query does a single indexed lookup instead of 92 sub-pipeline lookups. Alternatively, since `joined_on` is immutable, the headcount per department per day can be pre-aggregated at employee-creation time and stored as a running counter.

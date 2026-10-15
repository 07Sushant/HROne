"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Annotated, Optional

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError
from pymongo.collection import ReturnDocument

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "hrone")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

# epoch ms validated by Pydantic: rejects floats (strict), rejects seconds-range values
EpochMs = Annotated[int, Field(ge=100_000_000_000, le=4_102_444_800_000, strict=True)]


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #

class StatusEnum(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatusEnum(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}


# --------------------------------------------------------------------------- #
# Startup: create indexes idempotently
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index("emp_code", unique=True)
    # Unique compound index makes each (employee, day) pair atomic — no check-then-insert race on punch-in
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index("date")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers: time/date
# --------------------------------------------------------------------------- #

def epoch_ms_to_utc(ms: int) -> datetime:
    # Integer division truncates sub-second precision before storage (R1)
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_epoch_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch, with overnight-shift correction."""
    punch_ist = punch_in_utc.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    # Overnight shift: shift_end <= shift_start (e.g. 22:00-06:00)
    if eh * 60 + em <= sh * 60 + sm:
        punch_mins = punch_ist.hour * 60 + punch_ist.minute
        # A punch-in whose IST time is before shift_end belongs to the previous day's shift
        if punch_mins < eh * 60 + em:
            return (punch_ist - timedelta(days=1)).date().isoformat()
    return punch_ist.date().isoformat()


# --------------------------------------------------------------------------- #
# Helpers: business rule calculations
# --------------------------------------------------------------------------- #

def compute_late_minutes(punch_in_utc: datetime, shift_start: str) -> int:
    """R2: floor(seconds_since_shift_start / 60), only when strictly > 600 seconds late."""
    punch_ist = punch_in_utc.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    shift_start_ist = punch_ist.replace(hour=h, minute=m, second=0, microsecond=0)
    elapsed = (punch_ist - shift_start_ist).total_seconds()
    # Grace period: not late when elapsed is 600 seconds (10 min) or less
    if elapsed <= 600:
        return 0
    return int(elapsed / 60)


def compute_overtime_minutes(punch_out_utc: datetime, shift_end: str, shift_start: str, att_date: str) -> int:
    """R3: floor(excess_seconds / 60), only when >= 1800 seconds (30 min) past shift_end."""
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    d = date.fromisoformat(att_date)
    shift_end_ist = datetime(d.year, d.month, d.day, eh, em, 0, tzinfo=IST)
    # Overnight shift: shift_end falls on the next calendar day relative to attendance date
    if eh * 60 + em <= sh * 60 + sm:
        shift_end_ist += timedelta(days=1)
    elapsed = (punch_out_utc - shift_end_ist).total_seconds()
    if elapsed < 1800:
        return 0
    return int(elapsed / 60)


def round_half_up(value: float, decimals: int) -> float:
    """R8: half-up rounding (Python's round() uses banker's rounding — wrong for this spec)."""
    if value is None:
        return None
    q = Decimal("0." + "0" * decimals)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, half-up to 2 decimals."""
    return round_half_up((punch_out_utc - punch_in_utc).total_seconds() / 3600, 2)


def count_working_days(start: date, end: date) -> int:
    """R7: count Mon-Fri days inclusive."""
    count, day = 0, start
    while day <= end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Helpers: serialization
# --------------------------------------------------------------------------- #

def make_json_safe(obj):
    """Recursively convert ObjectId / datetime so MongoDB explain output is JSON-serialisable."""
    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_json_safe(v) for v in obj]
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def serialize_record(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    for field in ("punch_in", "punch_out"):
        if isinstance(doc.get(field), datetime):
            doc[field] = dt_to_epoch_ms(doc[field])
    history = []
    for entry in doc.get("history", []):
        entry = dict(entry)
        if isinstance(entry.get("at"), datetime):
            entry["at"] = dt_to_epoch_ms(entry["at"])
        new_changes = {}
        for key, val in entry.get("changes", {}).items():
            val = dict(val)
            if key in ("punch_in", "punch_out"):
                if isinstance(val.get("from"), datetime):
                    val["from"] = dt_to_epoch_ms(val["from"])
                if isinstance(val.get("to"), datetime):
                    val["to"] = dt_to_epoch_ms(val["to"])
            new_changes[key] = val
        entry["changes"] = new_changes
        history.append(entry)
    doc["history"] = history
    doc.setdefault("half_day", False)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("late_minutes", 0)
    return doc


def serialize_employee(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = dt_to_epoch_ms(doc["created_at"])
    return doc


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #

class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "09:30"
    shift_end: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "18:30"
    joined_on: str

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatusEnum = PresenceStatusEnum.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    status: Optional[StatusEnum] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(503, detail="database unreachable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [serialize_employee(e) for e in items]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    ts = epoch_ms_to_utc(body.punched_at) if body.punched_at else now_utc()
    att_date = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status.value,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # Unique index on (emp_code, date) makes this atomic — no check-then-insert race
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="already punched in for this date")

    return serialize_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    punch_out_dt = epoch_ms_to_utc(body.punched_at) if body.punched_at else now_utc()

    # Find the most recent open record whose punch_in is at or before punch_out (handles overnight)
    target = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not target:
        raise HTTPException(404, detail="no punch-in found for this employee")

    punch_in_dt = target["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, detail="punched_at must be after punch_in")
    if (punch_out_dt - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, detail="punch_out cannot be more than 24 hours after punch_in")

    wh = compute_work_hours(punch_in_dt, punch_out_dt)
    ot = compute_overtime_minutes(punch_out_dt, emp["shift_end"], emp["shift_start"], target["date"])
    hd = wh < 4.50

    # Conditional update on punch_out=None prevents two simultaneous punch-outs both succeeding
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": target["date"], "punch_out": None},
        {"$set": {"punch_out": punch_out_dt, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, detail="already punched out")

    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusEnum] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must be <= date_to")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status.value

    total = db.attendance_logs.count_documents(q)
    items = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [serialize_record(d) for d in items], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(...),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="attendance record not found for this date")

    new_status = body.status.value if body.status else record["status"]

    # ABSENT/LEAVE clears punch times; sending punch times with them is a contradiction
    if new_status in ("ABSENT", "LEAVE") and (body.punch_in or body.punch_out):
        raise HTTPException(422, detail="cannot supply punch times when setting ABSENT or LEAVE status")

    # Presence status requires a punch_in (supplied now or already stored)
    if new_status in PRESENCE_STATUSES and not body.punch_in and not record.get("punch_in"):
        raise HTTPException(422, detail="presence status requires a punch_in")

    update: dict = {}

    if body.status and body.status.value != record["status"]:
        update["status"] = body.status.value

    new_punch_in = record.get("punch_in")
    new_punch_out = record.get("punch_out")

    if body.punch_in:
        pi_dt = epoch_ms_to_utc(body.punch_in)
        # punch_in must still belong to the same attendance date after the change (R1)
        if get_attendance_date(pi_dt, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, detail="new punch_in would belong to a different attendance date")
        update["punch_in"] = pi_dt
        new_punch_in = pi_dt

    if body.punch_out:
        po_dt = epoch_ms_to_utc(body.punch_out)
        update["punch_out"] = po_dt
        new_punch_out = po_dt

    if new_status in ("ABSENT", "LEAVE"):
        update["punch_in"] = None
        update["punch_out"] = None
        update["work_hours"] = None
        update["late_minutes"] = 0
        update["overtime_minutes"] = 0
        update["half_day"] = False
        new_punch_in = None
        new_punch_out = None
    elif new_punch_in:
        pi = new_punch_in
        if pi.tzinfo is None:
            pi = pi.replace(tzinfo=timezone.utc)
        update["late_minutes"] = compute_late_minutes(pi, emp["shift_start"])
        if new_punch_out:
            po = new_punch_out
            if po.tzinfo is None:
                po = po.replace(tzinfo=timezone.utc)
            if po <= pi:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if (po - pi).total_seconds() > 86400:
                raise HTTPException(422, detail="punch_out cannot be more than 24 hours after punch_in")
            wh = compute_work_hours(pi, po)
            update["work_hours"] = wh
            update["overtime_minutes"] = compute_overtime_minutes(po, emp["shift_end"], emp["shift_start"], date)
            update["half_day"] = wh < 4.50

    changes = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        old_val = record.get(field)
        new_val = update.get(field, old_val)
        if old_val != new_val:
            changes[field] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(422, detail="no fields were changed by this request")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic lock on history length: prevents two concurrent PATCHes from silently overwriting each other
    current_history_len = len(record.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]},
        },
        {"$set": update, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, detail="concurrent edit detected — please retry")

    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #

def month_bounds(month: str):
    year, mon = map(int, month.split("-"))
    start = date(year, mon, 1)
    end = date(year, mon, calendar.monthrange(year, mon)[1])
    return start, end


# $dayOfWeek: 1=Sun, 2=Mon, 3=Tue, 4=Wed, 5=Thu, 6=Fri, 7=Sat — weekday is 2-6
WEEKDAY_COND = {"$and": [{"$gte": [{"$dayOfWeek": "$date_obj"}, 2]}, {"$lte": [{"$dayOfWeek": "$date_obj"}, 6]}]}

# Reusable expression: count present days (0.5 for half_day, 1.0 otherwise), weekdays only
PRESENT_DAYS_EXPR = {
    "$cond": [
        {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, WEEKDAY_COND]},
        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
        0,
    ]
}


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    month_start, month_end = month_bounds(month)
    joined = date.fromisoformat(emp["joined_on"])
    working_days = count_working_days(max(joined, month_start), month_end)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}},
        {"$addFields": {"date_obj": {"$dateFromString": {"dateString": "$date"}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_DAYS_EXPR},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    s = result[0] if result else {}

    present_days = s.get("present_days", 0)
    attendance_pct = round_half_up(present_days / working_days * 100, 2) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": s.get("leave_days", 0),
        "late_count": s.get("late_count", 0),
        "total_late_minutes": s.get("total_late_minutes", 0),
        "total_overtime_minutes": s.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month_start, month_end = month_bounds(month)
    ms, me = month_start.isoformat(), month_end.isoformat()

    emp_match: dict = {"joined_on": {"$lte": me}}
    if department:
        emp_match["department"] = department

    pipeline = [
        # R9: only employees who joined on or before month_end
        {"$match": emp_match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "emp_codes": {"$push": "$emp_code"}}},
        # Lookup all logs for those employees in the month; compute helpers inside the pipeline
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"codes": "$emp_codes"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$in": ["$emp_code", "$$codes"]},
                    {"$gte": ["$date", ms]},
                    {"$lte": ["$date", me]},
                ]}}},
                {"$addFields": {
                    "date_obj": {"$dateFromString": {"dateString": "$date"}},
                    "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                }},
                {"$addFields": {
                    "is_weekday": {"$and": [
                        {"$gte": [{"$dayOfWeek": "$date_obj"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date_obj"}, 6]},
                    ]},
                }},
            ],
            "as": "logs",
        }},
        # preserveNullAndEmptyArrays keeps departments with zero logs (they still have a headcount)
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$_id",
            "headcount": {"$first": "$headcount"},
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$eq": [{"$ifNull": ["$logs.is_presence", False]}, True]},
                        {"$eq": [{"$ifNull": ["$logs.is_weekday", False]}, True]},
                    ]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1.0]},
                    0,
                ]
            }},
            # avg_work_hours: mean over presence records that have a work_hours value
            "wh_sum": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$eq": [{"$ifNull": ["$logs.is_presence", False]}, True]},
                        {"$isNumber": "$logs.work_hours"},
                    ]},
                    "$logs.work_hours",
                    0,
                ]
            }},
            "wh_count": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$eq": [{"$ifNull": ["$logs.is_presence", False]}, True]},
                        {"$isNumber": "$logs.work_hours"},
                    ]},
                    1, 0,
                ]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$wh_count", 0]},
                {"$divide": ["$wh_sum", "$wh_count"]},
                None,
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": ASCENDING}},
    ]

    items = list(db.employees.aggregate(pipeline))
    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start, month_end = month_bounds(month)
    ms, me = month_start.isoformat(), month_end.isoformat()

    pipeline = [
        {"$match": {"date": {"$gte": ms, "$lte": me}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        # Logs whose employee no longer exists are dropped (spec: ignored)
        {"$unwind": "$emp"},
        {"$addFields": {"emp_code": "$_id", "name": "$emp.name", "department": "$emp.department"}},
    ]

    # Department filter narrows the ranking pool (spec: "ranking happens inside that department only")
    if department:
        pipeline.append({"$match": {"department": department}})

    pipeline += [
        # $rank: tied employees share a rank and the next rank is skipped (1, 2, 2, 4) — spec's exact example
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING},
            "output": {"rank": {"$rank": {}}},
        }},
        # Return every row whose rank <= limit (ties at the cutoff boundary are all included)
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        {"$project": {"_id": 0, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1, "rank": 1}},
    ]

    return {"month": month, "limit": limit, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(alias="from"),
    to: str = Query(...),
):
    try:
        from_date = date.fromisoformat(from_)
        to_date = date.fromisoformat(to)
    except ValueError:
        raise HTTPException(422, detail="from and to must be YYYY-MM-DD")

    if to_date < from_date:
        raise HTTPException(422, detail="to must not be before from")
    if (to_date - from_date).days > 92:
        raise HTTPException(422, detail="date range exceeds 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, detail="department not found")

    from_str, to_str = from_date.isoformat(), to_date.isoformat()
    # $densify requires datetime bounds; step=1 day fills every calendar day in the range
    from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=timezone.utc)
    to_dt = datetime(to_date.year, to_date.month, to_date.day, tzinfo=timezone.utc)

    pipeline = [
        {"$match": {"date": {"$gte": from_str, "$lte": to_str}}},
        # Join to filter logs to only this department's employees
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$addFields": {"date_obj": {"$dateFromString": {"dateString": "$date"}}}},
        {"$group": {
            "_id": "$date_obj",
            "present_count": {"$sum": {
                "$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                    0,
                ]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt + timedelta(days=1)]}}},
        {"$fill": {"output": {"present_count": {"value": 0}, "late_count": {"value": 0}}}},
        {"$addFields": {
            "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
            "dow": {"$dayOfWeek": "$_id"},
        }},
        {"$addFields": {
            "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
        }},
        # Per-day headcount: employees in the department with joined_on <= that day (R9)
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date_str"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$department", department]},
                    {"$lte": ["$joined_on", "$$d"]},
                ]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        {"$addFields": {
            # attendance_rate: only on working days with headcount > 0; working day with records = 0 still gets 0.0
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]},
        }},
        # 7-day rolling average over attendance_rate; $avg ignores null values (R8 spec)
        {"$setWindowFields": {
            "sortBy": {"_id": ASCENDING},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"_id": ASCENDING}},
        {"$project": {
            "_id": 0,
            "date": "$date_str",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    for row in rows:
        if row["attendance_rate"] is not None:
            row["attendance_rate"] = round_half_up(row["attendance_rate"], 4)
        if row["moving_avg_7d"] is not None:
            row["moving_avg_7d"] = round_half_up(row["moving_avg_7d"], 4)

    return {"department": department, "from": from_str, "to": to_str, "items": rows}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #

def _build_attendance_list_query(
    emp_code, date_from, date_to, status, page, page_size
):
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    valid = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in valid:
        raise HTTPException(422, detail=f"endpoint must be one of {sorted(valid)}")

    collection = "attendance_logs"
    explain_doc: dict = {}

    if endpoint == "attendance_list":
        q = _build_attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
        explain_doc = db.command("explain", {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }, verbosity="executionStats")

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required for employee_monthly")
        month_start, month_end = month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}},
            {"$addFields": {"date_obj": {"$dateFromString": {"dateString": "$date"}}}},
            {"$group": {"_id": None, "present_days": {"$sum": PRESENT_DAYS_EXPR}}},
        ]
        explain_doc = db.command("explain", {
            "aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {},
        }, verbosity="executionStats")

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required for department_summary")
        month_start, month_end = month_bounds(month)
        ms, me = month_start.isoformat(), month_end.isoformat()
        emp_match: dict = {"joined_on": {"$lte": me}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "emp_codes": {"$push": "$emp_code"}}},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [{"$match": {"$expr": {"$and": [
                    {"$in": ["$emp_code", "$$codes"]},
                    {"$gte": ["$date", ms]},
                    {"$lte": ["$date", me]},
                ]}}}],
                "as": "logs",
            }},
        ]
        collection = "employees"
        explain_doc = db.command("explain", {
            "aggregate": "employees", "pipeline": pipeline, "cursor": {},
        }, verbosity="executionStats")

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required for late_leaderboard")
        month_start, month_end = month_bounds(month)
        ms, me = month_start.isoformat(), month_end.isoformat()
        pipeline = [
            {"$match": {"date": {"$gte": ms, "$lte": me}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
        ]
        explain_doc = db.command("explain", {
            "aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {},
        }, verbosity="executionStats")

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, detail="department, from, and to are required for department_trend")
        explain_doc = db.command("explain", {
            "aggregate": "attendance_logs",
            "pipeline": [
                {"$match": {"date": {"$gte": from_, "$lte": to}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
                {"$unwind": "$emp"},
                {"$match": {"emp.department": department}},
            ],
            "cursor": {},
        }, verbosity="executionStats")

    return {"endpoint": endpoint, "collection": collection, "explain": make_json_safe(explain_doc)}

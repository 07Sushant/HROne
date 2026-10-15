# Employee Attendance & Analytics API

FastAPI + MongoDB attendance service for the HROne engineering assignment.

## How to run

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt

# Set environment variables (copy .env.example or create .env):
# MONGO_URI=mongodb+srv://...
# MONGO_DB=hrone

uvicorn app.main:app --port 8000 --reload
```

Requires **MongoDB 6.0+**. Atlas free tier (M0) works. The app creates all required indexes at startup.

Interactive docs: http://localhost:8000/docs

## Notes

- All timestamps in the API are **epoch milliseconds** (integer). ISO strings and floats are rejected with 422.
- Attendance dates are **IST (UTC+05:30)** calendar dates.
- Business rules R1–R10 are implemented in full, including overnight shift date attribution, half-up rounding, and the 30-minute overtime threshold.
- Analytics endpoints use native MongoDB aggregation pipelines only (no Python-level loops over documents).

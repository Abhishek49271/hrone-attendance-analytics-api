# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of contract v2.0. All application code is in `app/main.py`.

## Run locally
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# Set MONGO_URI and MONGO_DB in your environment or local .env
python sample_seed.py
python -m uvicorn app.main:app --port 8000 --reload
```
MongoDB 6.0+ is required. `/health` pings MongoDB and returns 503 if unavailable.

## API
Swagger UI: `http://127.0.0.1:8000/docs`

## Notes
- Instants are returned as epoch milliseconds; MongoDB stores UTC BSON dates.
- Employee and attendance natural keys are `emp_code` and `(emp_code, date)`. Internal MongoDB `_id` values are never returned.
- See `REVIEW.md` for starter defects and `DECISIONS.md` for implementation choices.
- Validate against the provided OpenAPI contract and test concurrent requests against a real MongoDB instance before submission.

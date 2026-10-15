"""Employee Attendance & Analytics API, contract v2.0."""
import os
import re
import calendar
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, StrictInt, field_validator, ConfigDict
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure
from bson import json_util

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def ensure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_late")


def now_ist():
    return datetime.now(IST).replace(microsecond=0)


def as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def from_ms(value):
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)


def epoch_ms(value):
    if value is None:
        return None
    return int(as_utc(value).timestamp() * 1000)


def response_record(doc):
    if not doc:
        return doc
    doc = dict(doc)
    doc.pop("_id", None)
    doc.pop("id", None)
    for field in ("punch_in", "punch_out"):
        doc[field] = epoch_ms(doc.get(field))
    history = []
    for entry in doc.get("history") or []:
        e = dict(entry)
        e["at"] = epoch_ms(e.get("at"))
        changes = {}
        for key, val in (e.get("changes") or {}).items():
            val = dict(val)
            if key in ("punch_in", "punch_out"):
                val["from"] = epoch_ms(val.get("from"))
                val["to"] = epoch_ms(val.get("to"))
            changes[key] = val
        e["changes"] = changes
        history.append(e)
    doc["history"] = history
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    return doc


def response_employee(doc):
    doc = dict(doc)
    doc.pop("_id", None)
    doc["created_at"] = epoch_ms(doc.get("created_at"))
    return doc


def validate_ms(value):
    if value is not None and not MIN_MS <= value <= MAX_MS:
        raise ValueError("timestamp must be epoch milliseconds within the allowed range")
    return value


def validate_date(value):
    try:
        parsed = date.fromisoformat(value)
    except Exception:
        raise ValueError("date must be valid YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise ValueError("date must use YYYY-MM-DD")
    return value


def validate_month(value):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise ValueError("month must be YYYY-MM")
    return value


def rounded(value, places=2):
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in, shift_start):
    local = as_utc(punch_in).astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    start = local.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = (local - start).total_seconds()
    return int(delta // 60) if delta > 600 else 0


def compute_work_hours(punch_in, punch_out):
    seconds = Decimal(str((as_utc(punch_out) - as_utc(punch_in)).total_seconds()))
    return float((seconds / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def shift_end_datetime(attendance_date, shift_start, shift_end):
    day = date.fromisoformat(attendance_date)
    h, m = map(int, shift_end.split(":"))
    end_day = day + timedelta(days=1) if shift_end <= shift_start else day
    return datetime.combine(end_day, datetime.min.time(), tzinfo=IST).replace(hour=h, minute=m)


def compute_overtime(punch_out, shift_start, shift_end, attendance_date):
    end = shift_end_datetime(attendance_date, shift_start, shift_end)
    minutes = int((as_utc(punch_out) - as_utc(end)).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def attendance_date_for(ts, emp):
    local = as_utc(ts).astimezone(IST)
    d = local.date()
    if emp["shift_end"] <= emp["shift_start"]:
        eh, em = map(int, emp["shift_end"].split(":"))
        if (local.hour, local.minute) < (eh, em):
            d -= timedelta(days=1)
    return d.isoformat()


def validate_shift(value):
    if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("time must use HH:MM in 24-hour format")
    return value


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def code(cls, v):
        if not re.fullmatch(r"EMP\d{4,6}", v or ""):
            raise ValueError("emp_code must be EMP followed by 4-6 digits")
        return v

    @field_validator("name")
    @classmethod
    def name_valid(cls, v):
        if not v.strip() or len(v) > 100: raise ValueError("name must be 1-100 characters")
        return v

    @field_validator("email")
    @classmethod
    def email_valid(cls, v):
        if len(v) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v or ""):
            raise ValueError("email must be a valid email address (max 120 characters)")
        return v

    @field_validator("department")
    @classmethod
    def department_valid(cls, v):
        if not v.strip() or len(v) > 50: raise ValueError("department must be 1-50 characters")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def shift_valid(cls, v): return validate_shift(v)

    @field_validator("joined_on")
    @classmethod
    def joined_valid(cls, v): return validate_date(v)

    @field_validator("shift_end")
    @classmethod
    def different_shift(cls, v, info):
        if info.data.get("shift_start") == v:
            raise ValueError("shift_start and shift_end must differ")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def ts_valid(cls, v): return validate_ms(v)


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def ts_valid(cls, v): return validate_ms(v)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out")
    @classmethod
    def ts_valid(cls, v): return validate_ms(v)

    @field_validator("reason")
    @classmethod
    def reason_valid(cls, v):
        if not 5 <= len(v) <= 200: raise ValueError("reason must be 5-200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def by_valid(cls, v):
        if not v.strip() or len(v) > 50: raise ValueError("regularized_by must be 1-50 characters")
        return v


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return response_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = [response_employee(d) for d in db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
    ts = (from_ms(body.punched_at) if body.punched_at is not None else now_ist())
    ts = ts.replace(microsecond=0)
    d = attendance_date_for(ts, emp)
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status,
           "punch_in": as_utc(ts), "punch_out": None, "work_hours": None,
           "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")
    return response_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
    ts = (from_ms(body.punched_at) if body.punched_at is not None else now_ist()).replace(microsecond=0)
    # Query dates and times in UTC because BSON datetimes are UTC and PyMongo reads naive UTC.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": as_utc(ts)}, "punch_out": None},
        sort=[("punch_in", DESCENDING)])
    if record is None:
        already = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": as_utc(ts)}, "punch_out": {"$ne": None}},
            sort=[("punch_in", DESCENDING)])
        if already:
            raise HTTPException(409, detail="Already punched out")
        raise HTTPException(404, detail="No punch-in found")
    pin = as_utc(record["punch_in"])
    if as_utc(ts) <= pin or as_utc(ts) > pin + timedelta(hours=24):
        raise HTTPException(422, detail="Punch-out must be after punch-in and within 24 hours")
    hours = compute_work_hours(pin, ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], record["date"])
    half = hours < 4.50
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": as_utc(ts), "work_hours": hours, "overtime_minutes": overtime, "half_day": half}})
    if result.modified_count != 1:
        raise HTTPException(409, detail="Already punched out")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return response_record(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[str] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from: validate_date(date_from)
    if date_to: validate_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must be <= date_to")
    if status and status not in STATUSES:
        raise HTTPException(422, detail="Invalid status")
    q = {}
    if emp_code: q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status: q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items": [response_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    validate_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, detail="Employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old: raise HTTPException(404, detail="Attendance record not found")
    updates = {}
    if body.status is not None: updates["status"] = body.status
    if "punch_in" in body.model_fields_set: updates["punch_in"] = from_ms(body.punch_in) if body.punch_in is not None else None
    if "punch_out" in body.model_fields_set: updates["punch_out"] = from_ms(body.punch_out) if body.punch_out is not None else None
    status = updates.get("status", old.get("status"))
    if status in ("ABSENT", "LEAVE"):
        if "punch_in" in body.model_fields_set or "punch_out" in body.model_fields_set:
            raise HTTPException(422, detail="ABSENT/LEAVE cannot be supplied with punch times")
        updates["punch_in"] = None
        updates["punch_out"] = None
    else:
        pin = updates.get("punch_in", old.get("punch_in"))
        pout = updates.get("punch_out", old.get("punch_out"))
        if pin is None: raise HTTPException(422, detail="Presence status requires punch_in")
        if attendance_date_for(pin, emp) != date:
            raise HTTPException(422, detail="punch_in must belong to the record attendance date")
        if pout is not None and (as_utc(pout) <= as_utc(pin) or as_utc(pout) > as_utc(pin)+timedelta(hours=24)):
            raise HTTPException(422, detail="punch_out must be after punch_in and within 24 hours")
    final_status = status
    final_pin = updates.get("punch_in", old.get("punch_in"))
    final_pout = updates.get("punch_out", old.get("punch_out"))
    if final_status in ("ABSENT", "LEAVE"):
        derived = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    else:
        derived = {"late_minutes": compute_late_minutes(final_pin, emp["shift_start"]),
                   "work_hours": compute_work_hours(final_pin, final_pout) if final_pout else None,
                   "overtime_minutes": compute_overtime(final_pout, emp["shift_start"], emp["shift_end"], date) if final_pout else 0,
                   "half_day": compute_work_hours(final_pin, final_pout) < 4.5 if final_pout else False}
    final = dict(old)
    final.update(updates)
    final.update(derived)
    changes = {}
    fields = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    for f in fields:
        before = old.get(f, False if f == "half_day" else 0 if f in ("late_minutes", "overtime_minutes") else None)
        after = final.get(f, False if f == "half_day" else 0 if f in ("late_minutes", "overtime_minutes") else None)
        if f in ("punch_in", "punch_out"):
            before, after = as_utc(before) if before else None, as_utc(after) if after else None
        if before != after:
            changes[f] = {"from": before, "to": after}
    if not changes: raise HTTPException(422, detail="Request does not change the record")
    history = list(old.get("history") or [])
    history.append({"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes})
    # Compare-and-swap protects history from concurrent lost updates.
    result = db.attendance_logs.update_one(
        {"_id": old["_id"], "history": old.get("history", [])},
        {"$set": {**updates, **derived, "history": history}})
    if result.modified_count != 1:
        raise HTTPException(409, detail="Concurrent regularization conflict; retry")
    return response_record(db.attendance_logs.find_one({"_id": old["_id"]}))


def month_bounds(month):
    validate_month(month)
    year, mon = map(int, month.split("-"))
    start = f"{year:04d}-{mon:02d}-01"
    end = f"{year:04d}-{mon:02d}-{calendar.monthrange(year, mon)[1]:02d}"
    return start, end, date.fromisoformat(end)


def weekdays_between(start, end):
    if start > end: return 0
    days = (end-start).days+1
    weeks, rem = divmod(days, 7)
    count = weeks*5
    for i in range(rem):
        if (start.weekday()+i)%7 < 5: count += 1
    return count


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end, last_day = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, detail="Employee not found")
    joined = date.fromisoformat(emp["joined_on"])
    work_start = max(joined, date.fromisoformat(start))
    working = weekdays_between(work_start, last_day)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": None,
          "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]}, {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
          "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
          "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
          "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
          "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]
    agg = list(db.attendance_logs.aggregate(pipeline))
    x = agg[0] if agg else {}
    pct = rounded(x.get("present_days", 0) / working * 100, 4) if working else None
    return {"emp_code": emp_code, "month": month, "working_days": working,
            "present_days": rounded(x.get("present_days", 0), 2), "leave_days": x.get("leave_days", 0),
            "late_count": x.get("late_count", 0), "total_late_minutes": x.get("total_late_minutes", 0),
            "total_overtime_minutes": x.get("total_overtime_minutes", 0), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end, last_day = month_bounds(month)
    match = {"$match": {"joined_on": {"$lte": end}}}
    if department is not None: match["$match"]["department"] = department
    pipeline = [match,
      {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
        {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}]}}},
        {"$group": {"_id": None,
          "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]}, {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
          "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
          "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]}, 1, 0]}},
          "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
          "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
          "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
          "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}}}
      ], "as": "stats"}},
      {"$project": {"_id": 0, "department": 1, "stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {}]}}},
      {"$group": {"_id": "$department", "headcount": {"$sum": 1},
        "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0]}},
        "work_hours_sum": {"$sum": {"$ifNull": ["$stats.work_hours_sum", 0]}},
        "work_hours_count": {"$sum": {"$ifNull": ["$stats.work_hours_count", 0]}},
        "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}},
        "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}},
        "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}}}},
      {"$sort": {"_id": 1}},
      {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": {"$round": ["$present_days", 2]},
        "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, {"$round": [{"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2]}, None]},
        "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}]
    items = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    for item in items:
        item["present_days"] = rounded(item["present_days"], 2)
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = rounded(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end, _ = month_bounds(month)
    pipeline = [
      {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
      {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
      {"$match": {"total_late_minutes": {"$gt": 0}}},
      {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
      {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
      {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
      {"$match": {"rank": {"$lte": limit}}},
      {"$sort": {"total_late_minutes": -1, "_id": 1}},
      {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}}
    ]
    items = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    validate_date(from_); validate_date(to)
    start, end = date.fromisoformat(from_), date.fromisoformat(to)
    if end < start: raise HTTPException(422, detail="to must be on or after from")
    if (end-start).days+1 > 92: raise HTTPException(422, detail="date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}): raise HTTPException(404, detail="Unknown department")
    # Daily attendance aggregation, then densify calendar gaps and compute moving average in MongoDB.
    pipeline = [
      {"$match": {"date": {"$gte": from_, "$lte": to}}},
      {"$unionWith": {"coll": "employees", "pipeline": [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "emp_code": 1, "date": {"$literal": from_}, "status": {"$literal": "ABSENT"}, "half_day": {"$literal": False}, "late_minutes": {"$literal": 0}}}
      ]}},
      {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
      {"$unwind": "$emp"},
      {"$match": {"emp.department": department}},
      {"$group": {"_id": "$date",
        "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
      {"$project": {"_id": 0, "date": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
      {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(start, datetime.min.time()), datetime.combine(end + timedelta(days=1), datetime.min.time())]}}},
      {"$set": {"date": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}}, "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
      {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
        {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
        {"$count": "n"}], "as": "hc"}},
      {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, "is_working_day": {"$and": [{"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]}, {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]}]}}},
      {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None]}}},
      {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
      {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": {"$round": ["$present_count", 2]}, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}}},
      {"$sort": {"date": 1}}
    ]
    try:
        items = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    except OperationFailure as exc:
        raise HTTPException(500, detail=f"Trend aggregation failed: {exc.details.get('errmsg', str(exc))}")
    # Mongo $densify bounds use UTC date values; the UTC calendar is intended for date-only strings.
    return {"department": department, "items": items}


def _explain_find(query, sort, skip, limit):
    return db.attendance_logs.find(query).sort(sort).skip(skip).limit(limit).explain("executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[str] = None, page: int = Query(1, ge=1),
                     page_size: int = Query(20, ge=1, le=100),
                     from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed: raise HTTPException(422, detail="Unsupported endpoint")
    if endpoint == "attendance_list":
        if date_from: validate_date(date_from)
        if date_to: validate_date(date_to)
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if status: q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        exp = _explain_find(q, [("date", -1), ("emp_code", 1)], (page-1)*page_size, page_size)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, detail="emp_code and month are required")
        start, end, _ = month_bounds(month)
        exp = db.attendance_logs.find({"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint == "department_summary":
        if not month: raise HTTPException(422, detail="month is required")
        month_bounds(month)
        q = {"joined_on": {"$lte": month_bounds(month)[1]}}
        if department: q["department"] = department
        exp = db.employees.find(q).explain("executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}
    if endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, detail="month is required")
        start, end, _ = month_bounds(month)
        exp = db.attendance_logs.find({"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint == "department_trend":
        if not department or not from_ or not to: raise HTTPException(422, detail="department, from and to are required")
        validate_date(from_); validate_date(to)
        exp = db.attendance_logs.find({"date": {"$gte": from_, "$lte": to}}).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}



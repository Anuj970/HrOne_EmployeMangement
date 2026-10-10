"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

All endpoints of the contract: health, employees, attendance (punch-in/out, PATCH, list),
analytics (monthly, department summary, late leaderboard, department trend) and /admin/explain.
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# override=False (the default) means real environment variables beat the .env file.
load_dotenv()

log = logging.getLogger("attendance")

mongo_uri = os.getenv("MONGO_URI")
mongo_db = os.getenv("MONGO_DB")
if not mongo_uri or not mongo_db:
    raise RuntimeError("MONGO_URI and MONGO_DB must be configured")

# tz_aware=True: PyMongo returns datetimes WITH tzinfo=UTC instead of naive ones.
# serverSelectionTimeoutMS: fail fast (3 s) instead of hanging ~30 s when MongoDB is unreachable.
client = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[mongo_db]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# Epoch MILLISECONDS only. StrictInt rejects floats and strings; the range rejects "seconds-looking" values.
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]

# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent: same name + same spec = no-op)
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    e, a = db.employees, db.attendance_logs
    # employees
    e.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    e.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_dept_emp_code")
    e.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_dept_joined_on")
    e.create_index([("joined_on", ASCENDING)], name="ix_joined_on")  # department summary without a department filter
    # attendance_logs
    a.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_code_date")
    a.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_date_desc_emp_code")
    a.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_status_date_emp_code"
    )
    a.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_emp_code_punch_in")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # MongoDB down at boot: still start; /health retries below.
        log.warning("Index creation failed at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """BSON datetime (UTC) -> epoch milliseconds. Exact integer maths, no float."""
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat a naive value as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_utc_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def round_half_up(value, places: int = 2) -> Decimal:
    """Exact decimal half-up rounding (R4, R8). Pass Decimal/int/str; avoid float inputs."""
    q = Decimal(1).scaleb(-places)
    return Decimal(value).quantize(q, rounding=ROUND_HALF_UP)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = r"^([01]\d|2[0-3]):[0-5]\d$"


def parse_date(s: str) -> date:
    """Strict YYYY-MM-DD -> date. Raises ValueError for anything else (e.g. 2026-02-30, 2026-1-5)."""
    if not _DATE_RE.match(s):
        raise ValueError("date must be YYYY-MM-DD")
    return date.fromisoformat(s)


def parse_date_or_422(s: str, name: str = "date") -> date:
    try:
        return parse_date(s)
    except ValueError:
        raise HTTPException(422, f"{name} must be a valid YYYY-MM-DD date")


# ---- shift / attendance-date / derived-field rules (R1 - R5) ---------------- #
def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM strings compare correctly


def attendance_date_for(punch_in: datetime, emp: dict) -> str:
    """R1: IST calendar date of the punch-in; for an overnight shift a punch-in earlier than
    shift_end belongs to the shift that started the previous day."""
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if is_overnight(emp["shift_start"], emp["shift_end"]) and ist.time() < _hm(emp["shift_end"]):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_start_dt(date_str: str, emp: dict) -> datetime:
    return datetime.combine(date.fromisoformat(date_str), _hm(emp["shift_start"]), tzinfo=IST)


def shift_end_dt(date_str: str, emp: dict) -> datetime:
    d = date.fromisoformat(date_str)
    if is_overnight(emp["shift_start"], emp["shift_end"]):
        d += timedelta(days=1)  # overnight shifts end on the next calendar day
    return datetime.combine(d, _hm(emp["shift_end"]), tzinfo=IST)


def compute_derived(emp: dict, date_str: str, status: str, punch_in, punch_out) -> dict:
    """The four stored derived fields (R2 - R5). Instants must already be truncated to seconds."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    # R2: late only if STRICTLY more than 10:00 after shift start; then floor minutes since shift start.
    delta = int((punch_in - shift_start_dt(date_str, emp)).total_seconds())
    late = delta // 60 if delta > 600 else 0
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    # R4: exact decimal arithmetic, half-up
    secs = int((punch_out - punch_in).total_seconds())
    wh = round_half_up(Decimal(secs) / Decimal(3600), 2)
    # R3: floor minutes after shift end, counted only if >= 30
    ot = int((punch_out - shift_end_dt(date_str, emp)).total_seconds()) // 60
    return {
        "work_hours": float(wh),
        "late_minutes": late,
        "overtime_minutes": ot if ot >= 30 else 0,
        "half_day": wh < Decimal("4.50"),  # R5 uses the ROUNDED value
    }


def _history_value(v):
    return to_ms(v) if isinstance(v, datetime) else v


def history_out(h: dict) -> dict:
    changes = {k: {"from": _history_value(v.get("from")), "to": _history_value(v.get("to"))}
               for k, v in (h.get("changes") or {}).items()}
    return {"at": to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Mongo attendance document -> API shape (no _id, epoch ms, defaults for legacy records)."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=_TIME_RE)
    shift_end: str = Field(default="18:30", pattern=_TIME_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_joined_on(cls, v: str) -> str:
        parse_date(v)
        return v

    @model_validator(mode="after")
    def _shift_times_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_null(self):
        # "omitted" means unchanged; an explicit null is not a valid value.
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} must not be null")
        return self


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# /health and /employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:  # MongoDB was down at boot; build the indexes now
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_seconds()
    try:
        # The unique index on emp_code decides the winner atomically; no check-then-insert.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: punch-in / punch-out / regularize / list
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    d = attendance_date_for(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **compute_derived(emp, d, body.status, ts, None),
        "history": [],
    }
    try:
        # The unique (emp_code, date) index makes this atomic: exactly one concurrent insert wins.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    out = from_ms(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    # Most recent record whose punch_in <= punched_at (works across midnight; ABSENT/LEAVE have null punch_in).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if out <= pin or out - pin > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")
    derived = compute_derived(emp, rec["date"], rec["status"], pin, out)
    # Atomic guard: only update if punch_out is STILL null. Of two simultaneous requests only one matches.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_in": pin, "punch_out": None},
        {"$set": {"punch_out": out, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date_or_422(date)
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old_status, old_in, old_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status
    if new_status in PRESENCE:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else old_in
        new_out = from_ms(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if attendance_date_for(new_in, emp) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    else:  # ABSENT / LEAVE clear both punch times
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch times cannot be supplied with ABSENT or LEAVE")
        new_in = new_out = None

    if (new_status, new_in, new_out) == (old_status, old_in, old_out):
        raise HTTPException(422, "request changes nothing")

    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                **compute_derived(emp, date, new_status, new_in, new_out)}
    old_vals = {"status": old_status, "punch_in": old_in, "punch_out": old_out,
                "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
                "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False))}
    changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if old_vals[k] != v}
    entry = {"at": now_utc_seconds(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the update only applies if the record is still exactly as we read it
    # (same history length, same status and punch times). The loser of a race gets 409 instead of
    # silently overwriting the winner's history entry.
    guard = {"_id": rec["_id"], "status": old_status, "punch_in": old_in, "punch_out": old_out}
    if "history" in rec:
        guard["history"] = {"$size": len(rec["history"])}
    else:
        guard["history"] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": new_vals, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return record_out(updated)


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None) -> dict:
    """The filter used by GET /attendance (also reused by the explain endpoint later)."""
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    df = parse_date_or_422(date_from, "date_from") if date_from else None
    dt = parse_date_or_422(date_to, "date_to") if date_to else None
    if df and dt and df > dt:
        raise HTTPException(422, "date_from must not be after date_to")
    if df or dt:
        q["date"] = {}
        if df:
            q["date"]["$gte"] = df.isoformat()
        if dt:
            q["date"]["$lte"] = dt.isoformat()
    if status:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
def validate_month(month: str) -> tuple[str, str]:
    """Validate YYYY-MM and return the month's first and last dates as YYYY-MM-DD strings."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be in YYYY-MM format")
    year, month_num = map(int, month.split("-"))
    first_day = date(year, month_num, 1)
    next_month = date(year + 1, 1, 1) if month_num == 12 else date(year, month_num + 1, 1)
    return first_day.isoformat(), (next_month - timedelta(days=1)).isoformat()


def count_weekdays(start: date, end: date) -> int:
    """Count Monday-Friday dates inclusively (R7, no holiday calendar)."""
    if start > end:
        return 0
    full_weeks, remainder = divmod((end - start).days + 1, 7)
    total = full_weeks * 5
    for offset in range(remainder):
        if (start.weekday() + offset) % 7 < 5:
            total += 1
    return total


def num_half_up(value, places: int):
    """Mongo number -> JSON number, exact decimal half-up (R8). None stays None.
    str() of a float gives its shortest repr (e.g. '0.12345'), which avoids binary-float noise."""
    if value is None:
        return None
    return float(round_half_up(Decimal(str(value)), places))


def _is_weekday(date_field: str) -> dict:
    """Mon-Fri test on a 'YYYY-MM-DD' string field. $dayOfWeek: 1 = Sunday ... 7 = Saturday."""
    dow = {"$dayOfWeek": {"$dateFromString": {"dateString": date_field}}}
    return {"$and": [{"$gte": [dow, 2]}, {"$lte": [dow, 6]}]}


def _presence_value(prefix: str = "$") -> dict:
    """1 for a full present day, 0.5 for a half day (R5). Missing half_day means false."""
    return {"$cond": [{"$eq": [f"{prefix}half_day", True]}, 0.5, 1]}


# --------------------------------------------------------------------------- #
# Pipeline builders: used by BOTH the analytics endpoints and /admin/explain,
# so the explained plan is exactly the plan the endpoint runs.
# --------------------------------------------------------------------------- #
def employee_monthly_pipeline(emp_code: str, first_day: str, last_day: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", list(PRESENCE)]}, _is_weekday("$date")]},
                _presence_value(), 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]


def department_summary_pipeline(first_day: str, last_day: str, department: Optional[str]) -> list:
    emp_match: dict = {"joined_on": {"$lte": last_day}}  # R9: joined on/before month end, logs or not
    if department:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", first_day]},
                    {"$lte": ["$date", last_day]},
                ]}}},
                {"$project": {"_id": 0, "status": 1, "date": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        # preserveNullAndEmptyArrays keeps employees with zero logs (R9)
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, _is_weekday("$logs.date")]},
                _presence_value("$logs."), 0]}},
            # average over RECORDS: sum / count of present-status records that have work_hours
            "wh_sum": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$isNumber": "$logs.work_hours"}]},
                "$logs.work_hours", 0]}},
            "wh_cnt": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$isNumber": "$logs.work_hours"}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, "$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$codes"},
            "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$wh_cnt", 0]}, {"$divide": ["$wh_sum", "$wh_cnt"]}, None]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(first_day: str, last_day: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        # inner-join semantics: logs whose employee does not exist are dropped by $unwind
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:  # rank inside the department only, so filter BEFORE ranking
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties at the cutoff stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def department_trend_pipeline(department: str, start_str: str, end_str: str) -> list:
    start = {"$dateFromString": {"dateString": start_str}}
    end = {"$dateFromString": {"dateString": end_str}}
    return [
        # ALL employees of the department (not only those joined before `to`), so a department whose
        # staff all join later still produces one row per day with headcount 0.
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"},
                    "employees": {"$push": {"joined_on": "$joined_on"}}}},
        # Gap-filling inside MongoDB: generate exactly one 'YYYY-MM-DD' per day from..to inclusive.
        {"$project": {"_id": 0, "codes": 1, "employees": 1, "day": {"$map": {
            "input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": start, "endDate": end, "unit": "day"}}, 1]}]},
            "as": "i",
            "in": {"$dateToString": {"format": "%Y-%m-%d",
                                     "date": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$i"}}}},
        }}}},
        {"$unwind": "$day"},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"d": "$day", "codes": "$codes"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$d"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
                {"$project": {"_id": 0, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$set": {
            "date": "$day",
            "headcount": {"$size": {"$filter": {"input": "$employees", "as": "e",
                                                "cond": {"$lte": ["$$e.joined_on", "$day"]}}}},
            "present_count": {"$sum": {"$map": {
                "input": {"$filter": {"input": "$logs", "as": "l", "cond": {"$in": ["$$l.status", list(PRESENCE)]}}},
                "as": "l", "in": _presence_value("$$l.")}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l",
                                                 "cond": {"$gt": ["$$l.late_minutes", 0]}}}},
            "is_working_day": _is_weekday("$day"),
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        # current row + up to 6 previous rows INSIDE the range; $avg ignores null rates, and is null if all are
        {"$setWindowFields": {"sortBy": {"date": 1},
                              "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"date": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


def validate_trend_range(from_date: str, to_date: str) -> tuple[str, str]:
    start = parse_date_or_422(from_date, "from")
    end = parse_date_or_422(to_date, "to")
    if end < start:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range must not exceed 92 days")
    return start.isoformat(), end.isoformat()


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    first_day, last_day = validate_month(month)
    emp = get_employee_or_404(emp_code)
    # R7: working days are counted from joined_on when the employee joined mid-month
    working_start = max(date.fromisoformat(first_day), parse_date(emp["joined_on"]))
    working_days = count_weekdays(working_start, date.fromisoformat(last_day))

    res = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first_day, last_day)))
    st = res[0] if res else {}
    present_days = st.get("present_days", 0)
    pct = None
    if working_days > 0:
        pct = num_half_up(Decimal(str(present_days)) * 100 / Decimal(working_days), 4)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": st.get("leave_days", 0),
        "late_count": st.get("late_count", 0),
        "total_late_minutes": st.get("total_late_minutes", 0),
        "total_overtime_minutes": st.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first_day, last_day = validate_month(month)
    items = list(db.employees.aggregate(department_summary_pipeline(first_day, last_day, department)))
    for it in items:
        it["avg_work_hours"] = num_half_up(it["avg_work_hours"], 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first_day, last_day = validate_month(month)
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(first_day, last_day, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"),
                     to_date: str = Query(..., alias="to")):
    start_str, end_str = validate_trend_range(from_date, to_date)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    items = list(db.employees.aggregate(department_trend_pipeline(department, start_str, end_str)))
    for it in items:  # R8: rates are rounded half-up to 4 decimals here, not with Mongo's half-even $round
        it["attendance_rate"] = num_half_up(it["attendance_rate"], 4)
        it["moving_avg_7d"] = num_half_up(it["moving_avg_7d"], 4)
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                      "department_trend"] = Path(..., description="Which endpoint's query to explain."),
    emp_code: Optional[str] = Query(None, description="attendance_list (optional filter), employee_monthly (required)."),
    month: Optional[str] = Query(None, description="YYYY-MM. Required for employee_monthly, department_summary, late_leaderboard."),
    department: Optional[str] = Query(None, description="department_trend (required); department_summary and late_leaderboard (optional)."),
    limit: int = Query(10, ge=1, le=50, description="late_leaderboard only."),
    date_from: Optional[str] = Query(None, description="attendance_list only (YYYY-MM-DD). NOT for department_trend, which uses 'from'."),
    date_to: Optional[str] = Query(None, description="attendance_list only (YYYY-MM-DD). NOT for department_trend, which uses 'to'."),
    status: Optional[Status] = Query(None, description="attendance_list only."),
    from_date: Optional[str] = Query(None, alias="from", description="department_trend only (YYYY-MM-DD). Required there."),
    to_date: Optional[str] = Query(None, alias="to", description="department_trend only (YYYY-MM-DD). Required there."),
    page: int = Query(1, ge=1, description="attendance_list only."),
    page_size: int = Query(20, ge=1, le=100, description="attendance_list only."),
):
    def agg(collection: str, pipeline: list) -> dict:
        return {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {"find": collection, "filter": attendance_query(emp_code, date_from, date_to, status),
                   "sort": dict(ATTENDANCE_SORT), "skip": (page - 1) * page_size, "limit": page_size}
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        first_day, last_day = validate_month(month)
        collection = "attendance_logs"
        command = agg(collection, employee_monthly_pipeline(emp_code, first_day, last_day))
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        first_day, last_day = validate_month(month)
        collection = "employees"
        command = agg(collection, department_summary_pipeline(first_day, last_day, department))
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        first_day, last_day = validate_month(month)
        collection = "attendance_logs"
        command = agg(collection, late_leaderboard_pipeline(first_day, last_day, limit, department))
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department_trend needs department, from and to (not date_from/date_to)")
        start_str, end_str = validate_trend_range(from_date, to_date)
        collection = "employees"
        command = agg(collection, department_trend_pipeline(department, start_str, end_str))
    else:
        raise HTTPException(422, "unknown endpoint identifier")

    explanation = db.command("explain", command, verbosity="executionStats")
    # json_util turns BSON-only types (Timestamp, ObjectId, ...) into JSON-safe values
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(explanation))}
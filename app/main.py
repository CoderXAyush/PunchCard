"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import re
import calendar
from datetime import datetime, timedelta, timezone, date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Annotated, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path as FPath
from pydantic import BaseModel, Field, BeforeValidator, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

# real env vars win over .env
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB", "attendance_db")

client: Optional[MongoClient] = None

def get_client() -> MongoClient:
    global client
    if client is None:
        client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
    return client

def get_db():
    return get_client()[MONGO_DB_NAME]

class _DBProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_db(), name)
    def __getitem__(self, name: str) -> Any:
        return get_db()[name]

db: Any = _DBProxy()

IST = timezone(timedelta(hours=5, minutes=30))

APP_VERSION = "2.0.0"

# ---------------------------------------------------------------------------
# Constants & regex
# ---------------------------------------------------------------------------
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000  # 2100-01-01

EMP_CODE_RE = re.compile(r"^EMP\d{4,6}$")
SHIFT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}

# ---------------------------------------------------------------------------
# Epoch millis strict validation
# ---------------------------------------------------------------------------
def _validate_epoch_millis(v: Any) -> int:
    if v is None:
        raise ValueError("epoch millis cannot be null")
    if isinstance(v, bool):
        raise ValueError("bool is not valid epoch millis")
    if not isinstance(v, int):
        raise ValueError("epoch millis must be an integer")
    if v < EPOCH_MIN or v > EPOCH_MAX:
        raise ValueError(f"epoch millis out of range [{EPOCH_MIN}, {EPOCH_MAX}]")
    return v

def _validate_epoch_millis_opt(v: Any) -> Optional[int]:
    if v is None:
        return None
    return _validate_epoch_millis(v)

def _validate_epoch_millis_nullable(v: Any) -> Optional[int]:
    if v is None:
        return None
    return _validate_epoch_millis(v)

EpochMillis = Annotated[int, BeforeValidator(_validate_epoch_millis)]
EpochMillisNullable = Annotated[Optional[int], BeforeValidator(_validate_epoch_millis_nullable)]
EpochMillisOpt = Annotated[Optional[int], BeforeValidator(_validate_epoch_millis_opt)]

# ---------------------------------------------------------------------------
# Helpers: time & truncation
# ---------------------------------------------------------------------------
def trunc_ms(ms: int) -> int:
    """Floor to whole seconds."""
    return (ms // 1000) * 1000

def ms_to_dt(ms: int) -> datetime:
    """Truncated UTC datetime from epoch millis."""
    ms = trunc_ms(int(ms))
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return int(dt.timestamp() * 1000)

def now_ms() -> int:
    return dt_to_ms(datetime.now(timezone.utc))

def now_dt_trunc() -> datetime:
    return ms_to_dt(now_ms())

def parse_shift(s: str) -> tuple[int, int]:
    h, m = map(int, s.split(":"))
    return h, m

def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start

def attendance_date_for_punch(ist: datetime, shift_start: str, shift_end: str) -> str:
    """R1: date is IST calendar date of shift start. Overnight exception."""
    if is_overnight(shift_start, shift_end):
        eh, em = parse_shift(shift_end)
        # if IST time is earlier than shift_end, belongs to previous day
        if (ist.hour, ist.minute, ist.second) < (eh, em, 0):
            return (ist.date() - timedelta(days=1)).isoformat()
    return ist.date().isoformat()

def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    y, mo, d = map(int, date_str.split("-"))
    sh, sm = parse_shift(shift_start)
    eh, em = parse_shift(shift_end)
    start_ist = datetime(y, mo, d, sh, sm, 0, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        # end is next calendar day
        end_ist = datetime(y, mo, d, eh, em, 0, tzinfo=IST) + timedelta(days=1)
    else:
        end_ist = datetime(y, mo, d, eh, em, 0, tzinfo=IST)
    return start_ist, end_ist

def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    """R2 grace 10 min, floor minutes from shift_start (non-overnight fallback)."""
    punch_ist = punch_in.astimezone(IST)
    h, m = parse_shift(shift_start)
    start_ist = punch_ist.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = (punch_ist - start_ist).total_seconds()
    if delta <= 600:
        return 0
    return int(delta // 60)

def compute_late_for_record(punch_in: datetime, shift_start: str, shift_end: str, att_date: str) -> int:
    start_ist, _ = shift_bounds(att_date, shift_start, shift_end)
    punch_ist = punch_in.astimezone(IST)
    delta = (punch_ist - start_ist).total_seconds()
    if delta <= 600:
        return 0
    return int(delta // 60)

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4 half-up 2 decimals."""
    secs = (punch_out - punch_in).total_seconds()
    h = Decimal(str(secs)) / Decimal("3600")
    r = h.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(r)

def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    """R3 >=30 else 0."""
    _, end_ist = shift_bounds(date_str, shift_start, shift_end)
    punch_ist = punch_out.astimezone(IST)
    mins = int((punch_ist - end_ist).total_seconds() // 60)
    return mins if mins >= 30 else 0

def half_up_2(x: float) -> float:
    # str(x) avoids binary-float rounding errors (e.g. 1.005 -> "1.005" not 1.004999...)
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def half_up_4(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

def working_days_in_month(joined_on: str, month: str) -> int:
    """R7 Mon-Fri from max(joined_on, month_start) to month_end."""
    y, m = map(int, month.split("-"))
    month_start = date_type(y, m, 1)
    last = calendar.monthrange(y, m)[1]
    month_end = date_type(y, m, last)
    j = date_type.fromisoformat(joined_on)
    start = max(j, month_start)
    if start > month_end:
        return 0
    cnt = 0
    cur = start
    while cur <= month_end:
        if cur.weekday() < 5:
            cnt += 1
        cur += timedelta(days=1)
    return cnt

def _weekday_from_datestr(s: str) -> int:
    return date_type.fromisoformat(s).weekday()  # 0 Mon
def _is_working_day(s: str) -> bool:
    return _weekday_from_datestr(s) < 5

# ---------------------------------------------------------------------------
# Serialization helpers (fill missing seeded fields)
# ---------------------------------------------------------------------------
def serialize_employee(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    ca = out.get("created_at")
    if isinstance(ca, datetime):
        out["created_at"] = dt_to_ms(ca)
    elif isinstance(ca, int):
        out["created_at"] = trunc_ms(ca)
    return out

def serialize_attendance(doc: dict) -> dict:
    out: dict[str, Any] = {}
    out["emp_code"] = doc["emp_code"]
    out["date"] = doc["date"]
    out["status"] = doc["status"]
    for k in ("punch_in", "punch_out"):
        v = doc.get(k)
        if v is None:
            out[k] = None
        elif isinstance(v, datetime):
            out[k] = dt_to_ms(v)
        elif isinstance(v, int):
            out[k] = trunc_ms(v)
        else:
            out[k] = None
    wh = doc.get("work_hours")
    out["work_hours"] = None if wh is None else float(wh)
    out["late_minutes"] = int(doc.get("late_minutes", 0) or 0)
    out["overtime_minutes"] = int(doc.get("overtime_minutes", 0) or 0)
    hv = doc.get("half_day")
    out["half_day"] = bool(hv) if hv is not None else False
    hist = doc.get("history") or []
    out_hist = []
    for e in hist:
        at = e.get("at")
        if isinstance(at, datetime):
            at_ms = dt_to_ms(at)
        elif isinstance(at, int):
            at_ms = trunc_ms(at)
        else:
            at_ms = at
        changes = {}
        for ck, cv in (e.get("changes") or {}).items():
            frm = cv.get("from")
            to = cv.get("to")
            if ck in ("punch_in", "punch_out", "at"):
                if isinstance(frm, datetime):
                    frm = dt_to_ms(frm)
                elif isinstance(frm, int) and frm is not None:
                    frm = trunc_ms(frm)
                if isinstance(to, datetime):
                    to = dt_to_ms(to)
                elif isinstance(to, int) and to is not None:
                    to = trunc_ms(to)
            changes[ck] = {"from": frm, "to": to}
        out_hist.append({"at": at_ms, "by": e.get("by"), "reason": e.get("reason"), "changes": changes})
    out["history"] = out_hist
    return out

# ---------------------------------------------------------------------------
# Pydantic models (request validation)
# ---------------------------------------------------------------------------
class EmployeeCreate(BaseModel):
    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def check_emp_code(cls, v):
        if not EMP_CODE_RE.match(v):
            raise ValueError("emp_code must match ^EMP\\d{4,6}$")
        return v

    @field_validator("email")
    @classmethod
    def check_email(cls, v):
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def check_shift(cls, v):
        if not SHIFT_RE.match(v):
            raise ValueError("shift must be HH:MM")
        return v

    @field_validator("joined_on")
    @classmethod
    def check_joined(cls, v):
        try:
            date_type.fromisoformat(v)
        except Exception:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def check_shift_diff(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def check_status(cls, v):
        if v not in PRESENT_STATUSES:
            raise ValueError("status must be one of PRESENT, WFH, ON_DUTY")
        return v

class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None

class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def check_status2(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {ALL_STATUSES}")
        return v

# ---------------------------------------------------------------------------
# Index creation
# ---------------------------------------------------------------------------
def ensure_indexes(database=None):
    d = database if database is not None else get_db()
    # employees
    d.employees.create_index([("emp_code", 1)], unique=True)  # POST /employees unique, GET /employees sort, punch-in emp lookup
    d.employees.create_index([("department", 1), ("emp_code", 1)])  # GET /employees?department, leaderboard department filter
    d.employees.create_index([("department", 1), ("joined_on", 1)])  # department_summary/monthly headcount: joined_on <= month end
    # attendance_logs
    d.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)  # punch-in race (emp_code,date) unique + monthly/attendance filters
    d.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])  # punch-out: most recent punch_in <= ts
    d.attendance_logs.create_index([("date", -1), ("emp_code", 1)])  # GET /attendance sort date desc, emp_code asc + leaderboard date filter
    d.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)])  # GET /attendance with status filter

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
    try:
        ensure_indexes(client[MONGO_DB_NAME])
    except Exception:
        # Must not crash app when Mongo is down — /health will report 503
        pass
    yield
    if client is not None:
        client.close()
        client = None

app = FastAPI(title="Employee Attendance & Analytics API", version=APP_VERSION, lifespan=lifespan)

# ---------------------------------------------------------------------------
# Helpers for analytics date ranges
# ---------------------------------------------------------------------------
def month_bounds(month: str) -> tuple[str, str, str]:
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    start = f"{month}-01"
    end = f"{month}-{last:02d}"
    # last day date string for headcount: month end
    return start, end, end

def parse_month_or_422(month: str):
    if not MONTH_RE.match(month):
        raise HTTPException(422, detail=[{"msg": "month must be YYYY-MM"}])
    return month

def parse_date_or_422(s: str, field: str = "date"):
    try:
        date_type.fromisoformat(s)
    except Exception:
        raise HTTPException(422, detail=[{"msg": f"{field} must be YYYY-MM-DD"}])
    return s

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        get_client().admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = ms_to_dt(now_ms())
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    # fetch without _id for response
    saved = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    # saved may have created_at as datetime; serialize
    # ensure created_at is datetime -> ms
    if saved is None:
        # fallback to doc
        return serialize_employee(doc)
    return serialize_employee(saved)

@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, detail="unknown employee")
    # determine ts
    if body.punched_at is not None:
        ts = ms_to_dt(body.punched_at)
    else:
        ts = now_dt_trunc()
    ts_ist = ts.astimezone(IST)
    att_date = attendance_date_for_punch(ts_ist, emp["shift_start"], emp["shift_end"])
    late = compute_late_for_record(ts, emp["shift_start"], emp["shift_end"], att_date)
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="already punched in for this date")
    saved = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": att_date}, {"_id": 0})
    return serialize_attendance(saved or doc)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, detail="unknown employee")
    if body.punched_at is not None:
        ts = ms_to_dt(body.punched_at)
    else:
        ts = now_dt_trunc()
    # find most recent record with punch_in <= ts
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, detail="no punch-in found")
    # check already punched out via in-memory; but atomic check below too
    if rec.get("punch_out") is not None:
        raise HTTPException(409, detail="already punched out")
    punch_in = rec["punch_in"]
    # ensure punch_in is aware
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    punch_in = punch_in.replace(microsecond=0)
    ts = ts.replace(microsecond=0)
    if ts <= punch_in:
        raise HTTPException(422, detail=[{"msg": "punched_at must be after punch_in"}])
    if (ts - punch_in).total_seconds() > 24 * 3600:
        raise HTTPException(422, detail=[{"msg": "punch_out more than 24h after punch_in"}])
    # compute derived
    att_date = rec["date"]
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    work_hours = compute_work_hours(punch_in, ts)
    overtime = compute_overtime(ts, shift_end, att_date, shift_start)
    half_day = work_hours < 4.50
    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": half_day}},
    )
    if res.matched_count == 0:
        raise HTTPException(409, detail="already punched out")
    updated = db.attendance_logs.find_one({"_id": rec["_id"]}, {"_id": 0})
    return serialize_attendance(updated)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, detail=[{"msg": "invalid status"}])
    if date_from is not None:
        parse_date_or_422(date_from, "date_from")
    if date_to is not None:
        parse_date_or_422(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail=[{"msg": "date_from > date_to"}])
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        qr: dict = {}
        if date_from:
            qr["$gte"] = date_from
        if date_to:
            qr["$lte"] = date_to
        q["date"] = qr
    if status is not None:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    parse_date_or_422(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, detail="unknown employee")
    # retry up to 5 times for optimistic concurrency
    for attempt in range(5):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, detail="no record for that date")
        # snapshot
        old_status = rec.get("status")
        old_pi = rec.get("punch_in")
        old_po = rec.get("punch_out")
        # normalize old datetimes for comparison (aware truncated)
        def norm_dt(v):
            if v is None:
                return None
            if isinstance(v, datetime):
                if v.tzinfo is None:
                    v = v.replace(tzinfo=timezone.utc)
                return v.replace(microsecond=0)
            return v
        old_pi_n = norm_dt(old_pi)
        old_po_n = norm_dt(old_po)
        # desired new values
        new_status = body.status if body.status is not None else old_status
        # validate ABSENT/LEAVE clears punch times
        if new_status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(422, detail=[{"msg": "ABSENT/LEAVE cannot have punch times"}])
            new_pi = None
            new_po = None
        else:
            # presence status needs punch_in
            if body.punch_in is not None:
                new_pi = ms_to_dt(body.punch_in)
            else:
                new_pi = old_pi_n
            if body.punch_out is not None:
                new_po = ms_to_dt(body.punch_out)
            else:
                new_po = old_po_n
            if new_pi is None:
                raise HTTPException(422, detail=[{"msg": "presence status requires punch_in"}])
        # punch_in must stay on record's attendance date under R1
        if new_pi is not None:
            pi_ist = new_pi.astimezone(IST)
            derived_date = attendance_date_for_punch(pi_ist, emp["shift_start"], emp["shift_end"])
            if derived_date != date:
                raise HTTPException(422, detail=[{"msg": "punch_in not on attendance date"}])
        # punch_out validations
        if new_pi is not None and new_po is not None:
            if new_po <= new_pi:
                raise HTTPException(422, detail=[{"msg": "punch_out must be after punch_in"}])
            if (new_po - new_pi).total_seconds() > 24 * 3600:
                raise HTTPException(422, detail=[{"msg": "punch_out more than 24h after punch_in"}])
        # recompute derived fields
        if new_status in ("ABSENT", "LEAVE"):
            new_late = 0
            new_ot = 0
            new_wh = None
            new_hd = False
        else:
            # late from punch_in
            assert new_pi is not None
            new_late = compute_late_for_record(new_pi, emp["shift_start"], emp["shift_end"], date)
            if new_po is not None:
                new_wh = compute_work_hours(new_pi, new_po)
                new_ot = compute_overtime(new_po, emp["shift_end"], date, emp["shift_start"])
                new_hd = new_wh < 4.50
            else:
                new_wh = None
                new_ot = 0
                new_hd = False
        # build changes diff only for fields that actually changed
        changes: dict = {}
        if new_status != old_status:
            changes["status"] = {"from": old_status, "to": new_status}
        if old_pi_n != new_pi:
            changes["punch_in"] = {"from": old_pi_n, "to": new_pi}
        if old_po_n != new_po:
            changes["punch_out"] = {"from": old_po_n, "to": new_po}
        if rec.get("work_hours") != new_wh:
            changes["work_hours"] = {"from": rec.get("work_hours"), "to": new_wh}
        if int(rec.get("late_minutes", 0) or 0) != new_late:
            changes["late_minutes"] = {"from": int(rec.get("late_minutes", 0) or 0), "to": new_late}
        if int(rec.get("overtime_minutes", 0) or 0) != new_ot:
            changes["overtime_minutes"] = {"from": int(rec.get("overtime_minutes", 0) or 0), "to": new_ot}
        if bool(rec.get("half_day", False)) != new_hd:
            changes["half_day"] = {"from": bool(rec.get("half_day", False)), "to": new_hd}
        if not changes:
            raise HTTPException(422, detail=[{"msg": "no changes"}])
        # prepare update
        set_fields: dict = {}
        # status always set if changed
        set_fields["status"] = new_status
        set_fields["punch_in"] = new_pi
        set_fields["punch_out"] = new_po
        set_fields["work_hours"] = new_wh
        set_fields["late_minutes"] = new_late
        set_fields["overtime_minutes"] = new_ot
        set_fields["half_day"] = new_hd
        at_dt = now_dt_trunc()
        hist_entry = {"at": at_dt, "by": body.regularized_by, "reason": body.reason, "changes": changes}
        # optimistic filter on history length (handles missing history)
        hist_len = len(rec.get("history") or [])
        filt = {
            "_id": rec["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, hist_len]},
        }
        res = db.attendance_logs.update_one(filt, {"$set": set_fields, "$push": {"history": hist_entry}})
        if res.modified_count == 1:
            updated = db.attendance_logs.find_one({"_id": rec["_id"]}, {"_id": 0})
            return serialize_attendance(updated)
        # else retry (409 eventually)
        if attempt == 4:
            raise HTTPException(409, detail="conflict, retry")
        continue
    raise HTTPException(409, detail="conflict")

# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------
def build_employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$addFields": {
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$eq": ["$is_present", True]}, {"$ne": ["$dow", 1]}, {"$ne": ["$dow", 7]}]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, detail="unknown employee")
    parse_month_or_422(month)
    month_start, month_end, _ = month_bounds(month)
    # working days
    wd = working_days_in_month(emp["joined_on"], month)
    pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
    agg = list(db.attendance_logs.aggregate(pipeline))
    if agg:
        row = agg[0]
        present_days = float(row.get("present_days", 0) or 0)
        leave_days = int(row.get("leave_days", 0) or 0)
        late_count = int(row.get("late_count", 0) or 0)
        total_late = int(row.get("total_late_minutes", 0) or 0)
        total_ot = int(row.get("total_overtime_minutes", 0) or 0)
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0
    if wd == 0:
        pct = None
    else:
        pct = half_up_2(present_days / wd * 100)
    # present_days rounding 2 decimals? spec says 2 decimals half-up for reported numbers
    # keep 2 decimals for present_days? but half_day 0.5 sums may be .5 -> return as number with 2 decimals? We'll half_up_2
    # Actually present_days is number with 0.5 increments; round to 2 decimals
    present_days = half_up_2(present_days) if present_days != 0 else 0.0 if wd != 0 else 0.0
    # but if present_days is integer, half_up_2(1)=1.0 which is fine
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": pct,
    }

def build_department_summary_pipeline(month_start: str, month_end: str, department: Optional[str]):
    match_emp: dict = {
        "department": department if department is not None else {"$gte": ""},
        "joined_on": {"$lte": month_end},
    }
    pipeline = [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "logs",
                "pipeline": [{"$match": {"date": {"$gte": month_start, "$lte": month_end}}}],
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "log_date": "$logs.date",
                "log_status": "$logs.status",
                "log_half": {"$ifNull": ["$logs.half_day", False]},
                "log_late": {"$ifNull": ["$logs.late_minutes", 0]},
                "log_ot": {"$ifNull": ["$logs.overtime_minutes", 0]},
                "log_wh": "$logs.work_hours",
                "dow": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$log_status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$dow", 1]},
                                    {"$ne": ["$dow", 7]},
                                    {"$ne": ["$log_date", None]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$log_half", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "avg_work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$in": ["$log_status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$log_wh", None]}]},
                            "$log_wh",
                            0,
                        ]
                    }
                },
                "avg_work_hours_cnt": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$in": ["$log_status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$log_wh", None]}]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$log_late", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$log_late"},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$log_status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$log_status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": 1,
                "avg_work_hours_sum": 1,
                "avg_work_hours_cnt": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]
    return pipeline

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    parse_month_or_422(month)
    month_start, month_end, _ = month_bounds(month)
    pipeline = build_department_summary_pipeline(month_start, month_end, department)
    rows = list(db.employees.aggregate(pipeline))
    items = []
    for r in rows:
        if r.get("headcount", 0) == 0:
            continue
        cnt = r.get("avg_work_hours_cnt", 0)
        s = r.get("avg_work_hours_sum", 0)
        if cnt and cnt > 0:
            avg = half_up_2(s / cnt)
        else:
            avg = None
        items.append(
            {
                "department": r["department"],
                "headcount": int(r["headcount"]),
                "present_days": half_up_2(float(r.get("present_days", 0) or 0)),
                "avg_work_hours": avg,
                "late_count": int(r.get("late_count", 0) or 0),
                "total_late_minutes": int(r.get("total_late_minutes", 0) or 0),
                "leave_count": int(r.get("leave_count", 0) or 0),
                "on_duty_count": int(r.get("on_duty_count", 0) or 0),
            }
        )
    return {"month": month, "items": items}

def build_late_leaderboard_pipeline(month_start: str, month_end: str, department: Optional[str], limit: int):
    # we filter department before ranking, so include that in match after lookup
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.extend(
        [
            {
                "$group": {
                    "_id": "$emp_code",
                    "emp_code": {"$first": "$emp_code"},
                    "name": {"$first": "$emp.name"},
                    "department": {"$first": "$emp.department"},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": 1,
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    parse_month_or_422(month)
    month_start, month_end, _ = month_bounds(month)
    pipeline = build_late_leaderboard_pipeline(month_start, month_end, department, limit)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}

def build_department_trend_pipeline(
    from_: str,
    to: str,
    d_from: date_type,
    d_to: date_type,
    emp_codes: list[str],
    hc_branches: list[dict],
) -> list[dict]:
    dt_from_bound = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    dt_to_bound = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc) + timedelta(days=1)
    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_, "$lte": to}}},
        {
            "$group": {
                "_id": "$date",
                "present_count_raw": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id": 1}},
        {"$addFields": {"dateObj": {"$dateFromString": {"dateString": "$_id"}}}},
        {
            "$densify": {
                "field": "dateObj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [dt_from_bound, dt_to_bound],
                },
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dateObj"}},
                "present_count_raw": {"$ifNull": ["$present_count_raw", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"date": 1}},
        {
            "$addFields": {
                "headcount": {"$switch": {"branches": hc_branches, "default": 0}} if hc_branches else 0,
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$dateObj"},
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate_raw": {
                    "$cond": [
                        {"$and": [{"$eq": ["$is_working_day", True]}, {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count_raw", "$headcount"]}, 4]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate_raw",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count_raw": 1,
                "late_count": 1,
                "attendance_rate_raw": 1,
                "moving_avg_7d_raw": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = FPath(...),
    from_: str = Query(..., alias="from"),
    to: str = Query(..., alias="to"),
):
    parse_date_or_422(from_, "from")
    parse_date_or_422(to, "to")
    if to < from_:
        raise HTTPException(422, detail=[{"msg": "to < from"}])
    d_from = date_type.fromisoformat(from_)
    d_to = date_type.fromisoformat(to)
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, detail=[{"msg": "range exceeds 92 days"}])
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, detail="unknown department")
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    emp_codes = [e["emp_code"] for e in emps]

    headcount_map: dict[str, int] = {}
    cur_h = d_from
    while cur_h <= d_to:
        ds = cur_h.isoformat()
        headcount_map[ds] = sum(1 for e in emps if e["joined_on"] <= ds)
        cur_h += timedelta(days=1)

    hc_branches = [
        {"case": {"$eq": ["$date", ds]}, "then": hc}
        for ds, hc in sorted(headcount_map.items())
    ]

    pipeline = build_department_trend_pipeline(from_, to, d_from, d_to, emp_codes, hc_branches)
    rows = list(db.attendance_logs.aggregate(pipeline))

    items: list[dict] = []
    for r in rows:
        present_raw = float(r.get("present_count_raw", 0) or 0)
        rate_raw = r.get("attendance_rate_raw")
        moving_raw = r.get("moving_avg_7d_raw")

        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": int(r["headcount"]),
                "present_count": half_up_2(present_raw) if present_raw else 0.0,
                "late_count": int(r.get("late_count", 0) or 0),
                "attendance_rate": half_up_4(rate_raw) if rate_raw is not None else None,
                "moving_avg_7d": half_up_4(moving_raw) if moving_raw is not None else None,
            }
        )

    return {"department": department, "items": items}

# ---------------------------------------------------------------------------
# Explain
# ---------------------------------------------------------------------------
EXPLAIN_ENDPOINTS = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}

def _explain_for_pipeline(collection_name: str, pipeline_or_find: dict):
    # collection_name is actual mongo collection for explain
    coll = db[collection_name]
    # pipeline_or_find is either {"find":..., "filter":..., "sort":...} or {"aggregate": pipeline}
    # For simplicity we dispatch via collection's explain API
    # PyMongo's command explain: db.command("explain", {find/aggregate...}, verbosity="executionStats")
    # We use the pipeline provided
    return pipeline_or_find

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = FPath(..., pattern="^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # validate required params per endpoint, else 422
    if endpoint == "attendance_list":
        if status is not None and status not in ALL_STATUSES:
            raise HTTPException(422, detail=[{"msg": "invalid status"}])
        if date_from is not None:
            parse_date_or_422(date_from, "date_from")
        if date_to is not None:
            parse_date_or_422(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, detail=[{"msg": "date_from > date_to"}])
        q: dict = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if date_from or date_to:
            qr: dict = {}
            if date_from:
                qr["$gte"] = date_from
            if date_to:
                qr["$lte"] = date_to
            q["date"] = qr
        if status is not None:
            q["status"] = status
        # explain the same find+sort as endpoint
        cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        explain = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail=[{"msg": "emp_code and month required"}])
        parse_month_or_422(month)
        month_start, month_end, _ = month_bounds(month)
        pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
        cmd = {"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}
        explain = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail=[{"msg": "month required"}])
        parse_month_or_422(month)
        month_start, month_end, _ = month_bounds(month)
        pipeline = build_department_summary_pipeline(month_start, month_end, department)
        cmd = {"explain": {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}
        explain = db.command(cmd)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail=[{"msg": "month required"}])
        parse_month_or_422(month)
        month_start, month_end, _ = month_bounds(month)
        pipeline = build_late_leaderboard_pipeline(month_start, month_end, department, limit)
        cmd = {"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}
        explain = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    elif endpoint == "department_trend":
        # requires department (from query param, not path), from, to
        dept = department
        if not dept or not from_ or not to:
            raise HTTPException(422, detail=[{"msg": "department, from, to required"}])
        parse_date_or_422(from_, "from")
        parse_date_or_422(to, "to")
        if to < from_:
            raise HTTPException(422, detail=[{"msg": "to < from"}])
        d_from = date_type.fromisoformat(from_)
        d_to = date_type.fromisoformat(to)
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(422, detail=[{"msg": "range exceeds 92 days"}])
        emps = list(db.employees.find({"department": dept}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
        emp_codes = [e["emp_code"] for e in emps]
        headcount_map = {}
        cur_h = d_from
        while cur_h <= d_to:
            ds = cur_h.isoformat()
            headcount_map[ds] = sum(1 for e in emps if e["joined_on"] <= ds)
            cur_h += timedelta(days=1)
        hc_branches = [
            {"case": {"$eq": ["$date", ds]}, "then": hc}
            for ds, hc in sorted(headcount_map.items())
        ]
        pipeline = build_department_trend_pipeline(from_, to, d_from, d_to, emp_codes, hc_branches)
        cmd = {
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        }
        explain = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    raise HTTPException(422, detail=[{"msg": "unknown endpoint"}])

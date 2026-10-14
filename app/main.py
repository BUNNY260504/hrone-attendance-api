"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Contract: openapi.yaml (business rules R1-R10). Stored shapes: DATA_MODEL.md.

Layout of this file:
  1. Configuration and database
  2. Time and rule helpers (R1-R5, R8)
  3. Serialization (MongoDB documents <-> API JSON)
  4. Request models and validated types
  5. Query / pipeline builders, shared by the endpoints and by /admin/explain
  6. Endpoints
"""
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration and database
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False: real environment variables take priority over .env

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# tz_aware=True: PyMongo returns aware UTC datetimes instead of naive ones, so arithmetic
# with IST values and conversion to epoch milliseconds never depends on the server's local zone.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=UTC,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
ABSENCE_STATUSES = ["ABSENT", "LEAVE"]
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_BELOW_HOURS = 4.50  # R5
MAX_SHIFT_LENGTH = timedelta(hours=24)
MAX_TREND_DAYS = 92
DERIVED_FIELDS = ["late_minutes", "work_hours", "overtime_minutes", "half_day"]
AUDITED_FIELDS = ["status", "punch_in", "punch_out", *DERIVED_FIELDS]


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when an identical index already exists."""
    # Uniqueness of the business key; also serves every lookup by emp_code.
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    # GET /employees?department=..., sorted by emp_code; department filters in analytics.
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_emp_code")
    # Department summary without a department filter: joined_on <= end of month (R9).
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")

    # One record per (emp_code, date): makes concurrent punch-ins safe, serves PATCH, the
    # per-employee monthly summary, the trend ($in on emp_code + date range) and filtered lists.
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_code_date_unique")
    # GET /attendance sort order (date desc, emp_code asc) and month-wide date ranges (leaderboard).
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_code")
    # GET /attendance?status=...: filter, sort and the filtered `total` count without a collection scan.
    logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_desc_emp_code"
    )
    # Punch-out: the employee's most recent record with punch_in <= punched_at.
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_code_punch_in")


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 2. Time and rule helpers
# --------------------------------------------------------------------------- #
def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def to_ms(value: Optional[datetime]) -> Optional[int]:
    """Aware (or naive-UTC) datetime -> epoch milliseconds, with exact integer arithmetic."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    hours, minutes = map(int, value.split(":"))
    return time(hours, minutes)


def half_up(value: Decimal, places: int) -> Decimal:
    """R8: decimal half-up rounding (Python's round() is banker's rounding on binary floats)."""
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: the IST date of the punch-in; for an overnight shift, a punch-in earlier than
    shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    shift_start, shift_end = parse_hhmm(emp["shift_start"]), parse_hhmm(emp["shift_end"])
    if shift_end <= shift_start and local.time() < shift_end:
        return local.date() - timedelta(days=1)
    return local.date()


def shift_window(day: date, emp: dict) -> tuple[datetime, datetime]:
    """Shift start and end instants for an attendance day; an overnight shift ends on day + 1."""
    start = datetime.combine(day, parse_hhmm(emp["shift_start"]), IST)
    end = datetime.combine(day, parse_hhmm(emp["shift_end"]), IST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start_at: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    seconds = int((punch_in - shift_start_at).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: hours, rounded to 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float(half_up(Decimal(seconds) / Decimal(3600), 2))


def compute_overtime(punch_out: datetime, shift_end_at: datetime) -> int:
    """R3: floor minutes after shift end, counted only when >= 30."""
    minutes = int((punch_out - shift_end_at).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], day: date, emp: dict) -> dict:
    """All derived fields for a record, per the table in DATA_MODEL.md section 3."""
    if status not in PRESENCE_STATUSES:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    shift_start_at, shift_end_at = shift_window(day, emp)
    derived = {
        "late_minutes": compute_late_minutes(punch_in, shift_start_at),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        work_hours = compute_work_hours(punch_in, punch_out)
        derived.update(
            work_hours=work_hours,
            overtime_minutes=compute_overtime(punch_out, shift_end_at),
            half_day=work_hours < HALF_DAY_BELOW_HOURS,  # R5 uses the rounded value
        )
    return derived


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = map(int, month.split("-"))
    first = date(year, mon, 1)
    next_first = date(year + mon // 12, mon % 12 + 1, 1)
    return first, next_first - timedelta(days=1)


def count_working_days(start: date, end: date) -> int:
    """R7: Mon-Fri days in [start, end]."""
    if start > end:
        return 0
    days = (end - start).days + 1
    full_weeks, remainder = divmod(days, 7)
    extra = sum(1 for i in range(remainder) if (start.weekday() + i) % 7 < 5)
    return full_weeks * 5 + extra


# --------------------------------------------------------------------------- #
# 3. Serialization
# --------------------------------------------------------------------------- #
def _api_value(value: Any) -> Any:
    return to_ms(value) if isinstance(value, datetime) else value


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def serialize_history(entry: dict) -> dict:
    return {
        "at": to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            field: {"from": _api_value(change.get("from")), "to": _api_value(change.get("to"))}
            for field, change in (entry.get("changes") or {}).items()
        },
    }


def serialize_record(doc: dict) -> dict:
    """Missing legacy fields get their documented defaults; _id is never exposed."""
    work_hours = doc.get("work_hours")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": None if work_hours is None else float(work_hours),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": [serialize_history(h) for h in doc.get("history") or []],
    }


def to_jsonable(doc: Any) -> Any:
    """Raw MongoDB output (explain plans) -> plain JSON (dates, ObjectIds, Int64...)."""
    return json.loads(json_util.dumps(doc))


# --------------------------------------------------------------------------- #
# 4. Request models and validated types
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def _check_date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise ValueError("expected a date as YYYY-MM-DD")
    date.fromisoformat(value)  # rejects impossible dates such as 2026-02-30
    return value


def _check_month(value: str) -> str:
    if not MONTH_RE.fullmatch(value):
        raise ValueError("expected a month as YYYY-MM")
    return value


IsoDate = Annotated[str, AfterValidator(_check_date)]
Month = Annotated[str, AfterValidator(_check_month)]
HhMm = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# Strict: floats (even 1.0), numeric strings and booleans are rejected; the bounds reject seconds.
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def validation_error(loc: list, msg: str, error_type: str = "value_error"):
    """Raise a 422 with FastAPI's standard validation body ({"detail": [...]})."""
    raise RequestValidationError([{"type": error_type, "loc": loc, "msg": msg, "input": None}])


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: HhMm = "09:30"
    shift_end: HhMm = "18:30"
    joined_on: IsoDate

    @model_validator(mode="after")
    def _shift_has_length(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    # Derived fields are not declared, so a client sending them is ignored (pydantic drops extras).
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        # Omitted means "unchanged"; an explicit null is not a valid value for these fields.
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null; omit it to leave it unchanged")
        return self


# --------------------------------------------------------------------------- #
# 5. Query / pipeline builders (one source of truth for endpoints and explain)
# --------------------------------------------------------------------------- #
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]

# Aggregation expressions over an attendance log document.
IS_PRESENT = {"$in": ["$status", PRESENCE_STATUSES]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
PRESENT_VALUE = {"$cond": [IS_PRESENT, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}  # R5, R6
WEEKDAY_PRESENT_VALUE = {"$cond": [IS_WEEKDAY, PRESENT_VALUE, 0]}  # R7
IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}


def count_if(condition: dict) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def status_is(status: str) -> dict:
    return {"$eq": ["$status", status]}


def round_half_up_expr(expr: Any, places: int) -> dict:
    """R8 inside the database: floor(x * 10^p + 0.5) / 10^p on Decimal128 (exact, half-up for x >= 0).
    $round is not usable: it rounds half to even. Null in, null out."""
    factor = Decimal128(str(10**places))
    scaled = {"$multiply": [{"$toDecimal": expr}, factor]}
    return {"$divide": [{"$floor": {"$add": [scaled, Decimal128("0.5")]}}, factor]}


def attendance_list_filter(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    if date_from and date_to and date_from > date_to:
        validation_error(["query", "date_from"], "date_from must be on or before date_to")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": WEEKDAY_PRESENT_VALUE},
                "leave_days": count_if(status_is("LEAVE")),
                "late_count": count_if(IS_LATE),
                "total_late_minutes": {"$sum": LATE_MINUTES},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    """Runs on `employees` so that people with zero logs are part of the headcount (R9)."""
    first, last = month_bounds(month)
    employee_match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    has_work_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": logs.name,
                "localField": "emp_code",
                "foreignField": "emp_code",  # with the date range below: served by emp_code_date_unique
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "work_hours": 1}},
                ],
                "as": "logs",
            }
        },
        # One row per log; an employee with no logs keeps one row with log_index null.
        {"$unwind": {"path": "$logs", "includeArrayIndex": "log_index", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "department": 1,
                "log_index": 1,
                "date": "$logs.date",
                "status": "$logs.status",
                "half_day": "$logs.half_day",
                "late_minutes": "$logs.late_minutes",
                "work_hours": "$logs.work_hours",
            }
        },
        {
            "$group": {
                "_id": "$department",
                # Each employee contributes exactly one row with log_index 0 or null.
                "headcount": count_if({"$eq": [{"$ifNull": ["$log_index", 0]}, 0]}),
                "present_days": {"$sum": WEEKDAY_PRESENT_VALUE},
                "work_hours_sum": {"$sum": {"$cond": [has_work_hours, {"$toDecimal": "$work_hours"}, 0]}},
                "work_hours_records": count_if(has_work_hours),
                "late_count": count_if(IS_LATE),
                "total_late_minutes": {"$sum": LATE_MINUTES},
                "leave_count": count_if(status_is("LEAVE")),
                "on_duty_count": count_if(status_is("ON_DUTY")),
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {"$toDouble": round_half_up_expr("$present_days", 2)},
                # Mean over records (not a mean of per-employee means).
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_records", 0]},
                        {"$toDouble": round_half_up_expr({"$divide": ["$work_hours_sum", "$work_hours_records"]}, 2)},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {
            "$lookup": {
                "from": employees.name,
                "localField": "_id",
                "foreignField": "emp_code",
                "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}],
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},  # drops logs whose employee does not exist
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})  # rank inside the department
    pipeline += [
        # $rank is standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking, so ties at the cutoff stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def department_codes(department: str) -> list[str]:
    return [e["emp_code"] for e in employees.find({"department": department}, {"_id": 0, "emp_code": 1})]


def department_trend_pipeline(department: str, codes: list[str], date_from: str, date_to: str) -> list:
    start = datetime.combine(date.fromisoformat(date_from), time(), UTC)
    end_exclusive = datetime.combine(date.fromisoformat(date_to), time(), UTC) + timedelta(days=1)
    is_rate_day = {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": date_from, "$lte": date_to}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": PRESENT_VALUE}, "late_count": count_if(IS_LATE)}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        # Gap filling: exactly one document per calendar day in [from, to], even with no records at all.
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start, end_exclusive]}}},
        {
            "$addFields": {
                "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {  # R9 per day: department employees who had joined by that day
                "from": employees.name,
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                    {"$count": "n"},
                ],
                "as": "joined",
            }
        },
        {"$addFields": {"headcount": {"$ifNull": [{"$first": "$joined.n"}, 0]}}},
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        is_rate_day,
                        round_half_up_expr({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
                        None,
                    ]
                }
            }
        },
        # Only rows inside [from, to] exist here, so the first rows naturally get a shorter window.
        # $avg ignores nulls and returns null when the window holds no rate.
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {"moving_avg": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$toDouble": "$present_count"},
                "late_count": 1,
                "attendance_rate": {"$toDouble": "$attendance_rate"},
                "moving_avg_7d": {"$toDouble": round_half_up_expr("$moving_avg", 4)},
            }
        },
    ]


def check_trend_range(date_from: str, date_to: str) -> None:
    if date_to < date_from:
        validation_error(["query", "to"], "to must be on or after from")
    if (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1 > MAX_TREND_DAYS:
        validation_error(["query", "to"], f"the range cannot exceed {MAX_TREND_DAYS} days")


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 6. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # the unique index decides races; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    cursor = (
        employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(d) for d in cursor],
        "total": employees.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee_or_404(body.emp_code)
    punched_at = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(punched_at, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **derive(body.status, punched_at, None, day, emp),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee_or_404(body.emp_code)
    punched_at = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    record = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},  # $lte on a date skips nulls
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if punched_at <= record["punch_in"]:
        validation_error(["body", "punched_at"], "punched_at must be after punch_in")
    if punched_at - record["punch_in"] > MAX_SHIFT_LENGTH:
        validation_error(["body", "punched_at"], "punched_at must be within 24 hours of punch_in")

    derived = derive(record["status"], record["punch_in"], punched_at, date.fromisoformat(record["date"]), emp)
    updated = logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},  # atomic: only one concurrent punch-out matches
        {"$set": {"punch_out": punched_at, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[IsoDate] = None,
    date_to: Optional[IsoDate] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query = attendance_list_filter(emp_code, date_from, date_to, status)
    cursor = logs.find(query).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [serialize_record(d) for d in cursor],
        "total": logs.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: Annotated[IsoDate, Path()], body: RegularizeRequest):
    emp = get_employee_or_404(emp_code)
    record = logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "no attendance record for this date")
    day = datetime.strptime(date, "%Y-%m-%d").date()

    new_status = body.status or record["status"]
    new_in = from_ms(body.punch_in) if body.punch_in is not None else None
    new_out = from_ms(body.punch_out) if body.punch_out is not None else None
    times_supplied = new_in is not None or new_out is not None

    if new_status in ABSENCE_STATUSES:
        if times_supplied:
            validation_error(["body"], f"{new_status} clears both punch times; do not send punch_in or punch_out")
        final_in = final_out = None
    else:
        final_in = new_in or record.get("punch_in")
        final_out = new_out or record.get("punch_out")
        if final_in is None:
            validation_error(["body", "punch_in"], f"status {new_status} requires a punch_in")
        if new_in is not None and attendance_date(new_in, emp) != day:
            validation_error(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if times_supplied and final_out is not None:
            if final_out <= final_in:
                validation_error(["body", "punch_out"], "punch_out must be after punch_in")
            if final_out - final_in > MAX_SHIFT_LENGTH:
                validation_error(["body", "punch_out"], "punch_out must be within 24 hours of punch_in")

    before = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "late_minutes": record.get("late_minutes") or 0,
        "work_hours": record.get("work_hours"),
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day") or False),
    }
    after = {"status": new_status, "punch_in": final_in, "punch_out": final_out,
             **derive(new_status, final_in, final_out, day, emp)}
    changes = {f: {"from": before[f], "to": after[f]} for f in AUDITED_FIELDS if before[f] != after[f]}
    if not changes:
        validation_error(["body"], "the request does not change anything")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history = record.get("history")
    # Optimistic concurrency: the update only applies if the record is still exactly as we read it
    # (same punch fields and same number of history entries); a concurrent writer makes us return 409.
    guard = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(history or [])]},
    }
    update: dict = {"$set": after}
    if isinstance(history, list):
        update["$push"] = {"history": entry}
    else:  # history missing or null on a legacy record
        update["$set"]["history"] = [entry]
    updated = logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently; retry")
    return serialize_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[Month, Query()]):
    emp = get_employee_or_404(emp_code)
    first, last = month_bounds(month)
    joined_on = date.fromisoformat(emp["joined_on"])
    working_days = count_working_days(max(first, joined_on), last)

    totals = next(logs.aggregate(employee_monthly_pipeline(emp_code, month)), {})
    present_days = Decimal(str(totals.get("present_days", 0)))
    attendance_pct = (
        float(half_up(present_days * 100 / working_days, 2)) if working_days else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(half_up(present_days, 2)),
        "leave_days": int(totals.get("leave_days", 0)),
        "late_count": int(totals.get("late_count", 0)),
        "total_late_minutes": int(totals.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(totals.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[Month, Query()], department: Optional[str] = None):
    items = list(employees.aggregate(department_summary_pipeline(month, department)))
    for item in items:
        for field in ("headcount", "late_count", "total_late_minutes", "leave_count", "on_duty_count"):
            item[field] = int(item[field])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[Month, Query()],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    items = list(logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    for item in items:
        item["total_late_minutes"] = int(item["total_late_minutes"])
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: Annotated[IsoDate, Query(alias="from")],
    date_to: Annotated[IsoDate, Query(alias="to")],
):
    check_trend_range(date_from, date_to)
    codes = department_codes(department)
    if not codes:
        raise HTTPException(404, "department not found")
    items = list(logs.aggregate(department_trend_pipeline(department, codes, date_from, date_to)))
    return {"department": department, "items": items}


ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _require(**params: Any) -> None:
    for name, value in params.items():
        if value is None:
            validation_error(["query", name], "Field required", "missing")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[Month], Query()] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[IsoDate] = None,
    date_to: Optional[IsoDate] = None,
    status: Optional[Status] = None,
    trend_from: Annotated[Optional[IsoDate], Query(alias="from")] = None,
    trend_to: Annotated[Optional[IsoDate], Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        collection = logs
        command = {
            "find": logs.name,
            "filter": attendance_list_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            collection, pipeline = logs, employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            _require(month=month)
            collection, pipeline = employees, department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            _require(month=month)
            collection, pipeline = logs, late_leaderboard_pipeline(month, limit, department)
        else:  # department_trend
            _require(department=department, **{"from": trend_from, "to": trend_to})
            check_trend_range(trend_from, trend_to)
            codes = department_codes(department)
            collection, pipeline = logs, department_trend_pipeline(department, codes, trend_from, trend_to)
        command = {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}

    plan = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection.name, "explain": to_jsonable(plan)}

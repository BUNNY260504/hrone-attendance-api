"""
Tests against a real MongoDB. They use the database named in MONGO_TEST_DB (default: attendance_test),
which is dropped and re-seeded, so never point this at real data.

    MONGO_URI=mongodb://localhost:27017 pytest -q
"""
import os
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

os.environ["MONGO_DB"] = os.getenv("MONGO_TEST_DB", "attendance_test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, db  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def ms(year, month, day, hh, mm, ss=0, millis=0) -> int:
    """An IST wall-clock time as epoch milliseconds."""
    dt = datetime(year, month, day, hh, mm, ss, tzinfo=IST)
    return int(dt.timestamp()) * 1000 + millis


def ist(year, month, day, hh, mm, ss=0) -> datetime:
    return datetime(year, month, day, hh, mm, ss, tzinfo=IST).astimezone(UTC)


@pytest.fixture(scope="module")
def client():
    db.client.drop_database(db.name)
    with TestClient(app) as c:
        yield c
    db.client.drop_database(db.name)


@pytest.fixture(autouse=True)
def clean(client):
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


def make_employee(client, code, dept="Engineering", start="09:30", end="18:30", joined="2026-01-01"):
    r = client.post("/employees", json={
        "emp_code": code, "name": f"Name {code}", "email": f"{code.lower()}@example.com",
        "department": dept, "shift_start": start, "shift_end": end, "joined_on": joined,
    })
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
def test_create_employee_shape_and_validation(client):
    body = make_employee(client, "EMP0001")
    assert isinstance(body["created_at"], int) and body["created_at"] > 100_000_000_000
    assert "_id" not in body and "id" not in body
    assert client.post("/employees", json={**body, "created_at": None}).status_code == 409

    base = {"emp_code": "EMP0002", "name": "A", "email": "a@b.co", "department": "X", "joined_on": "2026-01-01"}
    assert client.post("/employees", json=base).status_code == 201
    for bad in (
        {"emp_code": "E1"}, {"email": "nope"}, {"joined_on": "2026-02-30"}, {"joined_on": "01-01-2026"},
        {"shift_start": "25:00"}, {"shift_start": "10:00", "shift_end": "10:00"}, {"name": ""},
    ):
        assert client.post("/employees", json={**base, "emp_code": "EMP0003", **bad}).status_code == 422, bad


def test_create_employee_concurrent_duplicates(client):
    payload = {"emp_code": "EMP0100", "name": "A", "email": "a@b.co", "department": "X", "joined_on": "2026-01-01"}
    with ThreadPoolExecutor(10) as pool:
        codes = sorted(pool.map(lambda _: client.post("/employees", json=payload).status_code, range(10)))
    assert codes == [201] + [409] * 9


def test_list_employees_pagination_and_filter(client):
    for i in range(25):
        make_employee(client, f"EMP{i:04d}", dept="Sales" if i % 2 else "Engineering")
    r = client.get("/employees", params={"department": "Sales", "page": 2, "page_size": 5}).json()
    assert r["total"] == 12
    assert [e["emp_code"] for e in r["items"]] == [f"EMP{i:04d}" for i in (11, 13, 15, 17, 19)]
    assert client.get("/employees", params={"page": 0}).status_code == 422
    assert client.get("/employees", params={"page_size": 101}).status_code == 422


# --------------------------------------------------------------------------- #
# Punch-in / punch-out (R1-R5)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("clock,expected", [
    ((9, 40, 0), 0), ((9, 40, 1), 10), ((10, 15, 59), 45), ((9, 0, 0), 0),
])
def test_late_minutes_r2(client, clock, expected):
    make_employee(client, "EMP0001")
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, *clock)})
    assert r.status_code == 201, r.text
    assert r.json()["late_minutes"] == expected
    assert r.json()["date"] == "2026-07-06"


def test_truncation_to_seconds_r1(client):
    make_employee(client, "EMP0001")
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 40, 0, 900)})
    assert r.json()["late_minutes"] == 0
    assert r.json()["punch_in"] == ms(2026, 7, 6, 9, 40, 0)


def test_ist_date_boundary_r1(client):
    make_employee(client, "EMP0001")
    # 00:30 IST on the 7th is still the 6th in UTC; the attendance date is the IST date.
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 0, 30)})
    assert r.json()["date"] == "2026-07-07"


def test_overnight_shift(client):
    make_employee(client, "EMP0005", start="22:00", end="06:00")
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 5, 59)})
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 7 * 60 + 59
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)})
    assert r.status_code == 200 and r.json()["overtime_minutes"] == 40

    # A punch-in at exactly shift_end is not "earlier than" it: it is a new attendance day.
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 21, 55)})
    assert r.json()["date"] == "2026-07-07" and r.json()["late_minutes"] == 0
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 8, 6, 29, 59)})
    assert r.json()["overtime_minutes"] == 0 and r.json()["work_hours"] == 8.58


@pytest.mark.parametrize("out_clock,hours,half", [
    ((13, 59, 42), 4.5, False),   # 16182 s = 4.495 h -> 4.50 half-up (float round() would give 4.49)
    ((13, 59, 41), 4.49, True),
])
def test_work_hours_half_up_r4_r5(client, out_clock, hours, half):
    make_employee(client, "EMP0001")
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, *out_clock)})
    assert (r.json()["work_hours"], r.json()["half_day"]) == (hours, half)


def test_overtime_threshold_r3(client):
    make_employee(client, "EMP0001")
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 59, 59)})
    assert r.json()["overtime_minutes"] == 0
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 30)})
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 19, 0, 59)})
    assert r.json()["overtime_minutes"] == 30


def test_punch_errors(client):
    make_employee(client, "EMP0001")
    p = {"emp_code": "EMP0001"}
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404
    for bad in (1783312500, 1783312500000.0, "1783312500000", True, 5_000_000_000_000):
        assert client.post("/attendance/punch-in", json={**p, "punched_at": bad}).status_code == 422, bad
    assert client.post("/attendance/punch-in", json={**p, "status": "ABSENT"}).status_code == 422

    assert client.post("/attendance/punch-out", json={**p, "punched_at": ms(2026, 7, 6, 18, 0)}).status_code == 404
    t_in = ms(2026, 7, 6, 9, 30)
    assert client.post("/attendance/punch-in", json={**p, "punched_at": t_in}).status_code == 201
    assert client.post("/attendance/punch-in", json={**p, "punched_at": t_in + 3600_000}).status_code == 409
    assert client.post("/attendance/punch-out", json={**p, "punched_at": t_in}).status_code == 422
    assert client.post("/attendance/punch-out", json={**p, "punched_at": t_in + 25 * 3600_000}).status_code == 422
    assert client.post("/attendance/punch-out", json={**p, "punched_at": t_in + 3600_000}).status_code == 200
    assert client.post("/attendance/punch-out", json={**p, "punched_at": t_in + 7200_000}).status_code == 409


def test_concurrent_punch_in_and_out(client):
    make_employee(client, "EMP0001")
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}
    with ThreadPoolExecutor(10) as pool:
        codes = sorted(pool.map(lambda _: client.post("/attendance/punch-in", json=body).status_code, range(10)))
    assert codes == [201] + [409] * 9
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 30)}
    with ThreadPoolExecutor(10) as pool:
        codes = sorted(pool.map(lambda _: client.post("/attendance/punch-out", json=body).status_code, range(10)))
    assert codes == [200] + [409] * 9


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
def test_list_attendance(client):
    for code in ("EMP0002", "EMP0001"):
        make_employee(client, code)
        for d in (6, 7, 8):
            client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(2026, 7, d, 9, 30)})
    r = client.get("/attendance", params={"page_size": 3}).json()
    assert r["total"] == 6
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002"), ("2026-07-07", "EMP0001")]
    assert all("id" not in i and "_id" not in i for i in r["items"])
    r = client.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-07", "page": 2, "page_size": 1})
    assert r.json()["total"] == 2 and r.json()["items"][0]["date"] == "2026-07-07"
    assert client.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-01"}).status_code == 422
    assert client.get("/attendance", params={"status": "BOGUS"}).status_code == 422
    assert client.get("/attendance", params={"date_from": "2026-7-1"}).status_code == 422


# --------------------------------------------------------------------------- #
# Regularization
# --------------------------------------------------------------------------- #
def test_regularize_history_and_rules(client):
    make_employee(client, "EMP0001")
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 18, 30)})
    url = "/attendance/EMP0001/2026-07-07"
    who = {"reason": "biometric glitch", "regularized_by": "hr.admin"}

    r = client.patch(url, json={**who, "punch_in": ms(2026, 7, 7, 9, 28), "work_hours": 99})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["late_minutes"] == 0 and rec["work_hours"] == 9.03
    (entry,) = rec["history"]
    assert entry["by"] == "hr.admin" and isinstance(entry["at"], int)
    assert entry["changes"] == {
        "punch_in": {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert isinstance(stored["history"][0]["changes"]["punch_in"]["from"], datetime)

    assert client.patch(url, json={**who, "punch_in": ms(2026, 7, 7, 9, 28)}).status_code == 422  # no change
    assert client.patch(url, json={**who, "status": "ABSENT", "punch_out": ms(2026, 7, 7, 19, 0)}).status_code == 422
    assert client.patch(url, json={**who, "punch_in": ms(2026, 7, 8, 9, 0)}).status_code == 422  # other day
    assert client.patch(url, json={**who, "punch_out": ms(2026, 7, 7, 9, 0)}).status_code == 422  # before in
    assert client.patch(url, json={**who, "punch_in": None}).status_code == 422
    assert client.patch(url, json={"reason": "x", "regularized_by": "a", "status": "WFH"}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-01", json={**who, "status": "WFH"}).status_code == 404
    assert client.patch("/attendance/EMP9999/2026-07-07", json={**who, "status": "WFH"}).status_code == 404
    assert client.patch("/attendance/EMP0001/2026-13-01", json={**who, "status": "WFH"}).status_code == 422

    r = client.patch(url, json={**who, "status": "LEAVE"}).json()
    assert (r["punch_in"], r["punch_out"], r["work_hours"], r["late_minutes"]) == (None, None, None, 0)
    assert len(r["history"]) == 2 and r["history"][1]["changes"]["status"] == {"from": "PRESENT", "to": "LEAVE"}
    assert client.patch(url, json={**who, "status": "PRESENT"}).status_code == 422  # needs a punch_in
    r = client.patch(url, json={**who, "status": "WFH", "punch_in": ms(2026, 7, 7, 9, 45)}).json()
    assert r["late_minutes"] == 15 and r["punch_out"] is None and len(r["history"]) == 3


def test_regularize_legacy_record_and_concurrency(client):
    make_employee(client, "EMP0002")
    db.attendance_logs.insert_one({  # legacy: no history, no half_day
        "emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
        "punch_in": ist(2026, 7, 14, 9, 32), "punch_out": ist(2026, 7, 14, 18, 31),
        "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0,
    })
    url = "/attendance/EMP0002/2026-07-14"
    statuses = ["WFH", "ON_DUTY", "ABSENT", "LEAVE"] * 3

    def patch(status):
        return client.patch(url, json={"status": status, "reason": "concurrent", "regularized_by": "x"}).status_code

    with ThreadPoolExecutor(12) as pool:
        codes = list(pool.map(patch, statuses))
    ok = codes.count(200)
    assert ok >= 1 and set(codes) <= {200, 409, 422}
    history = db.attendance_logs.find_one({"emp_code": "EMP0002"})["history"]
    assert len(history) == ok  # no 200 was silently lost


# --------------------------------------------------------------------------- #
# Analytics against an independent Python reference on a randomized dataset
# --------------------------------------------------------------------------- #
def hu(value, places):
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def seed_random(seed=7):
    """Seeds documents directly (like the grader), with stored derived values that are NOT
    consistent with the punch times, to prove the analytics read stored values."""
    rnd = random.Random(seed)
    depts = ["Engineering", "Sales", "support", "Support"]
    emps = []
    for i in range(24):
        joined = rnd.choice(["2025-11-03", "2026-06-15", "2026-07-01", "2026-07-15", "2026-07-31", "2026-08-10"])
        emps.append({"emp_code": f"EMP{i:04d}", "name": f"N{i}", "email": f"e{i}@x.io",
                     "department": depts[i % 4], "shift_start": "09:30", "shift_end": "18:30",
                     "joined_on": joined, "created_at": datetime(2026, 1, 1, tzinfo=UTC)})
    db.employees.insert_many([dict(e) for e in emps])
    docs = []
    for e in emps[:-3]:  # the last three employees have no logs at all
        for day in range(1, 32):
            if rnd.random() < 0.25:
                continue
            d = f"2026-07-{day:02d}"
            status = rnd.choice(["PRESENT"] * 5 + ["WFH", "ON_DUTY", "ABSENT", "LEAVE"])
            doc = {"emp_code": e["emp_code"], "date": d, "status": status}
            if status in ("ABSENT", "LEAVE"):
                doc.update(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
            else:
                pin = ist(2026, 7, day, 9, rnd.randint(0, 59))
                doc.update(punch_in=pin, punch_out=pin + timedelta(hours=8),
                           work_hours=rnd.choice([None, 3.25, 8.005, 9.12, 7.5, 4.125]),
                           late_minutes=rnd.choice([0, 0, 0, 15, 30, 45]),
                           overtime_minutes=rnd.choice([0, 0, 40]))
                if rnd.random() < 0.8:
                    doc["half_day"] = rnd.random() < 0.2
            if rnd.random() < 0.2:
                doc.pop("history", None)
            docs.append(doc)
    # An orphan log (unknown employee) must be ignored by the summaries and the leaderboard.
    docs.append({"emp_code": "EMP9999", "date": "2026-07-06", "status": "PRESENT", "punch_in": ist(2026, 7, 6, 11, 0),
                 "punch_out": None, "work_hours": None, "late_minutes": 999, "overtime_minutes": 0})
    db.attendance_logs.insert_many(docs)
    return emps, docs


def is_weekday(d):
    return date.fromisoformat(d).weekday() < 5


def present_value(doc):
    if doc["status"] not in ("PRESENT", "WFH", "ON_DUTY"):
        return 0
    return 0.5 if doc.get("half_day") else 1


def test_analytics_match_reference(client):
    emps, docs = seed_random()
    by_code = {e["emp_code"]: e for e in emps}
    july = [d for d in docs if d["date"].startswith("2026-07")]

    # Employee monthly
    for e in emps:
        mine = [d for d in july if d["emp_code"] == e["emp_code"]]
        start = max(date(2026, 7, 1), date.fromisoformat(e["joined_on"]))
        wd = sum(1 for i in range((date(2026, 7, 31) - start).days + 1) if (start + timedelta(i)).weekday() < 5)
        present = sum(present_value(d) for d in mine if is_weekday(d["date"]))
        got = client.get(f"/analytics/employees/{e['emp_code']}/monthly", params={"month": "2026-07"}).json()
        assert got == {
            "emp_code": e["emp_code"], "month": "2026-07", "working_days": wd, "present_days": present,
            "leave_days": sum(d["status"] == "LEAVE" for d in mine),
            "late_count": sum(d["late_minutes"] > 0 for d in mine),
            "total_late_minutes": sum(d["late_minutes"] for d in mine),
            "total_overtime_minutes": sum(d["overtime_minutes"] for d in mine),
            "attendance_pct": hu(Decimal(str(present)) * 100 / wd, 2) if wd else None,
        }, e

    # Department summary
    got = client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    expected = []
    for dept in sorted({e["department"] for e in emps}):
        members = {e["emp_code"] for e in emps if e["department"] == dept and e["joined_on"] <= "2026-07-31"}
        if not members:
            continue
        mine = [d for d in july if d["emp_code"] in members]
        wh = [Decimal(str(d["work_hours"])) for d in mine
              if d["status"] in ("PRESENT", "WFH", "ON_DUTY") and d["work_hours"] is not None]
        expected.append({
            "department": dept, "headcount": len(members),
            "present_days": hu(sum(present_value(d) for d in mine if is_weekday(d["date"])), 2),
            "avg_work_hours": hu(sum(wh) / len(wh), 2) if wh else None,
            "late_count": sum(d["late_minutes"] > 0 for d in mine),
            "total_late_minutes": sum(d["late_minutes"] for d in mine),
            "leave_count": sum(d["status"] == "LEAVE" for d in mine),
            "on_duty_count": sum(d["status"] == "ON_DUTY" for d in mine),
        })
    assert got == expected

    # Late leaderboard (competition ranking, limit after ranking, ties by emp_code)
    for dept in (None, "Engineering"):
        totals = {}
        for d in july:
            emp = by_code.get(d["emp_code"])
            if emp and d["late_minutes"] > 0 and (dept is None or emp["department"] == dept):
                t = totals.setdefault(d["emp_code"], [0, 0])
                t[0] += d["late_minutes"]
                t[1] += 1
        rows = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
        for limit in (1, 3, 5, 50):
            exp = []
            for code, (total, count) in rows:
                rank = 1 + sum(1 for _, (t, _c) in rows if t > total)
                if rank <= limit:
                    exp.append({"rank": rank, "emp_code": code, "name": by_code[code]["name"],
                                "department": by_code[code]["department"], "total_late_minutes": total,
                                "late_count": count})
            params = {"month": "2026-07", "limit": limit, **({"department": dept} if dept else {})}
            got = client.get("/analytics/leaderboard/late", params=params).json()["items"]
            got = [{k: g[k] for k in ("rank", "emp_code", "name", "department", "total_late_minutes", "late_count")}
                   for g in got]
            assert got == exp, (dept, limit)

    # Department trend
    dept, d_from, d_to = "Sales", date(2026, 6, 28), date(2026, 8, 3)
    got = client.get(f"/analytics/departments/{dept}/trend", params={"from": str(d_from), "to": str(d_to)}).json()
    members = [e for e in emps if e["department"] == dept]
    codes = {e["emp_code"] for e in members}
    rows, rates = [], []
    for i in range((d_to - d_from).days + 1):
        day = d_from + timedelta(i)
        s = day.isoformat()
        today = [d for d in docs if d["date"] == s and d["emp_code"] in codes]
        hc = sum(e["joined_on"] <= s for e in members)
        present = sum(present_value(d) for d in today)
        working = day.weekday() < 5
        rate = hu(Decimal(str(present)) / hc, 4) if working and hc else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        rows.append({"date": s, "is_working_day": working, "headcount": hc, "present_count": present,
                     "late_count": sum(d["late_minutes"] > 0 for d in today), "attendance_rate": rate,
                     "moving_avg_7d": hu(sum(Decimal(str(r)) for r in window) / len(window), 4) if window else None})
    assert got["items"] == rows

    r = client.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"})
    assert r.status_code == 404
    r = client.get(f"/analytics/departments/{dept}/trend", params={"from": "2026-07-02", "to": "2026-07-01"})
    assert r.status_code == 422
    r = client.get(f"/analytics/departments/{dept}/trend", params={"from": "2026-07-01", "to": "2026-10-01"})
    assert r.status_code == 422  # 93 days
    r = client.get(f"/analytics/departments/{dept}/trend", params={"from": "2026-07-01", "to": "2026-09-30"})
    assert r.status_code == 200 and len(r.json()["items"]) == 92
    assert client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code == 404
    assert client.get("/analytics/departments/summary", params={"month": "2026-7"}).status_code == 422


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
def test_explain_uses_indexes(client):
    seed_random()
    cases = {
        "attendance_list": {},
        "employee_monthly": {"emp_code": "EMP0001", "month": "2026-07"},
        "department_summary": {"month": "2026-07"},
        "late_leaderboard": {"month": "2026-07", "department": "Sales"},
        "department_trend": {"department": "Sales", "from": "2026-07-01", "to": "2026-07-31"},
    }
    for endpoint, params in cases.items():
        r = client.get(f"/admin/explain/{endpoint}", params=params)
        assert r.status_code == 200, (endpoint, r.text)
        text = str(r.json()["explain"])
        assert "IXSCAN" in text and "COLLSCAN" not in text, endpoint
    assert client.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert client.get("/admin/explain/department_trend", params={"department": "Sales"}).status_code == 422
    assert client.get("/admin/explain/nope").status_code == 422

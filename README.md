# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne assignment (contract v2.0). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or put them in a local .env
python sample_seed.py            # optional: loads the nine sample documents
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ is required (it uses `$densify`, `$setWindowFields`, `$lookup` with `localField` + `pipeline`). A quick local server: `docker run -d -p 27017:27017 mongo:7`.

Indexes are created idempotently at startup. Interactive docs are at `http://localhost:8000/docs`.

## Tests
```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
The tests need a running MongoDB. They **drop and re-seed** the database named in `MONGO_TEST_DB` (default `attendance_test`). They cover R1-R5 edge cases (grace boundary, ms truncation, IST date boundary, overnight shifts, half-up rounding at 4.495 h), concurrency (punch-in, punch-out, duplicate employees, concurrent PATCHes), pagination, validation, explain plans, and a randomized dataset where every analytics endpoint is compared with an independent Python reference. That dataset includes ties, weekends, mid-month joiners, employees with no logs, orphan logs and inconsistent stored values.

## Notes and interpretations
* **Time:** the client is `tz_aware`, so datetimes are always aware UTC. Instants are truncated to whole seconds at the API boundary (`ms // 1000`).
* **Rounding (R8)** is half-up on exact decimals: Python `Decimal` for single values, and `Decimal128` with `floor(x*10^p + 0.5)` inside pipelines, because MongoDB's `$round` rounds half to even. `moving_avg_7d` averages the already-rounded daily rates.
* **Trend range:** "exceeds 92 days" is read as more than 92 calendar days inclusive, so a full quarter (for example 07-01..09-30) is allowed.
* **Punch-out** picks the latest record with `punch_in <= punched_at`. If that record is already closed the result is 409, even if an older open record exists.
* **PATCH** checks the "same attendance date" and "within 24 h" rules only when the request supplies the corresponding times, so a status-only correction never fails because of legacy stored data. Concurrent PATCHes use optimistic concurrency (the update is guarded on status, punches and history length). The loser gets 409, and no history entry is lost.
* `working_days` (pure calendar arithmetic) is computed in Python. Everything that reads attendance data runs in MongoDB aggregations.

## Missing
Nothing from the contract is knowingly missing.

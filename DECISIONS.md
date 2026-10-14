# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, enforces no duplicates), `(department, emp_code)` (filtered, sorted list; department lookups), `joined_on` (summary headcount). `attendance_logs`: unique `(emp_code, date)` (one record per day; monthly, trend, PATCH), `(date desc, emp_code)` (list sort, leaderboard month range), `(status, date desc, emp_code)` (status filter and its count), `(emp_code, punch_in desc)` (punch-out's latest record). Rejected: a lone `status` index, because it is low-cardinality and does not help the sort.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. There is no read beforehand. MongoDB's unique index admits exactly one. The other gets `DuplicateKeyError`, which I return as 409.

3. **Ties.** `$setWindowFields` with `$rank` gives competition ranks (1, 2, 2, 4). I filter `rank <= limit` afterwards, so everyone tied at the cutoff is returned, ordered by `emp_code`. The response can exceed `limit`, as the contract requires.

4. **Headcount.** The pipeline starts from `employees` (`joined_on <= month end`), `$lookup`s logs, and unwinds with `preserveNullAndEmptyArrays`. Each employee keeps one row with index 0 or null, and I count those rows.

5. **At 100x.** Precompute daily per-department rollups when a record is written, so trend and summary read about 30 small documents instead of scanning raw logs.

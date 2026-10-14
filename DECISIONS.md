# DECISIONS.md

1. **Indexes.** On `attendance_logs`:
   * unique `(emp_code, date)` guarantees one record per day and serves the monthly summary, the trend and PATCH;
   * `(date desc, emp_code)` matches the list's sort order and the leaderboard's month range;
   * `(status, date desc, emp_code)` serves a status filter and its `total` count;
   * `(emp_code, punch_in desc)` finds the record a punch-out closes.

   On `employees`: unique `emp_code`, `(department, emp_code)`, and `joined_on` for headcount. I rejected a plain `status` index: with only five values it barely narrows the search and cannot provide the sort.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and go straight to `insert_one`; there is no "does it exist?" check first. The unique index lets exactly one insert succeed. The other raises `DuplicateKeyError`, which I return as 409.

3. **Ties.** `$rank` gives tied employees the same rank and skips the next (1, 2, 2, 4). I keep `rank <= limit` *after* ranking, so everyone tied at the cutoff is returned, sorted by `emp_code`, even if that exceeds `limit`.

4. **Headcount.** The pipeline starts from `employees` (joined by month end) and joins their logs. Employees with no logs keep a single empty row, so each person is counted exactly once.

5. **At 100x.** Maintain daily per-department totals on every write, so the summary and trend read a few rows instead of scanning raw logs.

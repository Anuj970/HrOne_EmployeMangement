# DECISIONS.md

1. **Indexes.** I created unique `emp_code` and `(emp_code, date)` indexes to prevent duplicate employees and duplicate daily attendance records. The `(date desc, emp_code)` index supports attendance-list sorting and date-range queries. The `(emp_code, punch_in)` index helps find records for punch-out, while status and department-related indexes support filtering and summary queries. I rejected a separate `late_minutes` index because the date filter already narrows the leaderboard query, and extra indexes increase write costs.

2. **Punch-in race.** Both requests attempt `insert_one`. The unique `(emp_code, date)` index allows only one insert to succeed. The losing request raises `DuplicateKeyError` and returns HTTP 409. No separate check-before-insert is needed.

3. **Ties.** The leaderboard applies `$rank` before filtering ranks up to `limit`. Employees tied at the cutoff are all included, so the result may exceed the requested limit.

4. **Headcount.** The summary starts from employees and joins attendance logs using `$lookup`. `$unwind` preserves employees with no logs, and distinct employee codes are counted for headcount.

5. **100× scale.** I would precompute daily department summaries to reduce repeated aggregation over attendance records.
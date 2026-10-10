# REVIEW.md

## Defects Identified and Fixes

The table below summarizes the defects identified in the starter implementation, how to reproduce them, and the corresponding fixes.

| # | Location | Defect | How to Reproduce or Observe | Fix |
|---:|---|---|---|---|
| 1 | MongoDB configuration | Configuration could fall back silently to a local/default database, connection timeout was not explicit, and datetimes could be naive. | Unset `MONGO_URI`; stop MongoDB and observe connection behavior; inspect datetime timezone handling. | Require `MONGO_URI` and `MONGO_DB`, set a 3-second server-selection timeout, and use timezone-aware datetimes. |
| 2 | Startup and index creation | Required indexes were not created. | Inspect collection indexes and query plans; duplicate records or collection scans may occur as data grows. | Create named, idempotent indexes through `ensure_indexes()` at startup. |
| 3 | `health()` | The health endpoint could report success without checking MongoDB connectivity. | Disconnect MongoDB and call `GET /health`. | Ping MongoDB and return HTTP 503 if the database is unavailable. |
| 4 | `compute_late_minutes()` | Shift-start calculation could use the wrong timezone/date for IST or overnight shifts. Applying the grace period after flooring could also misclassify delays just over 10 minutes. | For a 09:30 shift, test a punch-in at 09:40:30; also test an overnight shift. | Build shift start in IST for the attendance date, compare the exact delay against 600 seconds, then calculate whole late minutes. |
| 5 | `compute_work_hours()` | Python's default rounding may not match decimal half-up rounding, and half-day status could depend on a rounded value. | Test 4.505 hours and values near the half-day boundary. | Use `Decimal` with `ROUND_HALF_UP` and apply the half-day rule consistently. |
| 6 | `compute_overtime()` and punch-out | Overnight shift end could be assigned to the wrong day; naive/aware datetime comparison could fail; the 30-minute minimum could be missing and the helper unused. | Test a shift crossing midnight, timezone-aware timestamps, and overtime just below and at 30 minutes. | Handle overnight shift end, use timezone-aware datetimes, enforce the minimum threshold, and invoke the calculation during punch-out. |
| 7 | Employee input model and `POST /employees` | Validation for employee code, email, field lengths, shift-time format, joining date, and distinct shift times was insufficient. | Submit malformed codes/emails, overlong values, invalid `HH:MM`, an invalid `joined_on` date, or identical shift times such as `09:30` and `09:30`. | Validate documented patterns, lengths, and dates, and reject identical shift times with HTTP 422. |
| 8 | `create_employee()` | Check-then-insert could race, and timestamps could use local time or inconsistent response formats. | Send concurrent requests with the same `emp_code`; inspect the `created_at` response. | Enforce a unique `emp_code` index, map duplicate-key errors to HTTP 409, and return server-generated timestamps as epoch milliseconds. |
| 9 | `list_employees()` | Pagination offsets, filtered totals, ordering, or query validation could be incorrect or missing. | Request page 2; filter by department and check `total`; submit invalid pagination values. | Calculate the offset as `(page - 1) * page_size`, apply filters to the count, sort by `emp_code`, and validate pagination parameters. |
| 10 | `punch_in()` and employee lookup | Punch-in for an unknown employee could return HTTP 500 instead of HTTP 404. | Punch in using a nonexistent `emp_code`. | Check employee existence and return HTTP 404 when the employee is missing. |
| 11 | `punch_in()` timestamp conversion | Timestamp conversion could depend on local timezone, retain fractional seconds, derive the wrong IST attendance date near midnight, or treat zero as missing. | Test integer seconds, floats, strings, fractional milliseconds, zero, and timestamps near an IST date boundary. | Validate epoch milliseconds strictly, normalize timezone handling, and derive the attendance date according to the API contract. |
| 12 | `punch_in()` status validation | Arbitrary attendance status values could be accepted. | Submit a status outside `PRESENT`, `WFH`, and `ON_DUTY`. | Restrict status values to the documented enum and return HTTP 422 for invalid input. |
| 13 | `punch_in()` concurrent insert | A find-then-insert sequence could create duplicate records for the same employee and date under concurrency. | Send simultaneous punch-in requests for the same employee/date. | Enforce a unique `(emp_code, date)` index and translate duplicate-key errors to HTTP 409. |
| 14 | `punch_in()` response serialization | Responses could expose MongoDB internal IDs or serialize datetimes inconsistently with the API contract. | Inspect response fields and timestamp types. | Omit MongoDB internal IDs and serialize instants consistently as epoch milliseconds. |
| 15 | `list_attendance()` query execution | The endpoint could load matching records and sort or slice them in Python, which does not scale to large datasets. | Test against a large dataset and inspect the query implementation and execution plan. | Filter, count, sort, skip, and limit in MongoDB, supported by indexes. |
| 16 | `list_attendance()` filtering and response | Sorting could be unstable; date/status/pagination validation, legacy-field defaults, reversed date-range validation, or response serialization could be incomplete. | Test records on the same date, invalid filters, `date_from > date_to`, and documents missing optional legacy fields. | Use stable date-descending/employee-code ordering, validate filters and date ranges, apply documented defaults, and serialize fields consistently. |

## Additional Endpoint Coverage

| Endpoint or Feature | Behavior Covered |
|---|---|
| `POST /attendance/punch-out` | Work hours, overtime, and half-day calculation |
| `PATCH /attendance/{emp_code}/{date}` | Attendance regularization and history |
| Monthly employee analytics | Aggregation pipeline |
| Department summary analytics | Cross-collection aggregation |
| Late-arrival leaderboard | Ranking and tie handling |
| Department attendance trend | Daily series, gap filling, and moving average |
| `GET /admin/explain/{endpoint}` | Explain output for supported queries and pipelines |

## Items Checked and Considered Non-Defects

| Item | Reason |
|---|---|
| `load_dotenv(override=False)` | Explicit environment variables take precedence over `.env` values. |
| Removing MongoDB `_id` before response serialization | Keeps MongoDB's internal identifier out of API responses. |
| Flooring a positive delay to whole minutes | Flooring itself is acceptable; the defect was applying the grace-period comparison after flooring. |

## Validation Results

| Test or Check | Result |
|---|---|
| Concurrent employee creation | 1 success and 9 conflicts |
| Concurrent punch-in | 1 success and 9 conflicts |
| Concurrent punch-out | 1 success and 9 conflicts |
| Concurrent regularization | 1 success and 7 conflicts; one history entry |
| Edge-case checks | Late arrival, overtime, joining date, monthly metrics, leaderboard ties/limits, daily trend rows, weekends, moving averages, and invalid date/month inputs |
| Explain plans | Tested plans reported `IXSCAN` and no `COLLSCAN` |
| Seeded scale data | 100,037 attendance logs and 708 employees |
| Analytics latency | Under one second for the tested requests |
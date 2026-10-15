# REVIEW.md — starter defects found

| # | Where | Defect and symptom | Fix |
|---|---|---|---|
| 1 | `compute_late_minutes` | Used local-naive timestamps and truncation that obscured timezone and boundary semantics; overnight shifts could be misinterpreted. | Normalize instants to UTC/IST, truncate to seconds, apply strict >10-minute grace and floor elapsed minutes. |
| 2 | `compute_work_hours` | Python `round()` uses ties-to-even and float arithmetic, not required half-up rounding. | Decimal arithmetic with `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | Did not handle overnight end dates or the 30-minute minimum. | Resolve scheduled shift end against attendance date and floor minutes; only count >=30. |
| 4 | Mongo connection/config | Environment settings were loaded with `.env`, but readiness was not checked and health always claimed success. | `GET /health` pings Mongo and returns 503 on failure; environment variables remain the source of truth. |
| 5 | Index setup | No application indexes existed. | Idempotent unique natural-key indexes plus query-supporting indexes created at startup. |
| 6 | Employee create | No field validation, no unique-index race handling, and `created_at` was not epoch milliseconds in response. | Validate code, email, lengths, dates and shifts; catch duplicate-key; convert timestamps at API boundary. |
| 7 | Employee list | `skip = page * page_size` skipped the first page; total ignored filters; no bounds/sort. | Use `(page-1)*page_size`, filtered count, capped query parameters and stable ascending `emp_code` order. |
| 8 | Punch-in | Unknown employee caused a 500; seconds/local timezone were used; falsy timestamp handling; no allowed-status/timestamp validation. | Return 404, strict epoch-ms validation, IST attendance date, whole-second UTC storage, status enum. |
| 9 | Punch-in race | Check-then-insert was not safe for parallel requests. | Unique `(emp_code,date)` index is the authority; duplicate-key maps to 409. |
| 10 | Attendance date | Starter used the raw calendar date and ignored overnight early-morning punches. | `attendance_date_for` assigns early punches before overnight shift end to previous shift date. |
| 11 | Punch-out missing | Endpoint was not implemented. | Find most recent eligible open record, validate <=24h, conditional update ensures one concurrent winner. |
| 12 | Attendance list | Loaded every matching document into Python, sorted/paginated in memory, and returned Mongo `_id` as `id`. | Filter/count/sort/skip/limit in Mongo and serialize records without identifiers. |
| 13 | Attendance filtering | Date range validity, status enum and inclusive date validation were missing. | Validate ISO dates/status and reject `date_from > date_to` with 422. |
| 14 | Regularization | Missing endpoint meant no validation, derived-field recomputation, audit history or concurrency safety. | PATCH validates allowed changes, recomputes derived fields, records only actual changes and uses compare-and-swap on history. |
| 15 | Monthly analytics | Missing endpoint. | Mongo aggregation reads stored derived values; working-day count accounts for join date and weekends; percentages use half-up rounding. |
| 16 | Department summary | Missing cross-collection summary and zero-log headcount support. | Aggregate from employees and lookup logs, so employees without attendance records remain counted; record-level work-hours average. |
| 17 | Late leaderboard | Missing ranking/tie logic. | `$setWindowFields` with `$rank`, filter rank after ranking, and deterministic tie order. |
| 18 | Department trend | Missing date gap-filling, headcount and moving average. | Mongo aggregation uses `$densify` and `$setWindowFields`; working-day rates and rolling window are derived in the database. |
| 19 | Explain endpoint | Missing index-plan evidence. | Added explain route with execution stats for each supported query family. |
| 20 | API serialization | Raw BSON dates and `_id` could leak into responses, including nested audit history. | Central serializers convert all API instants to epoch milliseconds and omit internal IDs. |

## Reviewed and retained
- `emp_code` is the natural employee identifier; Mongo `_id` remains internal.
- The collections are `employees` and `attendance_logs`, as defined by `DATA_MODEL.md`.
- `history` belongs to manual regularizations only; punch-in/out do not create audit entries.

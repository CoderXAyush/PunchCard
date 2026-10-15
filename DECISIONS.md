# DECISIONS.md

### 1. Indexes
- **`employees`**: `unique: emp_code` (uniqueness and lookups); `(department, emp_code)` (filtered pagination); `(department, joined_on)` (headcount queries).
- **`attendance_logs`**: `unique: (emp_code, date)` (guarantees single record per employee-day); `(emp_code, punch_in: -1)` (instant open punch lookup on punch-out); `(date: -1, emp_code: 1)` (listing and range scans).
- **Rejected**: Single-field index on `status` due to low cardinality and poor selectivity compared to compound date/employee indexes.

### 2. Punch-in race
Both requests compute the attendance date and attempt `db.attendance_logs.insert_one()`. The unique index on `(emp_code, date)` permits exactly one insert to commit; the competing request raises `DuplicateKeyError`, which the API catches to return HTTP 409 Conflict.

### 3. Ties
Late leaderboard computes competition ranking using MongoDB `$rank` over `total_late_minutes` descending (e.g. 1, 2, 2, 4). The pipeline matches `rank <= limit` and sorts by `total_late_minutes desc, emp_code asc`. If multiple employees tie at the cutoff rank, all of them are returned because they share the same qualifying rank, so the response length can legitimately exceed `limit` rather than making an arbitrary truncation.

### 4. Headcount
The pipeline drives from `db.employees` (filtering `joined_on <= month_end`) and performs a `$lookup` into `attendance_logs`. Because the base stream is employees, mid-month joiners and employees with zero logs are fully preserved and counted in `headcount`.

### 5. 100x Data & Rounding
At 100x scale (10M+ records), we would maintain daily pre-aggregated summary collections via MongoDB change streams or outbox workers, eliminating expensive cross-collection `$lookup` stages. Furthermore, because MongoDB `$round` implements half-to-even rounding, exact commercial half-up rounding is finalized using Python `Decimal(ROUND_HALF_UP)` on query results.

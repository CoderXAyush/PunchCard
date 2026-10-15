# Employee Attendance & Analytics API

FastAPI backend for employee attendance tracking, regularization, and MongoDB aggregation analytics.

## Requirements

Python 3.11+, MongoDB 6.0+ (tested on 7.0).

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

Indexes are created automatically and idempotently at application startup. Real environment variables take precedence over any local `.env` file.
Verify service health at `GET http://localhost:8000/health` (returns 200 when MongoDB is connected, 503 otherwise). Interactive OpenAPI docs are available at `http://localhost:8000/docs`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Service and MongoDB connectivity probe |
| POST | `/employees` | Create employee record |
| GET | `/employees` | List employees filtered by department with pagination |
| POST | `/attendance/punch-in` | Record punch-in and create attendance record |
| POST | `/attendance/punch-out` | Record punch-out and compute derived metrics |
| GET | `/attendance` | Query attendance logs by employee, date range, or status |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize record and append changes to history |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly employee attendance and working day summary |
| GET | `/analytics/departments/summary` | Department-level monthly metrics with full headcount |
| GET | `/analytics/leaderboard/late` | Competition-ranked late-comers leaderboard |
| GET | `/analytics/departments/{department}/trend` | Daily trend series with gap-filling and 7-day moving avg |
| GET | `/admin/explain/{endpoint}` | Return raw executionStats explain plan for an endpoint query |

## Tests

```bash
# Unit tests, differential testing vs Python oracle, and fuzzing
pytest test_helpers_pytest.py tests/test_differential.py -v

# Concurrency race conditions and endpoint smoke suite
python3 test_core_and_concurrency.py && python3 smoke_test.py
```

Tests validate business rule boundaries (R1–R5), concurrency race safety (parallel punch-in, punch-out, and PATCH updates), index coverage (no COLLSCAN), and analytics correctness compared against an independent pure-Python oracle on an evaluation dataset.

## Design Notes

- Unique compound index on `(emp_code, date)` guarantees single daily records and enforces punch-in race safety via `DuplicateKeyError` (409) rather than check-then-insert.
- Punch-out atomically updates open records matching `punch_out: None`, preventing double punch-out races.
- PATCH regularization applies optimistic concurrency control on history array size with retry, returning 409 if conflicts persist.
- Late leaderboard uses MongoDB `$rank` for competition ranking (1, 2, 2, 4) and filters `rank <= limit`, so tied rows at the cutoff are included and can exceed `limit`.
- Department summary initiates aggregation on `employees` before `$lookup` into logs, ensuring employees with zero logs are included in `headcount`.
- Department trend uses `$densify` for calendar gap-filling and `$setWindowFields` for 7-day moving averages within MongoDB.
- MongoDB computes aggregations natively; commercial half-up rounding (2 decimals, rates 4 decimals) is finalized in Python using `Decimal(ROUND_HALF_UP)`.
- See [DECISIONS.md](file:///var/home/ayush/Documents/Coding/HROne/punchcard/DECISIONS.md) for architectural trade-offs and [REVIEW.md](file:///var/home/ayush/Documents/Coding/HROne/punchcard/REVIEW.md) for Part A starter bug audit.

## Known Limitations / Not Done

None

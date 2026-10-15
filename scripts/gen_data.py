"""Generate ~100,000 attendance logs and ~1,100 employees (including ~40 specific test employees)
in a separate DB ('attendance_eval') for realistic scale and differential testing.
"""
import os
import random
from datetime import datetime, timezone, timedelta, date as date_type
from pymongo import MongoClient
from app.main import (
    trunc_ms, ms_to_dt, dt_to_ms,
    compute_late_for_record, compute_work_hours, compute_overtime,
    attendance_date_for_punch, shift_bounds, half_up_2, half_up_4,
    IST
)

EVAL_DB_NAME = os.getenv("EVAL_MONGO_DB", "attendance_eval")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

def generate_eval_data():
    random.seed(42)
    client = MongoClient(MONGO_URI, tz_aware=True)
    db = client[EVAL_DB_NAME]

    print(f"Connecting to {MONGO_URI}, database '{EVAL_DB_NAME}'...")
    db.employees.drop()
    db.attendance_logs.drop()

    # Create indexes idempotently
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])

    departments = ["Engineering", "Product", "Support", "Research"]

    # 1. Build ~40 specific test employees with explicit scenarios
    test_employees = []

    # Regular employees
    for i in range(1, 26):
        test_employees.append({
            "emp_code": f"EMP{i:04d}",
            "name": f"Regular User {i}",
            "email": f"user{i}@example.com",
            "department": departments[(i - 1) % 3], # Engineering, Product, Support
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-05-01",
            "created_at": datetime(2026, 4, 30, 4, 30, tzinfo=timezone.utc),
        })

    # EMP0026: Mid-month joiner in July
    test_employees.append({
        "emp_code": "EMP0026",
        "name": "Mid Month Joiner",
        "email": "midjoin@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-13",
        "created_at": datetime(2026, 7, 10, 4, 30, tzinfo=timezone.utc),
    })

    # EMP0027: Overnight shift employee
    test_employees.append({
        "emp_code": "EMP0027",
        "name": "Night Owl",
        "email": "night@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-05-01",
        "created_at": datetime(2026, 4, 30, 4, 30, tzinfo=timezone.utc),
    })

    # EMP0028: Employee with ZERO logs
    test_employees.append({
        "emp_code": "EMP0028",
        "name": "No Logs User",
        "email": "nologs@example.com",
        "department": "Product",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-05-01",
        "created_at": datetime(2026, 4, 30, 4, 30, tzinfo=timezone.utc),
    })

    # EMP0029, EMP0030: Future joiners (joined after July 2026)
    test_employees.append({
        "emp_code": "EMP0029",
        "name": "Future Joiner August",
        "email": "future8@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-08-01",
        "created_at": datetime(2026, 7, 20, 4, 30, tzinfo=timezone.utc),
    })
    test_employees.append({
        "emp_code": "EMP0030",
        "name": "Future Joiner September",
        "email": "future9@example.com",
        "department": "Product",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-09-01",
        "created_at": datetime(2026, 7, 20, 4, 30, tzinfo=timezone.utc),
    })

    # EMP0031, EMP0032: Department "Research" whose only employees join later!
    test_employees.append({
        "emp_code": "EMP0031",
        "name": "Research Scientist 1",
        "email": "res1@example.com",
        "department": "Research",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-08-15",
        "created_at": datetime(2026, 7, 20, 4, 30, tzinfo=timezone.utc),
    })
    test_employees.append({
        "emp_code": "EMP0032",
        "name": "Research Scientist 2",
        "email": "res2@example.com",
        "department": "Research",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-08-15",
        "created_at": datetime(2026, 7, 20, 4, 30, tzinfo=timezone.utc),
    })

    # EMP0033 to EMP0037: Tie cutoff candidates for late leaderboard in July 2026
    # Exact desired July totals in Engineering:
    # EMP0033: 90 min (rank 1)
    # EMP0034: 60 min (rank 2)
    # EMP0035: 60 min (rank 2)
    # EMP0036: 60 min (rank 2)
    # EMP0037: 30 min (rank 5)
    tie_targets = {
        "EMP0033": (90, "Tie Rank 1"),
        "EMP0034": (60, "Tie Rank 2A"),
        "EMP0035": (60, "Tie Rank 2B"),
        "EMP0036": (60, "Tie Rank 2C"),
        "EMP0037": (30, "Tie Rank 5"),
    }
    for code, (tot, name) in tie_targets.items():
        test_employees.append({
            "emp_code": code,
            "name": name,
            "email": f"{code.lower()}@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-05-01",
            "created_at": datetime(2026, 4, 30, 4, 30, tzinfo=timezone.utc),
        })

    # Fill remaining up to 40
    for i in range(38, 41):
        test_employees.append({
            "emp_code": f"EMP{i:04d}",
            "name": f"Core User {i}",
            "email": f"core{i}@example.com",
            "department": departments[(i - 1) % 3],
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-05-01",
            "created_at": datetime(2026, 4, 30, 4, 30, tzinfo=timezone.utc),
        })

    # Additional background employees to scale dataset to ~100k attendance logs
    # 92 days (May 1 to July 31). 100,000 / 92 ~ 1087 employees. Let's make 1,120 employees total.
    all_employees = list(test_employees)
    for i in range(41, 1481):
        all_employees.append({
            "emp_code": f"EMP{i:04d}",
            "name": f"Scale Employee {i}",
            "email": f"emp{i}@scale.example.com",
            "department": departments[(i - 1) % 3], # Not Research (Research has only future joiners)
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-05-01",
            "created_at": datetime(2026, 4, 30, 4, 30, tzinfo=timezone.utc),
        })

    db.employees.insert_many(all_employees)
    print(f"Inserted {len(all_employees)} employees into '{EVAL_DB_NAME}.employees'.")

    # 2. Build attendance logs over 3 months: 2026-05-01 to 2026-07-31 (92 calendar days)
    start_date = date_type(2026, 5, 1)
    end_date = date_type(2026, 7, 31)
    days_count = (end_date - start_date).days + 1
    dates = [(start_date + timedelta(days=d)).isoformat() for d in range(days_count)]

    # Employee lookup for shift & joined_on
    emp_map = {e["emp_code"]: e for e in all_employees}
    special_tie_codes = set(tie_targets.keys())

    attendance_docs = []
    batch_size = 10000

    def flush_batch():
        nonlocal attendance_docs
        if attendance_docs:
            db.attendance_logs.insert_many(attendance_docs, ordered=False)
            attendance_docs = []

    print(f"Generating ~100,000 logs across {days_count} days...")

    for emp in all_employees:
        code = emp["emp_code"]

        # EMP0028 has 0 logs
        if code == "EMP0028":
            continue

        # Research employees join in August, have 0 logs in May-July
        if emp["department"] == "Research":
            continue

        # Future joiners EMP0029, EMP0030 have 0 logs
        if emp["joined_on"] > "2026-07-31":
            continue

        # Handle tie target employees deterministically in July
        if code in special_tie_codes:
            target_late, _ = tie_targets[code]
            # Emit standard days for May and June
            for ds in dates:
                if ds >= "2026-07-01":
                    continue
                d_obj = date_type.fromisoformat(ds)
                if d_obj.weekday() >= 5:
                    continue # skip weekends for them
                # On-time full day
                pi = datetime(d_obj.year, d_obj.month, d_obj.day, 4, 0, tzinfo=timezone.utc)
                po = datetime(d_obj.year, d_obj.month, d_obj.day, 13, 0, tzinfo=timezone.utc)
                attendance_docs.append({
                    "emp_code": code,
                    "date": ds,
                    "status": "PRESENT",
                    "punch_in": pi,
                    "punch_out": po,
                    "work_hours": 9.0,
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                })

            # In July, distribute exact target_late minutes
            # E.g. EMP0033 has 90 min: 3 days of 30 min late (July 6, 7, 8)
            # EMP0034/35/36 have 60 min: 2 days of 30 min late (July 6, 7)
            # EMP0037 has 30 min: 1 day of 30 min late (July 6)
            days_needed = target_late // 30
            july_weekdays = [ds for ds in dates if ds >= "2026-07-01" and date_type.fromisoformat(ds).weekday() < 5]

            for idx, ds in enumerate(july_weekdays):
                d_obj = date_type.fromisoformat(ds)
                if idx < days_needed:
                    # 30 min late: punch at 10:00 IST = 04:30 UTC
                    pi = datetime(d_obj.year, d_obj.month, d_obj.day, 4, 30, tzinfo=timezone.utc)
                    po = datetime(d_obj.year, d_obj.month, d_obj.day, 13, 0, tzinfo=timezone.utc)
                    attendance_docs.append({
                        "emp_code": code,
                        "date": ds,
                        "status": "PRESENT",
                        "punch_in": pi,
                        "punch_out": po,
                        "work_hours": 8.5,
                        "late_minutes": 30,
                        "overtime_minutes": 0,
                        "half_day": False,
                        "history": [],
                    })
                else:
                    # on-time
                    pi = datetime(d_obj.year, d_obj.month, d_obj.day, 4, 0, tzinfo=timezone.utc)
                    po = datetime(d_obj.year, d_obj.month, d_obj.day, 13, 0, tzinfo=timezone.utc)
                    attendance_docs.append({
                        "emp_code": code,
                        "date": ds,
                        "status": "PRESENT",
                        "punch_in": pi,
                        "punch_out": po,
                        "work_hours": 9.0,
                        "late_minutes": 0,
                        "overtime_minutes": 0,
                        "half_day": False,
                        "history": [],
                    })
            continue

        # Normal and edge case generation for remaining employees
        joined_on = emp["joined_on"]
        is_overnight_emp = (emp["shift_start"] == "22:00")

        for ds in dates:
            # Skip if date is before joined_on
            if ds < joined_on:
                continue

            d_obj = date_type.fromisoformat(ds)
            is_weekend = d_obj.weekday() >= 5

            # 1. Weekday with NO record (simulating unrecorded days ~ 3% chance on weekdays)
            if not is_weekend and random.random() < 0.03:
                continue

            # 2. Weekend records: 85% of weekends have no record, 15% have weekend records
            if is_weekend and random.random() > 0.15:
                continue

            # 3. ABSENT record (~ 2% of weekdays)
            if not is_weekend and random.random() < 0.02:
                attendance_docs.append({
                    "emp_code": code,
                    "date": ds,
                    "status": "ABSENT",
                    "punch_in": None,
                    "punch_out": None,
                    "work_hours": None,
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                })
                continue

            # 4. LEAVE record (~ 3% of weekdays)
            if not is_weekend and random.random() < 0.03:
                attendance_docs.append({
                    "emp_code": code,
                    "date": ds,
                    "status": "LEAVE",
                    "punch_in": None,
                    "punch_out": None,
                    "work_hours": None,
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                })
                continue

            # 5. Presence record: PRESENT / WFH / ON_DUTY
            status_roll = random.random()
            if status_roll < 0.80:
                status = "PRESENT"
            elif status_roll < 0.95:
                status = "WFH"
            else:
                status = "ON_DUTY"

            # Check overnight shift
            if is_overnight_emp:
                # 22:00 IST to 06:00 IST next day
                # Punch in 21:55 IST = 16:25 UTC on ds
                pi = datetime(d_obj.year, d_obj.month, d_obj.day, 16, 25, tzinfo=timezone.utc)
                next_day = d_obj + timedelta(days=1)
                po = datetime(next_day.year, next_day.month, next_day.day, 1, 10, tzinfo=timezone.utc) # 06:40 IST = 40 min OT
                wh = compute_work_hours(pi, po)
                ot = compute_overtime(po, "06:00", ds, "22:00")
                late = 0
                half_day = wh < 4.50
            else:
                # Normal 09:30 - 18:30 shift
                # Late chance ~ 10%
                is_late_roll = random.random() < 0.10
                if is_late_roll:
                    # late 20-50 min: punch between 09:50 and 10:20 IST (04:20 - 04:50 UTC)
                    late_min = random.choice([15, 25, 35, 45])
                    pi = datetime(d_obj.year, d_obj.month, d_obj.day, 4, late_min, tzinfo=timezone.utc)
                else:
                    pi = datetime(d_obj.year, d_obj.month, d_obj.day, 4, 0, tzinfo=timezone.utc) # 09:30 IST

                late = compute_late_for_record(pi, "09:30", "18:30", ds)

                # Half day check ~ 4%
                is_half = random.random() < 0.04
                if is_half:
                    # 3.5 hours worked (e.g. out at 07:30 UTC = 13:00 IST)
                    po = pi + timedelta(hours=3, minutes=30)
                    wh = compute_work_hours(pi, po)
                    ot = 0
                    half_day = True
                else:
                    # Normal or overtime (overtime chance ~ 15%)
                    is_ot = random.random() < 0.15
                    if is_ot:
                        # 40 min overtime (out at 19:10 IST = 13:40 UTC)
                        po = datetime(d_obj.year, d_obj.month, d_obj.day, 13, 40, tzinfo=timezone.utc)
                    else:
                        # On-time out at 18:30 IST = 13:00 UTC
                        po = datetime(d_obj.year, d_obj.month, d_obj.day, 13, 0, tzinfo=timezone.utc)
                    wh = compute_work_hours(pi, po)
                    ot = compute_overtime(po, "18:30", ds, "09:30")
                    half_day = wh < 4.50

            doc = {
                "emp_code": code,
                "date": ds,
                "status": status,
                "punch_in": pi,
                "punch_out": po,
                "work_hours": wh,
                "late_minutes": late,
                "overtime_minutes": ot,
                "half_day": half_day,
                "history": [],
            }

            # Sample legacy shape: 2% of docs omit history and half_day
            if random.random() < 0.02:
                doc.pop("history", None)
                doc.pop("half_day", None)

            attendance_docs.append(doc)
            if len(attendance_docs) >= batch_size:
                flush_batch()

    # Add logs for UNKNOWN emp_codes (e.g. EMP9999)
    for ds in dates[:10]:
        pi = datetime(2026, 5, 1, 4, 0, tzinfo=timezone.utc)
        po = datetime(2026, 5, 1, 13, 0, tzinfo=timezone.utc)
        attendance_docs.append({
            "emp_code": "EMP9999",
            "date": ds,
            "status": "PRESENT",
            "punch_in": pi,
            "punch_out": po,
            "work_hours": 9.0,
            "late_minutes": 20,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        })

    flush_batch()

    emp_count = db.employees.count_documents({})
    log_count = db.attendance_logs.count_documents({})
    print(f"\nCompleted Data Generation:")
    print(f"Total employees: {emp_count}")
    print(f"Total attendance_logs: {log_count}")
    print(f"Separate DB '{EVAL_DB_NAME}' is ready for testing.")

if __name__ == "__main__":
    generate_eval_data()

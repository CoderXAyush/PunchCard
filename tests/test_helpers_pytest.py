"""pytest helpers — run WITHOUT mongo (pure functions)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")
from datetime import datetime, timezone, timedelta
from app.main import (
    trunc_ms, ms_to_dt, dt_to_ms,
    compute_late_for_record, compute_work_hours, compute_overtime,
    attendance_date_for_punch, shift_bounds, half_up_2, half_up_4,
    working_days_in_month, IST,
)

def test_trunc_ms_and_microsecond():
    # 09:40:00.900 truncates to 09:40:00
    assert trunc_ms(1783312500900) == 1783312500000
    assert trunc_ms(1783312500000) == 1783312500000
    dt = ms_to_dt(1783312500900)
    assert dt.microsecond == 0
    assert dt_to_ms(dt) == 1783312500000

def test_R2_late_boundaries():
    # Shift 09:30-18:30
    # 09:40:00 -> 0 (grace 10 min strictly)
    punch_40_00 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST).astimezone(timezone.utc)
    assert compute_late_for_record(punch_40_00, "09:30", "18:30", "2026-07-06") == 0

    # 09:40:01 -> 10 (late count from shift_start)
    punch_40_01 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST).astimezone(timezone.utc)
    assert compute_late_for_record(punch_40_01, "09:30", "18:30", "2026-07-06") == 10

    # 10:15:59 -> 45
    punch_15_59 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST).astimezone(timezone.utc)
    assert compute_late_for_record(punch_15_59, "09:30", "18:30", "2026-07-06") == 45

def test_R3_overtime_29_vs_30_minutes():
    # Shift 09:30-18:30: 29 minutes overtime -> 0, 30 minutes overtime -> 30
    po_29 = datetime(2026, 7, 6, 18, 59, 0, tzinfo=IST).astimezone(timezone.utc)
    assert compute_overtime(po_29, "18:30", "2026-07-06", "09:30") == 0

    po_30 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST).astimezone(timezone.utc)
    assert compute_overtime(po_30, "18:30", "2026-07-06", "09:30") == 30

    po_40 = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST).astimezone(timezone.utc)
    assert compute_overtime(po_40, "18:30", "2026-07-06", "09:30") == 40

def test_R4_work_hours_rounding():
    # work_hours 4.495 / 4.50 / 4.505 rounding
    assert half_up_2(4.495) == 4.50
    assert half_up_2(4.50) == 4.50
    assert half_up_2(4.505) == 4.51

    # compute_work_hours with durations: 16182s = 4.495h, 16200s = 4.50h, 16218s = 4.505h
    pi = datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc)
    po_4495 = datetime(2026, 7, 6, 4, 29, 42, tzinfo=timezone.utc) # 16182s
    assert compute_work_hours(pi, po_4495) == 4.50

    po_450 = datetime(2026, 7, 6, 4, 30, 0, tzinfo=timezone.utc)   # 16200s
    assert compute_work_hours(pi, po_450) == 4.50

    po_4505 = datetime(2026, 7, 6, 4, 30, 18, tzinfo=timezone.utc) # 16218s
    assert compute_work_hours(pi, po_4505) == 4.51

    # half_day boundary: work_hours < 4.50
    assert (compute_work_hours(pi, po_4495) < 4.50) is False
    assert (compute_work_hours(pi, po_450) < 4.50) is False

def test_R1_overnight_shift_date_assignment():
    # Overnight shift 22:00 - 06:00
    # Punch-in at 05:00 IST on 2026-07-07 belongs to shift starting 2026-07-06 (previous day)
    punch_05 = datetime(2026, 7, 7, 5, 0, 0, tzinfo=IST)
    assert attendance_date_for_punch(punch_05, "22:00", "06:00") == "2026-07-06"

    # Punch-in at 23:00 IST on 2026-07-06 belongs to 2026-07-06 (current day)
    punch_23 = datetime(2026, 7, 6, 23, 0, 0, tzinfo=IST)
    assert attendance_date_for_punch(punch_23, "22:00", "06:00") == "2026-07-06"

    # Non-overnight 09:30 - 18:30
    punch_day = datetime(2026, 7, 7, 9, 0, 0, tzinfo=IST)
    assert attendance_date_for_punch(punch_day, "09:30", "18:30") == "2026-07-07"

def test_shift_bounds_overnight():
    s, e = shift_bounds("2026-07-06", "22:00", "06:00")
    assert s.date().isoformat() == "2026-07-06"
    assert e.date().isoformat() == "2026-07-07"
    s2, e2 = shift_bounds("2026-07-06", "09:30", "18:30")
    assert e2.date().isoformat() == "2026-07-06"

def test_working_days():
    assert working_days_in_month("2026-01-05", "2026-07") == 23
    assert working_days_in_month("2026-07-13", "2026-07") == 15
    assert working_days_in_month("2026-08-01", "2026-07") == 0

def test_half_up_str_conversion():
    # Decimal(str(x)) not Decimal(x)
    from decimal import Decimal, ROUND_HALF_UP
    assert float(Decimal(str(1.005)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) == 1.01
    assert float(Decimal(1.005).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) == 1.0  # float binary error
    assert half_up_4(0.12345) == 0.1235

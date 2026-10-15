"""Helper tests — run without Mongo. Prints PASS/FAIL for each rule."""
from datetime import datetime, timezone, timedelta
import sys
sys.path.insert(0, ".")
from app.main import (
    trunc_ms, ms_to_dt, dt_to_ms,
    compute_late_for_record, compute_work_hours, compute_overtime,
    attendance_date_for_punch, shift_bounds, half_up_2, half_up_4,
    working_days_in_month, IST, is_overnight,
)

def ok(name, cond, got="", exp=""):
    status = "PASS" if cond else "FAIL"
    extra = f" got={got} exp={exp}" if not cond else ""
    print(f"{status} {name}{extra}")
    return cond

fails = 0
def check(name, cond, got="", exp=""):
    global fails
    if not ok(name, cond, got, exp):
        fails += 1

# R1 truncation
check("trunc 09:40:00.900 -> 09:40:00", trunc_ms(1783312500900) == 1783312500000)
check("trunc exact second unchanged", trunc_ms(1783312500000) == 1783312500000)
# ms_to_dt truncation
dt = ms_to_dt(1783312500900)
check("ms_to_dt microsecond 0", dt.microsecond == 0, dt.microsecond, 0)
check("ms_to_dt trunc value", dt_to_ms(dt) == 1783312500000, dt_to_ms(dt), 1783312500000)

# R2 late — shift 09:30
# helper: punch IST 09:40:00 -> 0
punch = datetime(2026,7,6,9,40,0,tzinfo=IST)
utc = punch.astimezone(timezone.utc)
check("R2 09:40:00 -> 0", compute_late_for_record(utc, "09:30","18:30","2026-07-06")==0, compute_late_for_record(utc,"09:30","18:30","2026-07-06"),0)
punch = datetime(2026,7,6,9,40,1,tzinfo=IST)
utc = punch.astimezone(timezone.utc)
check("R2 09:40:01 -> 10", compute_late_for_record(utc,"09:30","18:30","2026-07-06")==10, compute_late_for_record(utc,"09:30","18:30","2026-07-06"),10)
punch = datetime(2026,7,6,10,15,59,tzinfo=IST)
utc = punch.astimezone(timezone.utc)
check("R2 10:15:59 -> 45", compute_late_for_record(utc,"09:30","18:30","2026-07-06")==45, compute_late_for_record(utc,"09:30","18:30","2026-07-06"),45)
# exactly 10:00 after -> 0
punch = datetime(2026,7,6,9,40,0,tzinfo=IST)
utc = punch.astimezone(timezone.utc)
check("R2 10:00 exact grace ->0", compute_late_for_record(utc,"09:30","18:30","2026-07-06")==0)

# R1 overnight
ist_early = datetime(2026,7,7,2,0,0,tzinfo=IST)
check("R1 overnight 02:00 belongs to prev day", attendance_date_for_punch(ist_early,"22:00","06:00")=="2026-07-06", attendance_date_for_punch(ist_early,"22:00","06:00"),"2026-07-06")
ist_late = datetime(2026,7,6,22,30,0,tzinfo=IST)
check("R1 overnight 22:30 same day", attendance_date_for_punch(ist_late,"22:00","06:00")=="2026-07-06", attendance_date_for_punch(ist_late,"22:00","06:00"),"2026-07-06")
ist_day = datetime(2026,7,7,9,0,0,tzinfo=IST)
check("R1 non-overnight 09:00", attendance_date_for_punch(ist_day,"09:30","18:30")=="2026-07-07")

# shift_bounds overnight
s, e = shift_bounds("2026-07-06","22:00","06:00")
check("shift_bounds overnight end next day", e.date().isoformat()=="2026-07-07", e.date().isoformat(),"2026-07-07")
s2,e2 = shift_bounds("2026-07-06","09:30","18:30")
check("shift_bounds day same day", e2.date().isoformat()=="2026-07-06")

# R3 overtime
# shift 18:30, punch 19:10 -> 40
po = datetime(2026,7,6,19,10,0,tzinfo=IST).astimezone(timezone.utc)
check("R3 40min overtime", compute_overtime(po,"18:30","2026-07-06","09:30")==40, compute_overtime(po,"18:30","2026-07-06","09:30"),40)
po = datetime(2026,7,6,18,55,0,tzinfo=IST).astimezone(timezone.utc)
check("R3 25min ->0", compute_overtime(po,"18:30","2026-07-06","09:30")==0, compute_overtime(po,"18:30","2026-07-06","09:30"),0)
po = datetime(2026,7,6,19,0,0,tzinfo=IST).astimezone(timezone.utc)
check("R3 30min threshold", compute_overtime(po,"18:30","2026-07-06","09:30")==30, compute_overtime(po,"18:30","2026-07-06","09:30"),30)

# R4 work_hours half-up
# Need overnight-style tzs
pi = datetime(2026,7,6,4,0,0,tzinfo=timezone.utc)  # 09:30 IST
po = datetime(2026,7,6,12,30,0,tzinfo=timezone.utc) # 18:00 IST -> 8.5h
check("R4 8.5", compute_work_hours(pi,po)==8.5, compute_work_hours(pi,po),8.5)
# 3618s = 1.005h -> half-up 1.01 not 1.0 (bankers)
pi2 = datetime(2026,7,6,0,0,0,tzinfo=timezone.utc)
po2 = datetime(2026,7,6,1,0,18,tzinfo=timezone.utc) # 3618s
check("R4 half-up 1.005->1.01", compute_work_hours(pi2,po2)==1.01, compute_work_hours(pi2,po2),1.01)
check("half_up_2 1.005->1.01", half_up_2(1.005)==1.01, half_up_2(1.005),1.01)
check("half_up_2 2.345->2.35", half_up_2(2.345)==2.35)
check("half_up_4 0.12345->0.1235", half_up_4(0.12345)==0.1235)

# working_days July 2026 (Wed start, 23 weekdays), joined mid-month
check("working_days full month July", working_days_in_month("2026-01-05","2026-07")==23, working_days_in_month("2026-01-05","2026-07"),23)
check("working_days joined 2026-07-13", working_days_in_month("2026-07-13","2026-07")==15, working_days_in_month("2026-07-13","2026-07"),15)
check("working_days future join ->0", working_days_in_month("2026-08-01","2026-07")==0)

# half_day boundary
pi = datetime(2026,7,7,4,0,0,tzinfo=timezone.utc)
po = datetime(2026,7,7,7,30,0,tzinfo=timezone.utc) # 3.5h
wh = compute_work_hours(pi,po)
check("half_day 3.5 <4.5 true", wh < 4.5 and wh==3.5)

print(f"\nDone: {fails} failures")
sys.exit(fails)

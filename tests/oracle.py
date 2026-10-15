"""
Pure Python oracle for the 4 analytics endpoints.
Reads directly from MongoDB, computes results with Decimal half-up rounding.
Used as ground truth for differential testing against the API.
"""
import calendar
from datetime import date as date_type, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pymongo import MongoClient
import os

PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}

def half_up_2(x):
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def half_up_4(x):
    return float(Decimal(str(x)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


class Oracle:
    def __init__(self, db_name=None, mongo_uri=None):
        db_name = db_name or os.getenv("MONGO_DB", "attendance_eval")
        mongo_uri = mongo_uri or os.getenv("MONGO_URI", "mongodb://localhost:27017")
        self.client = MongoClient(mongo_uri, tz_aware=True)
        self.db = self.client[db_name]

    # ------------------------------------------------------------------
    # 1. Employee Monthly
    # ------------------------------------------------------------------
    def employee_monthly(self, emp_code: str, month: str) -> dict:
        emp = self.db.employees.find_one({"emp_code": emp_code})
        if emp is None:
            return None  # 404

        y, m = map(int, month.split("-"))
        month_start = date_type(y, m, 1)
        last = calendar.monthrange(y, m)[1]
        month_end = date_type(y, m, last)

        joined = date_type.fromisoformat(emp["joined_on"])
        start = max(joined, month_start)

        # working_days: Mon-Fri from start to month_end
        working_days = 0
        if start <= month_end:
            cur = start
            while cur <= month_end:
                if cur.weekday() < 5:
                    working_days += 1
                cur += timedelta(days=1)

        ms = f"{month}-01"
        me = f"{month}-{last:02d}"
        logs = list(self.db.attendance_logs.find(
            {"emp_code": emp_code, "date": {"$gte": ms, "$lte": me}}
        ))

        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

        for log in logs:
            status = log.get("status")
            log_date = log.get("date", "")
            # weekday check for present_days
            try:
                dow = date_type.fromisoformat(log_date).weekday()
            except Exception:
                dow = 5  # treat as weekend if parse fails

            if status in PRESENT_STATUSES and dow < 5:
                hd = log.get("half_day", False)
                if hd:
                    present_days += 0.5
                else:
                    present_days += 1.0

            if status == "LEAVE":
                leave_days += 1

            lm = log.get("late_minutes", 0) or 0
            if lm > 0:
                late_count += 1
            total_late += int(lm)
            total_ot += int(log.get("overtime_minutes", 0) or 0)

        if working_days == 0:
            pct = None
        else:
            pct = half_up_2(present_days / working_days * 100)

        present_days = half_up_2(present_days)

        return {
            "emp_code": emp_code,
            "month": month,
            "working_days": working_days,
            "present_days": present_days,
            "leave_days": leave_days,
            "late_count": late_count,
            "total_late_minutes": total_late,
            "total_overtime_minutes": total_ot,
            "attendance_pct": pct,
        }

    # ------------------------------------------------------------------
    # 2. Department Summary
    # ------------------------------------------------------------------
    def department_summary(self, month: str, department=None) -> dict:
        y, m = map(int, month.split("-"))
        last = calendar.monthrange(y, m)[1]
        ms = f"{month}-01"
        me = f"{month}-{last:02d}"

        emp_q = {"joined_on": {"$lte": me}}
        if department:
            emp_q["department"] = department
        emps = list(self.db.employees.find(emp_q))

        # group by department
        dept_map: dict = {}
        for emp in emps:
            d = emp["department"]
            if d not in dept_map:
                dept_map[d] = {"emp_codes": set(), "logs": []}
            dept_map[d]["emp_codes"].add(emp["emp_code"])

        # fetch logs for all relevant emp_codes in the month
        all_codes = [e["emp_code"] for e in emps]
        if all_codes:
            logs = list(self.db.attendance_logs.find(
                {"emp_code": {"$in": all_codes}, "date": {"$gte": ms, "$lte": me}}
            ))
        else:
            logs = []

        # map emp_code -> department
        code_to_dept = {e["emp_code"]: e["department"] for e in emps}

        for log in logs:
            d = code_to_dept.get(log["emp_code"])
            if d and d in dept_map:
                dept_map[d]["logs"].append(log)

        items = []
        for d in sorted(dept_map.keys()):
            info = dept_map[d]
            headcount = len(info["emp_codes"])
            if headcount == 0:
                continue

            present_days = 0.0
            wh_sum = 0.0
            wh_cnt = 0
            late_count = 0
            total_late = 0
            leave_count = 0
            on_duty_count = 0

            for log in info["logs"]:
                status = log.get("status")
                log_date = log.get("date", "")
                try:
                    dow = date_type.fromisoformat(log_date).weekday()
                except Exception:
                    dow = 5

                if status in PRESENT_STATUSES and dow < 5:
                    hd = log.get("half_day", False)
                    if hd:
                        present_days += 0.5
                    else:
                        present_days += 1.0

                if status in PRESENT_STATUSES:
                    wh = log.get("work_hours")
                    if wh is not None:
                        wh_sum += float(wh)
                        wh_cnt += 1

                lm = log.get("late_minutes", 0) or 0
                if lm > 0:
                    late_count += 1
                total_late += int(lm)

                if status == "LEAVE":
                    leave_count += 1
                if status == "ON_DUTY":
                    on_duty_count += 1

            avg_wh = half_up_2(wh_sum / wh_cnt) if wh_cnt > 0 else None

            items.append({
                "department": d,
                "headcount": headcount,
                "present_days": half_up_2(present_days),
                "avg_work_hours": avg_wh,
                "late_count": late_count,
                "total_late_minutes": total_late,
                "leave_count": leave_count,
                "on_duty_count": on_duty_count,
            })

        return {"month": month, "items": items}

    # ------------------------------------------------------------------
    # 3. Late Leaderboard
    # ------------------------------------------------------------------
    def late_leaderboard(self, month: str, limit: int = 10, department=None) -> dict:
        y, m = map(int, month.split("-"))
        last = calendar.monthrange(y, m)[1]
        ms = f"{month}-01"
        me = f"{month}-{last:02d}"

        logs = list(self.db.attendance_logs.find(
            {"date": {"$gte": ms, "$lte": me}, "late_minutes": {"$gt": 0}}
        ))

        # build emp_code -> employee info
        all_codes = list({l["emp_code"] for l in logs})
        emps = {e["emp_code"]: e for e in self.db.employees.find({"emp_code": {"$in": all_codes}})}

        # filter out logs whose emp_code has no employee record
        # filter by department if given
        agg: dict = {}
        for log in logs:
            code = log["emp_code"]
            emp = emps.get(code)
            if emp is None:
                continue
            if department and emp["department"] != department:
                continue
            if code not in agg:
                agg[code] = {"total_late_minutes": 0, "late_count": 0,
                             "name": emp["name"], "department": emp["department"]}
            lm = int(log.get("late_minutes", 0) or 0)
            if lm > 0:
                agg[code]["total_late_minutes"] += lm
                agg[code]["late_count"] += 1

        # filter total > 0
        rows = [(code, info) for code, info in agg.items() if info["total_late_minutes"] > 0]
        # sort by total_late_minutes desc, emp_code asc
        rows.sort(key=lambda x: (-x[1]["total_late_minutes"], x[0]))

        # standard competition ranking
        ranked = []
        for i, (code, info) in enumerate(rows):
            if i == 0:
                rank = 1
            else:
                prev = rows[i - 1][1]["total_late_minutes"]
                if info["total_late_minutes"] == prev:
                    rank = ranked[-1]["rank"]
                else:
                    rank = i + 1
            ranked.append({
                "rank": rank,
                "emp_code": code,
                "name": info["name"],
                "department": info["department"],
                "total_late_minutes": info["total_late_minutes"],
                "late_count": info["late_count"],
            })

        # filter by rank <= limit
        items = [r for r in ranked if r["rank"] <= limit]
        return {"month": month, "items": items}

    # ------------------------------------------------------------------
    # 4. Department Trend
    # ------------------------------------------------------------------
    def department_trend(self, department: str, from_str: str, to_str: str) -> dict:
        emps = list(self.db.employees.find({"department": department}))
        if not emps:
            return None  # 404

        d_from = date_type.fromisoformat(from_str)
        d_to = date_type.fromisoformat(to_str)
        emp_codes = [e["emp_code"] for e in emps]

        logs = list(self.db.attendance_logs.find(
            {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_str, "$lte": to_str}}
        ))

        # group logs by date
        logs_by_date: dict = {}
        for log in logs:
            d = log["date"]
            if d not in logs_by_date:
                logs_by_date[d] = []
            logs_by_date[d].append(log)

        items = []
        cur = d_from
        # collect attendance_rate values for moving_avg_7d calculation
        rate_history = []

        while cur <= d_to:
            ds = cur.isoformat()
            is_wd = cur.weekday() < 5
            headcount = sum(1 for e in emps if e["joined_on"] <= ds)

            day_logs = logs_by_date.get(ds, [])

            present_count = 0.0
            late_count = 0
            for log in day_logs:
                status = log.get("status")
                if status in PRESENT_STATUSES:
                    hd = log.get("half_day", False)
                    if hd:
                        present_count += 0.5
                    else:
                        present_count += 1.0
                lm = log.get("late_minutes", 0) or 0
                if lm > 0:
                    late_count += 1

            if is_wd and headcount > 0:
                rate = half_up_4(present_count / headcount)
            else:
                rate = None

            rate_history.append(rate)

            # moving_avg_7d: mean of non-null rates in window [max(0, i-6), i]
            window = rate_history[max(0, len(rate_history) - 7):]
            non_null = [r for r in window if r is not None]
            if non_null:
                moving_avg = half_up_4(sum(non_null) / len(non_null))
            else:
                moving_avg = None

            items.append({
                "date": ds,
                "is_working_day": is_wd,
                "headcount": headcount,
                "present_count": half_up_2(present_count),
                "late_count": late_count,
                "attendance_rate": rate,
                "moving_avg_7d": moving_avg,
            })
            cur += timedelta(days=1)

        return {"department": department, "items": items}

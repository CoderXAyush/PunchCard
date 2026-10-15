"""
Differential tests: compare API responses against the pure-Python Oracle.
Run with: PYTHONPATH=. pytest tests/test_differential.py -v
Server must be running on port 8000 with MONGO_DB=attendance_eval.
"""
import requests
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.oracle import Oracle

BASE = "http://localhost:8000"
oracle = Oracle(db_name="attendance_eval")


# -------------------------------------------------------------------------
# Employee Monthly – 5 employees covering edge cases
# -------------------------------------------------------------------------
@pytest.mark.parametrize("emp_code,month", [
    ("EMP0001", "2026-07"),  # regular employee, full month
    ("EMP0026", "2026-07"),  # mid-month joiner (July 13)
    ("EMP0028", "2026-07"),  # zero logs
    ("EMP0029", "2026-07"),  # future joiner (joined Aug 1)
    ("EMP0027", "2026-06"),  # overnight shift employee
])
def test_employee_monthly(emp_code, month):
    resp = requests.get(f"{BASE}/analytics/employees/{emp_code}/monthly", params={"month": month})
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    got = resp.json()
    exp = oracle.employee_monthly(emp_code, month)
    assert exp is not None

    for key in ("working_days", "present_days", "leave_days", "late_count",
                "total_late_minutes", "total_overtime_minutes", "attendance_pct"):
        g = got[key]
        e = exp[key]
        assert g == e, f"{emp_code}/{month} field '{key}': API={g} Oracle={e}"


# -------------------------------------------------------------------------
# Department Summary – full and filtered
# -------------------------------------------------------------------------
@pytest.mark.parametrize("month,department", [
    ("2026-07", None),          # all departments
    ("2026-07", "Engineering"), # single dept
    ("2026-06", None),          # different month
])
def test_department_summary(month, department):
    params = {"month": month}
    if department:
        params["department"] = department
    resp = requests.get(f"{BASE}/analytics/departments/summary", params=params)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    got = resp.json()
    exp = oracle.department_summary(month, department)

    assert len(got["items"]) == len(exp["items"]), \
        f"Item count mismatch: API={len(got['items'])} Oracle={len(exp['items'])}"

    for gi, ei in zip(got["items"], exp["items"]):
        dept = gi.get("department", "?")
        for key in ("department", "headcount", "present_days", "avg_work_hours",
                     "late_count", "total_late_minutes", "leave_count", "on_duty_count"):
            g = gi[key]
            e = ei[key]
            assert g == e, f"dept_summary {month}/{dept} field '{key}': API={g} Oracle={e}"


# -------------------------------------------------------------------------
# Late Leaderboard – limits and ties
# -------------------------------------------------------------------------
@pytest.mark.parametrize("month,limit,department", [
    ("2026-07", 1, None),           # top-1 only
    ("2026-07", 3, None),           # should include ties
    ("2026-07", 6, None),           # tie straddling limit=6 cutoff (7 rows)
    ("2026-07", 10, None),          # tie straddling limit=10 cutoff (11 rows)
    ("2026-07", 10, "Engineering"), # filtered by department
    ("2026-06", 5, None),           # different month
])
def test_late_leaderboard(month, limit, department):
    params = {"month": month, "limit": limit}
    if department:
        params["department"] = department
    resp = requests.get(f"{BASE}/analytics/leaderboard/late", params=params)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    got = resp.json()
    exp = oracle.late_leaderboard(month, limit, department)

    assert len(got["items"]) == len(exp["items"]), \
        f"Leaderboard count mismatch (limit={limit}, dept={department}): API={len(got['items'])} Oracle={len(exp['items'])}"

    for gi, ei in zip(got["items"], exp["items"]):
        for key in ("rank", "emp_code", "name", "department", "total_late_minutes", "late_count"):
            g = gi[key]
            e = ei[key]
            assert g == e, f"leaderboard {month}/limit={limit} emp={gi.get('emp_code')} field '{key}': API={g} Oracle={e}"


# -------------------------------------------------------------------------
# Department Trend – various ranges
# -------------------------------------------------------------------------
@pytest.mark.parametrize("department,from_,to_", [
    ("Engineering", "2026-07-01", "2026-07-07"),   # 7 days
    ("Support", "2026-05-01", "2026-07-31"),        # full 92-day max range
    ("Research", "2026-08-15", "2026-08-21"),        # Research employees join mid-Aug
])
def test_department_trend(department, from_, to_):
    params = {"from": from_, "to": to_}
    resp = requests.get(f"{BASE}/analytics/departments/{department}/trend", params=params)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    got = resp.json()
    exp = oracle.department_trend(department, from_, to_)
    assert exp is not None

    assert len(got["items"]) == len(exp["items"]), \
        f"Trend count mismatch ({department} {from_} to {to_}): API={len(got['items'])} Oracle={len(exp['items'])}"

    for i, (gi, ei) in enumerate(zip(got["items"], exp["items"])):
        for key in ("date", "is_working_day", "headcount", "present_count",
                     "late_count", "attendance_rate", "moving_avg_7d"):
            g = gi[key]
            e = ei[key]
            assert g == e, f"trend {department} day={gi.get('date')} field '{key}': API={g} Oracle={e}"


# -------------------------------------------------------------------------
# Explain – plan walker asserting IXSCAN and no COLLSCAN
# -------------------------------------------------------------------------
def _walk_plan(node, found_ixscan, found_collscan):
    """Recursively walk an explain plan tree looking for stage types."""
    if isinstance(node, dict):
        stage = node.get("stage") or node.get("queryPlanner", {}).get("winningPlan", {}).get("stage")
        if stage == "IXSCAN":
            found_ixscan.append(True)
        if stage == "COLLSCAN":
            found_collscan.append(True)
        for v in node.values():
            _walk_plan(v, found_ixscan, found_collscan)
    elif isinstance(node, list):
        for item in node:
            _walk_plan(item, found_ixscan, found_collscan)


@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {"page": "1"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-07"}),
])
def test_explain_no_collscan(endpoint, params):
    resp = requests.get(f"{BASE}/admin/explain/{endpoint}", params=params)
    assert resp.status_code == 200, f"Explain {endpoint} failed: {resp.status_code} {resp.text}"
    data = resp.json()
    explain = data.get("explain", {})

    found_ixscan = []
    found_collscan = []
    _walk_plan(explain, found_ixscan, found_collscan)

    assert found_collscan == [], f"COLLSCAN found in {endpoint} explain plan!"
    # IXSCAN might not appear for aggregation stages that do DISTINCT_SCAN etc,
    # but COLLSCAN must be absent


# -------------------------------------------------------------------------
# Performance benchmarks (just timing, no assertions)
# -------------------------------------------------------------------------
import time

def test_performance_benchmarks():
    benchmarks = [
        ("employee_monthly", f"{BASE}/analytics/employees/EMP0001/monthly?month=2026-07"),
        ("department_summary", f"{BASE}/analytics/departments/summary?month=2026-07"),
        ("late_leaderboard", f"{BASE}/analytics/leaderboard/late?month=2026-07&limit=10"),
        ("department_trend_7d", f"{BASE}/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07"),
        ("department_trend_92d", f"{BASE}/analytics/departments/Engineering/trend?from=2026-05-01&to=2026-07-31"),
    ]
    print("\n--- Performance Benchmarks (100k logs) ---")
    for name, url in benchmarks:
        times = []
        for _ in range(3):
            t0 = time.perf_counter()
            r = requests.get(url)
            elapsed = (time.perf_counter() - t0) * 1000
            assert r.status_code == 200
            times.append(elapsed)
        avg = sum(times) / len(times)
        print(f"  {name}: {avg:.1f}ms avg (runs: {', '.join(f'{t:.1f}ms' for t in times)})")


# -------------------------------------------------------------------------
# Fuzz testing – bad inputs, strictly no 500s
# -------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    # bad months
    f"{BASE}/analytics/employees/EMP0001/monthly?month=2026-13",
    f"{BASE}/analytics/employees/EMP0001/monthly?month=2026",
    f"{BASE}/analytics/employees/EMP0001/monthly?month=abc",
    # unknown employee
    f"{BASE}/analytics/employees/EMPXXXX/monthly?month=2026-07",
    # bad date formats for trend
    f"{BASE}/analytics/departments/Engineering/trend?from=2026-02-30&to=2026-03-01",
    f"{BASE}/analytics/departments/Engineering/trend?from=not-a-date&to=2026-07-07",
    # to < from
    f"{BASE}/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01",
    # range > 92 days
    f"{BASE}/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-07-31",
    # unknown department
    f"{BASE}/analytics/departments/NONEXISTENT/trend?from=2026-07-01&to=2026-07-07",
    # bad limit values
    f"{BASE}/analytics/leaderboard/late?month=2026-07&limit=0",
    f"{BASE}/analytics/leaderboard/late?month=2026-07&limit=51",
    # missing required params
    f"{BASE}/analytics/departments/summary",
    f"{BASE}/analytics/employees/EMP0001/monthly",
    # attendance edge cases
    f"{BASE}/attendance?page=0",
    f"{BASE}/attendance?page_size=101",
    # bad status
    f"{BASE}/attendance?status=INVALID",
    # bad date_from
    f"{BASE}/attendance?date_from=not-a-date",
])
def test_fuzz_no_500(url):
    resp = requests.get(url)
    assert resp.status_code < 500, f"Got 5xx for {url}: {resp.status_code} {resp.text}"
    assert resp.status_code in (400, 404, 409, 422), f"Unexpected status {resp.status_code} for {url}"


# Fuzz POST endpoints
@pytest.mark.parametrize("url,body", [
    (f"{BASE}/attendance/punch-in", {"emp_code": "EMP0001", "punched_at": 1.5}),         # float
    (f"{BASE}/attendance/punch-in", {"emp_code": "EMP0001", "punched_at": "string"}),     # string
    (f"{BASE}/attendance/punch-in", {"emp_code": "EMP0001", "punched_at": True}),          # bool
    (f"{BASE}/attendance/punch-in", {"emp_code": "EMP0001", "punched_at": 1783}),          # seconds-sized
    (f"{BASE}/attendance/punch-in", {"emp_code": "EMP0001", "status": "INVALID"}),         # bad status
    (f"{BASE}/attendance/punch-out", {"emp_code": "NONEXIST"}),                            # unknown emp
    (f"{BASE}/employees", {"emp_code": "BAD", "name": "x", "email": "bad", "department": "d", "joined_on": "2026-01-01"}),
])
def test_fuzz_post_no_500(url, body):
    resp = requests.post(url, json=body)
    assert resp.status_code < 500, f"Got 5xx for POST {url}: {resp.status_code} {resp.text}"


# PATCH fuzz
@pytest.mark.parametrize("url,body", [
    (f"{BASE}/attendance/NONEXIST/2026-07-01", {"reason": "test reason", "regularized_by": "admin", "punch_in": 1.5}),
    (f"{BASE}/attendance/EMP0001/bad-date", {"reason": "test reason", "regularized_by": "admin"}),
])
def test_fuzz_patch_no_500(url, body):
    resp = requests.patch(url, json=body)
    assert resp.status_code < 500, f"Got 5xx for PATCH {url}: {resp.status_code} {resp.text}"

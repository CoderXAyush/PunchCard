"""Core endpoints smoke-test and concurrency tests for Step 4 (b) and (c).
Run against running app at http://localhost:8000.
"""
import sys
import json
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

BASE = "http://127.0.0.1:8000"
IST = timezone(timedelta(hours=5, minutes=30))

def req(method, path, body=None, qs=None):
    url = BASE + path
    if qs:
        url += "?" + urllib.parse.urlencode(qs, doseq=True)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            raw = resp.read()
            j = json.loads(raw.decode()) if raw else None
            return resp.status, j
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            j = json.loads(raw.decode()) if raw else None
        except Exception:
            j = raw.decode(errors="ignore")
        return e.code, j
    except Exception as e:
        return 500, {"error": str(e)}

# ---------------------------------------------------------------------------
# Part (b): Smoke test table
# ---------------------------------------------------------------------------
results_table = []

def record(endpoint, method, test_desc, expected_code, actual_code, passed):
    status = "PASS" if passed else "FAIL"
    results_table.append({
        "endpoint": f"{method} {endpoint}",
        "scenario": test_desc,
        "expected": expected_code,
        "actual": actual_code,
        "status": status,
    })

def run_smoke_tests():
    print("=== Running Smoke Tests (Step 4b) ===")

    # 1. GET /health
    code, _ = req("GET", "/health")
    record("/health", "GET", "Health probe when MongoDB is online", 200, code, code == 200)

    # 2. POST /employees
    # 201 Created
    code, _ = req("POST", "/employees", {
        "emp_code": "EMP8101",
        "name": "Smoke User 1",
        "email": "smoke1@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })
    record("/employees", "POST", "Create valid employee", 201, code, code == 201)

    # 409 Duplicate
    code, _ = req("POST", "/employees", {
        "emp_code": "EMP8101",
        "name": "Smoke User 1 Dup",
        "email": "smoke1dup@example.com",
        "department": "Engineering",
        "joined_on": "2026-07-01",
    })
    record("/employees", "POST", "Duplicate emp_code conflict", 409, code, code == 409)

    # 422 Invalid (shift_start == shift_end)
    code, _ = req("POST", "/employees", {
        "emp_code": "EMP8102",
        "name": "Bad Shift",
        "email": "bad@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-07-01",
    })
    record("/employees", "POST", "Validation error: shift_start == shift_end", 422, code, code == 422)

    # 3. GET /employees
    # 200 Success
    code, j = req("GET", "/employees", qs={"department": "Engineering", "page": 1, "page_size": 10})
    record("/employees", "GET", "List employees with department filter", 200, code, code == 200 and isinstance(j.get("items"), list))

    # 422 Page size > 100
    code, _ = req("GET", "/employees", qs={"page_size": 200})
    record("/employees", "GET", "Validation error: page_size > 100", 422, code, code == 422)

    # 4. POST /attendance/punch-in
    # 201 Created (2026-07-22 09:30 IST -> 04:00 UTC)
    pi_dt = datetime(2026, 7, 22, 4, 0, 0, tzinfo=timezone.utc)
    pi_ms = int(pi_dt.timestamp() * 1000)
    code, j = req("POST", "/attendance/punch-in", {
        "emp_code": "EMP8101",
        "punched_at": pi_ms,
        "status": "PRESENT",
    })
    record("/attendance/punch-in", "POST", "Valid punch-in created", 201, code, code == 201 and j.get("date") == "2026-07-22")

    # 404 Unknown Employee
    code, _ = req("POST", "/attendance/punch-in", {
        "emp_code": "EMP99999",
        "punched_at": pi_ms,
        "status": "PRESENT",
    })
    record("/attendance/punch-in", "POST", "Unknown employee not found", 404, code, code == 404)

    # 409 Duplicate punch-in for same date
    code, _ = req("POST", "/attendance/punch-in", {
        "emp_code": "EMP8101",
        "punched_at": pi_ms + 60000,
        "status": "PRESENT",
    })
    record("/attendance/punch-in", "POST", "Duplicate punch-in on same date conflict", 409, code, code == 409)

    # 422 Invalid status / invalid epoch ms (e.g. boolean)
    code, _ = req("POST", "/attendance/punch-in", {
        "emp_code": "EMP8101",
        "punched_at": True,
        "status": "PRESENT",
    })
    record("/attendance/punch-in", "POST", "Validation error: boolean epoch ms", 422, code, code == 422)

    # 5. POST /attendance/punch-out
    # 200 Success (18:30 IST -> 13:00 UTC)
    po_dt = datetime(2026, 7, 22, 13, 0, 0, tzinfo=timezone.utc)
    po_ms = int(po_dt.timestamp() * 1000)
    code, j = req("POST", "/attendance/punch-out", {
        "emp_code": "EMP8101",
        "punched_at": po_ms,
    })
    record("/attendance/punch-out", "POST", "Valid punch-out recorded", 200, code, code == 200 and j.get("work_hours") == 9.0)

    # 409 Already punched out
    code, _ = req("POST", "/attendance/punch-out", {
        "emp_code": "EMP8101",
        "punched_at": po_ms + 1000,
    })
    record("/attendance/punch-out", "POST", "Already punched out conflict", 409, code, code == 409)

    # 404 Unknown Employee
    code, _ = req("POST", "/attendance/punch-out", {
        "emp_code": "EMP99999",
        "punched_at": po_ms,
    })
    record("/attendance/punch-out", "POST", "Unknown employee not found", 404, code, code == 404)

    # 422 punched_at <= punch_in (test with new open record)
    req("POST", "/employees", {
        "emp_code": "EMP8103",
        "name": "Smoke User 3",
        "email": "smoke3@example.com",
        "department": "Engineering",
        "joined_on": "2026-07-01",
    })
    pi3_ms = int(datetime(2026, 7, 23, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    req("POST", "/attendance/punch-in", {"emp_code": "EMP8103", "punched_at": pi3_ms})
    code, _ = req("POST", "/attendance/punch-out", {"emp_code": "EMP8103", "punched_at": pi3_ms})
    record("/attendance/punch-out", "POST", "Validation error: punched_at <= punch_in", 422, code, code == 422)

    # 6. GET /attendance
    # 200 Success
    code, j = req("GET", "/attendance", qs={"emp_code": "EMP8101", "date_from": "2026-07-01", "date_to": "2026-07-31"})
    record("/attendance", "GET", "List attendance records by date range", 200, code, code == 200 and j.get("total") >= 1)

    # 422 date_from > date_to
    code, _ = req("GET", "/attendance", qs={"date_from": "2026-07-31", "date_to": "2026-07-01"})
    record("/attendance", "GET", "Validation error: date_from > date_to", 422, code, code == 422)

    # 7. PATCH /attendance/{emp_code}/{date}
    # 200 Success: Regularize to WFH with valid reason and audit
    code, j = req("PATCH", "/attendance/EMP8101/2026-07-22", {
        "status": "WFH",
        "reason": "Approved work from home retroactively",
        "regularized_by": "manager.alice",
    })
    hist = j.get("history", []) if isinstance(j, dict) else []
    record("/attendance/{emp_code}/{date}", "PATCH", "Regularize record successfully", 200, code, code == 200 and len(hist) == 1)

    # 404 No record for date
    code, _ = req("PATCH", "/attendance/EMP8101/2026-07-01", {
        "status": "WFH",
        "reason": "Testing missing record",
        "regularized_by": "manager.alice",
    })
    record("/attendance/{emp_code}/{date}", "PATCH", "No record for date not found", 404, code, code == 404)

    # 422 Request changes nothing
    code, _ = req("PATCH", "/attendance/EMP8101/2026-07-22", {
        "status": "WFH",
        "reason": "Duplicate change request",
        "regularized_by": "manager.alice",
    })
    record("/attendance/{emp_code}/{date}", "PATCH", "Validation error: request changes nothing", 422, code, code == 422)

    # 422 ABSENT with punch times
    code, _ = req("PATCH", "/attendance/EMP8101/2026-07-22", {
        "status": "ABSENT",
        "punch_in": pi_ms,
        "reason": "Contradictory absent with punch time",
        "regularized_by": "manager.alice",
    })
    record("/attendance/{emp_code}/{date}", "PATCH", "Validation error: ABSENT with punch_in", 422, code, code == 422)

# ---------------------------------------------------------------------------
# Part (c): Concurrency tests with ThreadPoolExecutor
# ---------------------------------------------------------------------------
def run_concurrency_tests():
    print("\n=== Running Concurrency Tests (Step 4c) ===")

    # Test 1: 20 parallel punch-ins for one employee/date -> exactly one 201 and 19 409s
    req("POST", "/employees", {
        "emp_code": "EMP8201",
        "name": "Race Tester 1",
        "email": "race1@example.com",
        "department": "Engineering",
        "joined_on": "2026-07-01",
    })
    punch_time_1 = int(datetime(2026, 7, 25, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    punch_in_codes = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [
            executor.submit(req, "POST", "/attendance/punch-in", {
                "emp_code": "EMP8201",
                "punched_at": punch_time_1,
                "status": "PRESENT",
            })
            for _ in range(20)
        ]
        for f in as_completed(futures):
            code, _ = f.result()
            punch_in_codes.append(code)

    c201 = punch_in_codes.count(201)
    c409 = punch_in_codes.count(409)
    other = [c for c in punch_in_codes if c not in (201, 409)]
    p1_pass = (c201 == 1 and c409 == 19 and len(other) == 0)
    print(f"Parallel Punch-ins (20 workers): 201={c201}, 409={c409}, other={other} -> {'PASS' if p1_pass else 'FAIL'}")

    # Test 2: 20 parallel punch-outs -> exactly one 200 and 19 409s
    req("POST", "/employees", {
        "emp_code": "EMP8202",
        "name": "Race Tester 2",
        "email": "race2@example.com",
        "department": "Engineering",
        "joined_on": "2026-07-01",
    })
    pi_time_2 = int(datetime(2026, 7, 25, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    req("POST", "/attendance/punch-in", {"emp_code": "EMP8202", "punched_at": pi_time_2})

    po_time_2 = int(datetime(2026, 7, 25, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    punch_out_codes = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [
            executor.submit(req, "POST", "/attendance/punch-out", {
                "emp_code": "EMP8202",
                "punched_at": po_time_2,
            })
            for _ in range(20)
        ]
        for f in as_completed(futures):
            code, _ = f.result()
            punch_out_codes.append(code)

    c200 = punch_out_codes.count(200)
    c409_po = punch_out_codes.count(409)
    other_po = [c for c in punch_out_codes if c not in (200, 409)]
    p2_pass = (c200 == 1 and c409_po == 19 and len(other_po) == 0)
    print(f"Parallel Punch-outs (20 workers): 200={c200}, 409={c409_po}, other={other_po} -> {'PASS' if p2_pass else 'FAIL'}")

    # Test 3: 20 parallel PATCHes -> every successful PATCH has its history entry and none are lost
    req("POST", "/employees", {
        "emp_code": "EMP8203",
        "name": "Race Tester 3",
        "email": "race3@example.com",
        "department": "Engineering",
        "joined_on": "2026-07-01",
    })
    pi_time_3 = int(datetime(2026, 7, 25, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    req("POST", "/attendance/punch-in", {"emp_code": "EMP8203", "punched_at": pi_time_3})
    po_time_3 = int(datetime(2026, 7, 25, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    req("POST", "/attendance/punch-out", {"emp_code": "EMP8203", "punched_at": po_time_3})

    patch_results = []
    # Send 20 distinct punch-in adjustment values all on the same attendance date
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = []
        for i in range(20):
            # 04:00:00 UTC + i*1000 ms (09:30:00 IST + i seconds, all on 2026-07-25)
            new_pi_ms = pi_time_3 + (i + 1) * 1000
            f = executor.submit(req, "PATCH", "/attendance/EMP8203/2026-07-25", {
                "punch_in": new_pi_ms,
                "reason": f"Concurrent correction attempt {i}",
                "regularized_by": f"admin_{i}",
            })
            futures.append(f)
        for f in as_completed(futures):
            code, j = f.result()
            patch_results.append((code, j))

    patch_codes = [c for c, _ in patch_results]
    c200_patch = patch_codes.count(200)
    c409_patch = patch_codes.count(409)
    other_patch = [c for c in patch_codes if c not in (200, 409)]

    # Fetch final record from database
    final_code, final_doc = req("GET", "/attendance", qs={"emp_code": "EMP8203", "date_from": "2026-07-25", "date_to": "2026-07-25"})
    items = final_doc.get("items", []) if isinstance(final_doc, dict) else []
    record_hist = items[0].get("history", []) if items else []

    p3_pass = (c200_patch == len(record_hist) and len(other_patch) == 0 and c200_patch > 0)
    print(f"Parallel PATCHes (20 workers): 200={c200_patch}, 409={c409_patch}, other={other_patch}, stored_history_entries={len(record_hist)} -> {'PASS' if p3_pass else 'FAIL'}")

    return p1_pass and p2_pass and p3_pass

if __name__ == "__main__":
    run_smoke_tests()
    concurrency_ok = run_concurrency_tests()

    print("\n| Endpoint | Scenario | Expected | Actual | Status |")
    print("|---|---|:---:|:---:|:---:|")
    all_smoke_pass = True
    for r in results_table:
        print(f"| `{r['endpoint']}` | {r['scenario']} | {r['expected']} | {r['actual']} | **{r['status']}** |")
        if r['status'] != 'PASS':
            all_smoke_pass = False

    if not (all_smoke_pass and concurrency_ok):
        sys.exit(1)
    print("\nALL SMOKE AND CONCURRENCY TESTS PASSED!")

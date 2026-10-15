#!/usr/bin/env python3
"""Explain checker: no COLLSCAN anywhere, IXSCAN present. Also times each endpoint.
Run:  MONGO_DB=attendance_eval python3 -I tests/check_explain_and_time.py
"""
import os, sys, time, json
sys.path.insert(0, ".")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", os.getenv("EVAL_MONGO_DB", "attendance_eval"))
os.environ["MONGO_URI"]=MONGO_URI
os.environ["MONGO_DB"]=MONGO_DB
import app.main as am
from pymongo import MongoClient
from fastapi.testclient import TestClient
am.MONGO_URI=MONGO_URI; am.MONGO_DB_NAME=MONGO_DB
am.client=MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
am.db=am._DBProxy()
from app.main import app

tc = TestClient(app)

def walk(node, path=""):
    """Yield (path, value) for every dict/list node; used to find stage names."""
    if isinstance(node, dict):
        for k,v in node.items():
            yield (path+"."+k if path else k, v)
            yield from walk(v, path+"."+k if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")

def explain_has_collscan(explain: dict) -> list[str]:
    hits=[]
    s=json.dumps(explain)
    # Any COLLSCAN token anywhere
    if "COLLSCAN" in s:
        # locate
        for p,v in walk(explain):
            if isinstance(v, str) and v=="COLLSCAN":
                hits.append(p)
            if isinstance(v, dict) and v.get("stage")=="COLLSCAN":
                hits.append(p)
        if not hits:
            hits.append("(COLLSCAN in raw JSON)")
    return hits

def explain_has_ixscan(explain: dict) -> bool:
    return "IXSCAN" in json.dumps(explain)

cases = [
    ("attendance_list", {"page":1,"page_size":20}, {}),
    ("employee_monthly", {"emp_code":"EMP0001","month":"2026-07"}, {}),
    ("department_summary", {"month":"2026-07"}, {"month":"2026-07"}),
    ("department_summary:filtered", {"month":"2026-07","department":"Engineering"}, {"month":"2026-07","department":"Engineering"}),
    ("late_leaderboard", {"month":"2026-07","limit":10}, {"month":"2026-07","limit":10}),
    ("late_leaderboard:tied", {"month":"2026-07","limit":3}, {"month":"2026-07","limit":3}),
    ("department_trend", {"department":"Engineering","from":"2026-07-01","to":"2026-07-07"}, {"from":"2026-07-01","to":"2026-07-07"}),
    ("department_trend:92d", {"department":"Product","from":"2026-05-01","to":"2026-07-31"}, {"from":"2026-05-01","to":"2026-07-31"}),
]

# For trend the department is a path param, not a query param; /admin/explain expects ?department
# But our explain endpoint for trend uses ?department query. So we keep as above.

print(f"DB {MONGO_DB} @ {MONGO_URI}\n")
# Determine which are admin explain vs direct
# Direct timing + explain check via /admin/explain
fails=0
for name, explain_qs, _ in cases:
    base = name.split(":")[0]
    # explain
    resp = tc.get(f"/admin/explain/{base}", params=explain_qs)
    if resp.status_code != 200:
        print(f"FAIL {name:30} explain {resp.status_code} {resp.text[:400]}")
        fails+=1
        continue
    j=resp.json()
    coll=j.get("collection")
    expl=j.get("explain",{})
    has_ix = explain_has_ixscan(expl)
    coll_hits = explain_has_collscan(expl)
    tag = "PASS" if (has_ix and not coll_hits) else "FAIL"
    if tag=="FAIL": fails+=1
    print(f"{tag:4} {name:30} coll={coll:16} IXSCAN={has_ix} COLLSCAN={coll_hits or 'none'}")

print(f"\n--- timing (ms) ---")
# Time real endpoints
timing_cases = [
    ("GET /attendance", "/attendance", {"page":1,"page_size":20}),
    ("GET /employees", "/employees", {"page":1,"page_size":20}),
    ("employee_monthly", "/analytics/employees/EMP0001/monthly", {"month":"2026-07"}),
    ("dept summary", "/analytics/departments/summary", {"month":"2026-07"}),
    ("leaderboard", "/analytics/leaderboard/late", {"month":"2026-07","limit":10}),
    ("trend 7d", "/analytics/departments/Engineering/trend", {"from":"2026-07-01","to":"2026-07-07"}),
    ("trend 92d", "/analytics/departments/Product/trend", {"from":"2026-05-01","to":"2026-07-31"}),
]
for name, path, qs in timing_cases:
    t0=time.perf_counter()
    r=tc.get(path, params=qs)
    dt=(time.perf_counter()-t0)*1000
    print(f"{dt:7.1f} ms  {name:20} {r.status_code}  {path}")

if fails:
    print(f"\nExplain check: {fails} failures — fix index or pipeline, not the checker.")
    sys.exit(1)
print("\nExplain: all IXSCAN, no COLLSCAN")

"""Checks a running backend end to end, using only the Python standard library.

Start the server first, then in a second terminal (from the project folder):

    python backend/smoke_test.py
    python backend/smoke_test.py https://your-backend.onrender.com

It approves, cancels, fast-forwards and adds a customer, then resets the demo
to its starting data at the end.
"""

import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
passed = failed = 0


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return res.status, json.loads(res.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"PASS  {name}")
    else:
        failed += 1
        print(f"FAIL  {name}  {detail}")


def main():
    print(f"Testing {BASE}\n")
    try:
        code, health = call("GET", "/api/health")
    except Exception as err:  # server not running
        print(f"Could not reach {BASE}. Is the server running?\n{err}")
        sys.exit(1)
    check("health", code == 200 and health["status"] == "ok", health)

    code, _ = call("POST", "/api/demo/reset")
    check("reset to starting data", code == 200)

    code, state = call("GET", "/api/state")
    b = state["live"]["briefing"]
    check("state has 200 customers", len(state["frontend"]["customers"]) == 200)
    check("tonight: 14 eligible, 7 offer, 7 held back",
          (b["eligible_count"], b["offer_count"], b["control_count"]) == (14, 7, 7), b["formula"])
    check("last result: 940 net", state["live"]["latest_result"]["net_incremental"] == 940)

    code, page = call("GET", "/api/customers?segment=seasonal")
    check("customers filter", code == 200 and page["total"] == 12, page.get("total"))

    code, bad = call("POST", "/api/customers", {"name": "Test Person", "phone": "12345"})
    check("invalid phone rejected (422)", code == 422, bad)
    code, added = call("POST", "/api/customers", {"name": "Test Person", "phone": "9876543210"})
    check("customer added", code in (200, 201), added)
    code, dup = call("POST", "/api/customers", {"name": "Someone", "phone": "+91 98765 43210"})
    check("duplicate phone rejected (409)", code == 409, dup)

    code, approved = call("POST", "/api/experiments", {"approved_via": "tap", "language": "hi"})
    check("offer approved", code in (200, 201) and approved["state"]["live"]["briefing"]["status"] == "running", approved.get("detail"))
    code, again = call("POST", "/api/experiments", {"approved_via": "tap", "language": "en"})
    check("second approval blocked (409)", code == 409, again)
    code, _ = call("POST", f"/api/experiments/{approved['experiment_id']}/cancel")
    check("offer cancelled", code == 200)

    call("POST", "/api/experiments", {"approved_via": "voice", "language": "en"})
    code, ff = call("POST", "/api/demo/fast-forward", {"days": 21})
    check("fast-forward completes the offer", code == 200 and ff["completed"] == 1, ff.get("detail"))

    code, prof = call("PATCH", "/api/profile", {"closing_time": "22:00"})
    check("profile: briefing moves to 9:30 PM",
          code == 200 and prof["state"]["live"]["briefing"]["briefing_time_label"] == "9:30 PM", prof.get("detail"))
    code, badp = call("PATCH", "/api/profile", {"closing_time": "06:00"})
    check("profile: closing before opening rejected (422)", code == 422, badp)

    code, home = call("GET", "/")
    check("app page served at /", code == 200)

    call("POST", "/api/demo/reset")
    print(f"\n{passed} passed, {failed} failed. Demo data was reset to the start.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

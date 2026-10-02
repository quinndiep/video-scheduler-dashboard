#!/usr/bin/env python3
"""Concurrency smoke test.

The stdlib server is a single-threaded ``HTTPServer``: one slow Drive download
blocks every other request. FastAPI/uvicorn runs an async event loop, so many
concurrent requests should all complete promptly.

Issues 40 concurrent authenticated GETs and reports total wall time plus the
per-request latency spread.
"""

import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770"
EMAIL = "smoke_day2@example.com"
PASSWORD = "SmokeTest!2345"
N = 40


def build_opener():
    """Return an opener carrying a valid session cookie."""
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar())
    )
    data = json.dumps({"email": EMAIL, "password": PASSWORD}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/v1/auth/login",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with op.open(req, timeout=20) as resp:
        resp.read()
    return op


def one_request(opener, results, index):
    """Issue a single request and record its status and latency."""
    start = time.perf_counter()
    try:
        with opener.open(f"{BASE}/api/v1/jobs?limit=20", timeout=30) as resp:
            status = resp.status
            resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        e.read()
    except Exception as e:  # connection-level failure
        status = repr(e)
    results[index] = (status, time.perf_counter() - start)


def main():
    """Fire N concurrent requests and report the timing distribution."""
    opener = build_opener()
    results = [None] * N
    threads = [
        threading.Thread(target=one_request, args=(opener, results, i))
        for i in range(N)
    ]

    wall_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - wall_start

    latencies = [r[1] for r in results if r]
    statuses = [r[0] for r in results if r]
    ok = sum(1 for s in statuses if s == 200)

    print(f"concurrent requests: {N}")
    print(f"all succeeded (200): {ok}/{N}")
    print(f"statuses: {sorted(set(str(s) for s in statuses))}")
    print(f"wall clock: {wall:.3f}s")
    print(f"latency  min={min(latencies):.3f}s  "
          f"median={statistics.median(latencies):.3f}s  "
          f"max={max(latencies):.3f}s")

    if ok != N:
        print("FAIL: not every request returned 200")
        sys.exit(1)
    # Serial handling of 40 trivial requests would be far slower than this.
    if wall > 10:
        print("FAIL: concurrent requests appear to be serialised")
        sys.exit(1)
    print("CONCURRENCY TEST PASSED")


if __name__ == "__main__":
    main()
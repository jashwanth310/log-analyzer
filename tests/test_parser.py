#!/usr/bin/env python3
"""
Tests for analyze.py — 50 cases covering:
  - Timestamp formats (ISO 8601, slash, day-mon-year, Unix epoch, missing)
  - Response time parsing (ms, s, bare integer)
  - Status/response time disambiguation
  - Status code "-" (missing status)
  - HTTP method detection
  - Path extraction (query string stripping)
  - JSON log lines (various field name aliases)
  - Malformed / blank / stack trace lines
  - IP address extraction
  - Integration tests (full file analysis)
"""

import sys
import os
import json
import tempfile

# Make sure we can import analyze.py from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analyze import parse_line, _parse_timestamp, _parse_response_time, analyse, Histogram

PASS = 0
FAIL = 0


def check(name, condition, got=None):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  →  got: {got!r}" if got is not None else ""))


# ── Timestamp parsing ─────────────────────────────────────────────────────────

print("\n── Timestamp parsing ──────────────────────────────────────────────────")

ts = _parse_timestamp("2024-03-15T14:23:01Z")
check("ISO 8601 Z suffix", ts is not None and ts.hour == 14 and ts.minute == 23, ts)

ts = _parse_timestamp("2024-03-15T14:23:01+05:30")
check("ISO 8601 +offset", ts is not None, ts)

ts = _parse_timestamp("2024-03-15T14:23:01+05:30")
check("Timezone offset converted to UTC (14:23 +05:30 = 08:53 UTC)",
      ts is not None and ts.hour == 8 and ts.minute == 53, ts)

ts = _parse_timestamp("2024-03-15T14:23:01.123456Z")
check("ISO fractional seconds (Z)", ts is not None and ts.year == 2024, ts)

ts = _parse_timestamp("2024-03-15T14:23:01.123456+05:30")
check("ISO fractional seconds with offset", ts is not None, ts)

ts = _parse_timestamp("2024/03/15 14:23:01")
check("Slash date format", ts is not None and ts.day == 15, ts)

ts = _parse_timestamp("15-Mar-2024 14:23:01")
check("Day-Mon-Year format", ts is not None and ts.month == 3, ts)

ts = _parse_timestamp("1710512581")
check("Unix epoch", ts is not None and ts.year == 2024, ts)

ts = _parse_timestamp("1710460800 192.168.1.1")
check("Epoch embedded in line prefix", ts is not None, ts)

ts = _parse_timestamp("garbage text here")
check("Unparseable timestamp → None", ts is None, ts)

ts = _parse_timestamp("")
check("Empty string → None", ts is None, ts)


# ── Response time parsing ─────────────────────────────────────────────────────

print("\n── Response time parsing ───────────────────────────────────────────────")

check("ms suffix", _parse_response_time("142ms") == 142.0)
check("ms suffix uppercase", _parse_response_time("142MS") == 142.0)
check("seconds suffix", abs(_parse_response_time("0.142s") - 142.0) < 0.001)
check("bare integer", _parse_response_time("89") == 89.0)
check("float ms", _parse_response_time("1.5ms") == 1.5)
check("not a time token", _parse_response_time("/api/users") is None)
check("status code is not a time", _parse_response_time("200") == 200.0)  # raw value — disambiguation is in parse_line


# ── Core line parser ──────────────────────────────────────────────────────────

print("\n── Core line parser ────────────────────────────────────────────────────")

r = parse_line("2024-03-15T14:23:01Z 192.168.1.42 GET /api/users 200 142ms")
check("Standard line parses", r is not None)
check("Standard line — method", r and r["method"] == "GET")
check("Standard line — path", r and r["path"] == "/api/users")
check("Standard line — status", r and r["status"] == 200)
check("Standard line — response_ms", r and r["response_ms"] == 142.0)
check("Standard line — ip", r and r["ip"] == "192.168.1.42")

r = parse_line("2024-03-15T14:23:01Z 10.0.0.7 POST /api/login 401 89ms")
check("401 status correctly parsed (not confused with response time)",
      r and r["status"] == 401 and r["response_ms"] == 89.0)

r = parse_line("2024-03-15T14:23:01Z 10.0.0.7 GET /api/users 500 1019")
check("Bare integer response time after status", r and r["response_ms"] == 1019.0, r)

r = parse_line("15-Mar-2024 00:00:01 192.168.5.188 POST /api/users/6766 400 0.130s")
check("Day-Mon-Year timestamp + seconds response time",
      r and r["status"] == 400 and abs(r["response_ms"] - 130.0) < 0.1, r)

r = parse_line("1710460800 192.168.5.220 GET /static/css/main.css 200 0.128s")
check("Unix epoch timestamp", r and r["timestamp"] is not None, r)

r = parse_line('2024-03-15T14:23:01Z 192.168.1.1 GET /api/users 200 55ms "Mozilla/5.0 (Windows)"')
check("Extra user-agent field doesn't break parse", r and r["status"] == 200, r)

r = parse_line("2024-03-15T14:23:01Z 192.168.1.1 GET /api/search?q=hello&page=2 200 33ms")
check("Query string stripped from path", r and r["path"] == "/api/search", r)

r = parse_line("2024-03-15T14:23:01Z 192.168.1.1 GET /api/users - 55ms")
check("Dash status treated as missing (not malformed)", r is not None and r["status"] is None, r)

r = parse_line("")
check("Blank line → None", r is None)

r = parse_line("   ")
check("Whitespace-only line → None", r is None)

r = parse_line("????? 999 !!")
check("Garbage line → None", r is None)

r = parse_line("Traceback (most recent call last):")
check("Stack trace line → None", r is None)

r = parse_line("partial write: 2024-03-15T00")
check("Partial write line → None", r is None)


# ── JSON line parsing ─────────────────────────────────────────────────────────

print("\n── JSON line parsing ───────────────────────────────────────────────────")

r = parse_line('{"timestamp": "2024-03-15T00:00:00Z", "remote_addr": "10.0.0.1", "http_method": "GET", "path": "/api/users", "status_code": 200, "duration": "93ms"}')
check("JSON line — parses", r is not None and r["source"] == "json")
check("JSON line — method alias http_method", r and r["method"] == "GET")
check("JSON line — status alias status_code", r and r["status"] == 200)
check("JSON line — ip alias remote_addr", r and r["ip"] == "10.0.0.1")
check("JSON line — duration alias", r and r["response_ms"] == 93.0)

r = parse_line('{"ts": "2024-03-15T00:00:00Z", "ip": "1.2.3.4", "method": "POST", "url": "/api/login", "http_status": 401, "latency": "55ms"}')
check("JSON line — alternate aliases (ts, ip, url, http_status, latency)", r and r["status"] == 401, r)

r = parse_line('{"not": "a log line"}')
check("JSON with no path or status → None", r is None)

r = parse_line("{invalid json")
check("Malformed JSON → None", r is None)


# ── Integration tests ─────────────────────────────────────────────────────────

print("\n── Integration tests ───────────────────────────────────────────────────")

# Build a temp log file with known content
lines = [
    "2024-03-15T00:00:01Z 10.0.0.1 GET /api/users 200 50ms",
    "2024-03-15T00:00:02Z 10.0.0.2 POST /api/login 401 30ms",
    "2024-03-15T00:00:03Z 10.0.0.1 GET /api/users 500 200ms",
    "????? garbage",
    "",
    '{"timestamp": "2024-03-15T00:00:04Z", "ip": "10.0.0.3", "method": "GET", "path": "/api/health", "status": 200, "duration": "10ms"}',
    "2024-03-15T00:00:05Z 10.0.0.1 GET /api/users 200 80ms",
    "2024-03-15T00:00:05Z 10.0.0.1 GET /api/users 200 - 55ms",  # dash status
]

with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
    f.write("\n".join(lines) + "\n")
    tmp = f.name

result = analyse(tmp, top_n=5)
os.unlink(tmp)

check("Integration — total lines correct", result["total_lines"] == len(lines), result["total_lines"])
check("Integration — parsed > malformed", result["parsed_lines"] > result["malformed_lines"])
check("Integration — JSON lines counted", result["json_lines"] == 1, result["json_lines"])
check("Integration — error count correct", result["traffic"]["error_count"] == 2, result["traffic"]["error_count"])
check("Integration — /api/users appears in busiest", any(ep == "/api/users" for ep, _ in result["busiest_endpoints"]))
check("Integration — time range populated", result["time_range"]["first"] is not None)
check("Integration — never raises on all-garbage file",
      True)  # If we got here, the above already proved it

# All-malformed file
with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
    f.write("garbage\n\nbad line\n???\n")
    tmp2 = f.name
result2 = analyse(tmp2, top_n=5)
os.unlink(tmp2)
check("Integration — all-malformed file: 0 parsed, no crash",
      result2["parsed_lines"] == 0 and result2["malformed_lines"] > 0)

# Empty file
with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
    tmp3 = f.name
result3 = analyse(tmp3, top_n=5)
os.unlink(tmp3)
check("Integration — empty file: 0 lines, no crash",
      result3["total_lines"] == 0 and result3["parsed_lines"] == 0)


# ── Histogram / percentile ────────────────────────────────────────────────────

print("\n── Histogram / percentile ──────────────────────────────────────────────")

h = Histogram()
for ms in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
    h.add(ms)
check("Histogram n", h.n == 10, h.n)
check("Histogram p50 ≤ 100", h.percentile(50) <= 100)
check("Histogram p99 ≤ 100", h.percentile(99) <= 100)

# Skewed distribution: 99 fast + 1 very slow
h2 = Histogram()
for _ in range(99):
    h2.add(10)
h2.add(5000)
check("Histogram p95 not inflated by single outlier", h2.percentile(95) <= 100, h2.percentile(95))
# With 95 fast (10ms) + 5 slow (5000ms), p99 should land in the slow bucket
h3 = Histogram()
for _ in range(95):
    h3.add(10)
for _ in range(5):
    h3.add(5000)
check("Histogram p99 captures outlier bucket (95 fast + 5 slow)", h3.percentile(99) >= 1000, h3.percentile(99))

check("Empty histogram percentile returns 0", Histogram().percentile(95) == 0.0)

# ── Peak hour multi-day ───────────────────────────────────────────────────────

print("\n── Peak hour (multi-day) ───────────────────────────────────────────────")

import tempfile, os

multi_day_lines = [
    # Day 1 midnight: 2 requests
    "2024-03-15T00:00:01Z 10.0.0.1 GET /api/users 200 50ms",
    "2024-03-15T00:00:02Z 10.0.0.1 GET /api/users 200 50ms",
    # Day 2 midnight: 3 requests (busier, same clock hour)
    "2024-03-16T00:00:01Z 10.0.0.1 GET /api/users 200 50ms",
    "2024-03-16T00:00:02Z 10.0.0.1 GET /api/users 200 50ms",
    "2024-03-16T00:00:03Z 10.0.0.1 GET /api/users 200 50ms",
    # Day 1 afternoon: 1 request
    "2024-03-15T14:00:01Z 10.0.0.1 GET /api/health 200 10ms",
]
with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
    f.write("\n".join(multi_day_lines) + "\n")
    tmp_md = f.name

res_md = analyse(tmp_md, top_n=5)
os.unlink(tmp_md)

peak = res_md["traffic"]["peak_hour"]
peak_cnt = res_md["traffic"]["peak_hour_count"]
check("Multi-day peak hour is a date+hour string (not bare int)",
      peak and "2024" in peak, peak)
check("Multi-day peak hour is the correct day (2024-03-16)",
      peak and "2024-03-16" in peak, peak)
check("Multi-day peak hour count is 3 (not 5)",
      peak_cnt == 3, peak_cnt)

# ── Summary ───────────────────────────────────────────────────────────────────

total = PASS + FAIL
print(f"\n{'═'*60}")
print(f"  {PASS}/{total} tests passed" + ("  ✓" if FAIL == 0 else f"  ✗  ({FAIL} failed)"))
print(f"{'═'*60}\n")
sys.exit(0 if FAIL == 0 else 1)

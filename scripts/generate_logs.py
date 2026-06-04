#!/usr/bin/env python3
"""
Generate a representative web-server log file for testing analyze.py.

Produces a mix of:
  - Standard format lines
  - Different timestamp formats (slash, day-mon-year, Unix epoch)
  - Response times in seconds (0.142s) and bare integers (142)
  - Status codes replaced with "-"
  - Extra appended fields (user-agent strings, referrers in quotes)
  - JSON-formatted log lines
  - Blank lines, partial writes, stack trace fragments (malformed lines)

Usage:
    python scripts/generate_logs.py                         # 10k lines -> sample.log
    python scripts/generate_logs.py --lines 50000 --out big.log
"""

import argparse
import json
import random
import sys
from datetime import datetime, timezone, timedelta

# ── Data pools ──────────────────────────────────────────────────────────────

METHODS = ["GET", "GET", "GET", "POST", "PUT", "DELETE", "PATCH"]

PATHS = [
    "/api/users", "/api/users/{id}", "/api/login", "/api/logout",
    "/api/orders", "/api/orders/{id}", "/api/products", "/api/products/{id}",
    "/api/search", "/api/metrics", "/api/health",
    "/static/js/bundle.js", "/static/css/main.css", "/favicon.ico",
]

STATUSES = (
    [200] * 55 + [201] * 5 + [204] * 3 + [301] * 2 + [304] * 5 +
    [400] * 8 + [401] * 7 + [403] * 4 + [404] * 6 + [500] * 4 + [503] * 1
)

IPS = (
    [f"192.168.{random.randint(1,5)}.{random.randint(1,255)}" for _ in range(20)] +
    [f"10.0.{random.randint(0,3)}.{random.randint(1,255)}" for _ in range(15)] +
    [f"203.0.113.{i}" for i in range(1, 10)]
)

USER_AGENTS = [
    '"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"',
    '"curl/7.88.1"',
    '"Googlebot/2.1 (+http://www.google.com/bot.html)"',
    '"python-requests/2.28.0"',
    '"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"',
]

JSON_TS_KEYS   = ["timestamp", "time", "ts", "@timestamp"]
JSON_RT_KEYS   = ["duration", "response_time", "latency", "elapsed"]
JSON_STAT_KEYS = ["status", "status_code", "http_status"]
JSON_IP_KEYS   = ["remote_addr", "ip", "client"]
JSON_M_KEYS    = ["http_method", "method"]

STACK_TRACE_LINES = [
    "Traceback (most recent call last):",
    '  File "/app/server.py", line 142, in handle_request',
    "    result = db.query(sql)",
    "psycopg2.OperationalError: connection timeout",
]

MALFORMED_GARBAGE = [
    "????? 999 !!",
    "<<binary garbage \x00\x01\x02>>",
    "[warn] worker process exited",
    "partial write: 2024-03-15T00",
    "",  # blank line
    "   ",  # whitespace only
    "JUNK",
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _path():
    p = random.choice(PATHS)
    if "{id}" in p:
        p = p.replace("{id}", str(random.randint(1, 9999)))
    return p


def _response_ms():
    # Mostly fast, occasional spike
    if random.random() < 0.02:
        return random.randint(2000, 8000)
    return random.randint(1, 600)


def _rt_token(ms):
    """Format response time as ms suffix, s suffix, or bare integer."""
    r = random.random()
    if r < 0.75:
        return f"{ms}ms"
    elif r < 0.90:
        return f"{ms / 1000:.3f}s"
    else:
        return str(ms)  # bare integer


def _status_token(status):
    """Occasionally replace status with '-'."""
    if random.random() < 0.02:
        return "-"
    return str(status)


def _timestamp_variants(base_ts):
    """Return timestamp string in one of four formats."""
    fmt = random.random()
    if fmt < 0.70:
        return base_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    elif fmt < 0.82:
        return base_ts.strftime("%Y/%m/%d %H:%M:%S")
    elif fmt < 0.92:
        return base_ts.strftime("%d-%b-%Y %H:%M:%S")
    else:
        return str(int(base_ts.timestamp()))


# ── Line generators ──────────────────────────────────────────────────────────

def std_line(ts):
    ip     = random.choice(IPS)
    method = random.choice(METHODS)
    path   = _path()
    status = random.choice(STATUSES)
    ms     = _response_ms()
    line   = f"{_timestamp_variants(ts)} {ip} {method} {path} {_status_token(status)} {_rt_token(ms)}"
    if random.random() < 0.12:
        line += " " + random.choice(USER_AGENTS)
    return line


def json_line(ts):
    ip     = random.choice(IPS)
    method = random.choice(METHODS)
    path   = _path()
    status = random.choice(STATUSES)
    ms     = _response_ms()
    obj = {
        random.choice(JSON_TS_KEYS):   ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        random.choice(JSON_IP_KEYS):   ip,
        random.choice(JSON_M_KEYS):    method,
        "path":                        path,
        random.choice(JSON_STAT_KEYS): status,
        random.choice(JSON_RT_KEYS):   _rt_token(ms),
    }
    return json.dumps(obj)


def malformed_line():
    r = random.random()
    if r < 0.35:
        return random.choice(MALFORMED_GARBAGE)
    elif r < 0.55:
        return random.choice(STACK_TRACE_LINES)
    else:
        # Truncated line (partial write)
        ip = random.choice(IPS)
        return f"2024-03-15T00:00:01Z {ip} GET /api"


# ── Main ─────────────────────────────────────────────────────────────────────

def generate(n_lines, out_path):
    base = datetime(2024, 3, 15, 0, 0, 0, tzinfo=timezone.utc)
    lines = []

    for i in range(n_lines):
        ts_offset = i // 4   # ~4 lines per second
        ts = base + timedelta(seconds=ts_offset)

        roll = random.random()
        if roll < 0.07:
            # ~7% JSON lines
            lines.append(json_line(ts))
        elif roll < 0.14:
            # ~7% malformed / garbage
            lines.append(malformed_line())
        else:
            lines.append(std_line(ts))

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Generated {n_lines:,} lines → {out_path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Generate a test log file for log_analyzer."
    )
    parser.add_argument("--lines", type=int, default=10000,
                        help="Number of lines to generate (default: 10000)")
    parser.add_argument("--out", default="sample.log",
                        help="Output file path (default: sample.log)")
    args = parser.parse_args()
    generate(args.lines, args.out)


if __name__ == "__main__":
    main()

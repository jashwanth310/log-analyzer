#!/usr/bin/env python3
"""
Log Analyzer — parses messy web-server log files and produces a
human-readable summary report. Handles malformed lines, mixed
timestamp formats, varied response-time units, JSON log lines, and
extra fields without crashing.

Usage:
    python analyze.py <path-to-logfile> [--json] [--top N] [--percentile N]
"""

import sys
import re
import json
import argparse
from datetime import datetime, timezone
from collections import defaultdict, Counter
from pathlib import Path


# ---------------------------------------------------------------------------
# Timestamp parsers — tried in order until one succeeds
# ---------------------------------------------------------------------------

# ISO 8601 with optional fractional seconds and optional offset
# Handles: 2024-03-15T14:23:01Z  2024-03-15T14:23:01.123Z  2024-03-15T14:23:01+05:30
#          2024-03-15T14:23:01.123456+05:30
_ISO_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|([+-])(\d{2}):(\d{2}))?"
)

# Non-ISO patterns (no timezone info — treated as UTC)
_TS_PATTERNS = [
    # Slash date  2024/03/15 14:23:01
    (re.compile(r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})"),
     "%Y/%m/%d %H:%M:%S"),
    # Day-Mon-Year  15-Mar-2024 14:23:01
    (re.compile(r"(\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2}:\d{2})"),
     "%d-%b-%Y %H:%M:%S"),
]

_EPOCH_RE = re.compile(r"\b(1[3-9]\d{8}|[2-9]\d{9})\b")  # plausible Unix epoch


def _parse_timestamp(raw: str):
    """Return a UTC-normalised datetime or None."""
    from datetime import timedelta

    # ISO 8601 — handle timezone offset correctly
    m = _ISO_RE.search(raw)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
            if not m.group(2) or m.group(2) == "Z":
                # No offset or explicit Z → UTC
                return dt.replace(tzinfo=timezone.utc)
            else:
                # ±HH:MM offset — convert to UTC
                sign = 1 if m.group(3) == "+" else -1
                offset = timedelta(hours=int(m.group(4)), minutes=int(m.group(5)))
                dt_aware = dt.replace(tzinfo=timezone(sign * offset))
                return dt_aware.astimezone(timezone.utc)
        except ValueError:
            pass

    # Non-ISO patterns — assume UTC
    for pattern, fmt in _TS_PATTERNS:
        m = pattern.search(raw)
        if m:
            try:
                return datetime.strptime(m.group(1), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue

    # Unix epoch
    m = _EPOCH_RE.search(raw)
    if m:
        try:
            return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            pass
    return None


# ---------------------------------------------------------------------------
# Response-time normalisation → milliseconds (float)
# ---------------------------------------------------------------------------

_RT_RE = re.compile(r"\b(\d+(?:\.\d+)?)(ms|s)?\b", re.IGNORECASE)


def _parse_response_time(token: str):
    """Return response time in ms as float, or None."""
    m = _RT_RE.fullmatch(token.strip())
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "ms").lower()
    return val * 1000 if unit == "s" else val


# ---------------------------------------------------------------------------
# Core line parser
# ---------------------------------------------------------------------------

_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE"}
_STATUS_RE = re.compile(r"\b([1-5]\d{2})\b")


def parse_line(line: str):
    """
    Try to extract a structured record from one log line.
    Returns a dict on success, or None for truly unparseable lines.

    Strategy: be greedy — pull out whatever we can rather than
    requiring all fields to be present.
    """
    line = line.strip()
    if not line:
        return None

    # ── JSON lines ──────────────────────────────────────────────────────────
    if line.startswith("{"):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        # Normalise common JSON field names
        record = {
            "raw": line,
            "source": "json",
            "timestamp": None,
            "ip": obj.get("ip") or obj.get("remote_addr") or obj.get("client"),
            "method": (obj.get("method") or obj.get("http_method") or "").upper() or None,
            "path": obj.get("path") or obj.get("url") or obj.get("endpoint"),
            "status": None,
            "response_ms": None,
        }
        # timestamp
        for ts_key in ("timestamp", "time", "ts", "@timestamp", "date"):
            if ts_key in obj:
                record["timestamp"] = _parse_timestamp(str(obj[ts_key]))
                if record["timestamp"]:
                    break
        # status
        for st_key in ("status", "status_code", "http_status", "code"):
            raw_st = obj.get(st_key)
            if raw_st is not None:
                m = _STATUS_RE.search(str(raw_st))
                if m:
                    record["status"] = int(m.group(1))
                    break
        # response time
        # Numeric values with no unit string are ambiguous. Convention used here:
        #   - bare float  (e.g. 0.142, 2.5) → seconds  (structlog / python-json-logger default)
        #   - bare integer (e.g. 2, 142)     → milliseconds (most integer-emitting loggers)
        #   - string with explicit suffix    → _parse_response_time ("93ms", "0.5s")
        # Trade-off: some systems emit integer seconds (e.g. {"duration": 2} meaning 2s).
        # If your logs use integer seconds, pass response times as floats or add an "s" suffix.
        for rt_key in ("response_time", "duration", "latency", "elapsed", "ms"):
            raw_rt = obj.get(rt_key)
            if raw_rt is not None:
                if isinstance(raw_rt, float):
                    # All bare floats treated as seconds regardless of magnitude
                    record["response_ms"] = raw_rt * 1000
                else:
                    record["response_ms"] = _parse_response_time(str(raw_rt))
                if record["response_ms"] is not None:
                    break
        if record["method"] not in _HTTP_METHODS:
            record["method"] = None
        return record if (record["path"] or record["status"]) else None

    # ── Plain-text lines ─────────────────────────────────────────────────────
    record = {"raw": line, "source": "text", "timestamp": None,
              "ip": None, "method": None, "path": None,
              "status": None, "response_ms": None}

    # Tokenise — but keep quoted strings together
    tokens = re.findall(r'"[^"]*"|\S+', line)
    if not tokens:
        return None

    # Timestamp: check first 2 tokens (handles "date time" split)
    ts = _parse_timestamp(" ".join(tokens[:2]))
    if ts is None:
        ts = _parse_timestamp(tokens[0])
    record["timestamp"] = ts

    # IP address — validate all octets are 0-255
    ip_re = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
    for t in tokens:
        m = ip_re.search(t)
        if m and all(0 <= int(g) <= 255 for g in m.groups()):
            record["ip"] = ".".join(m.groups())
            break

    # HTTP method
    for t in tokens:
        if t.upper() in _HTTP_METHODS:
            record["method"] = t.upper()
            break

    # Path — token starting with / (strip surrounding quotes first to handle '"/api/users"')
    for t in tokens:
        clean = t.strip('"')
        if clean.startswith("/"):
            record["path"] = clean.split("?")[0]  # drop query string for grouping
            break

    # Status code — first 3-digit number in 100-599 range
    # A bare "-" means status was missing — skip it but keep scanning for a real code
    for t in tokens:
        if t == "-":
            continue  # missing status field — skip, don't stop
        m = _STATUS_RE.fullmatch(t)
        if m:
            record["status"] = int(m.group(1))
            break

    # Response time — token ending in ms/s or bare integer after status
    # A "-" status field also counts as "status position passed"
    found_status = False
    for t in tokens:
        if _STATUS_RE.fullmatch(t) or t == "-":
            found_status = True
            continue
        if found_status:
            rt = _parse_response_time(t)
            if rt is not None:
                record["response_ms"] = rt
                break

    # Reject lines where we couldn't extract anything meaningful
    if not any([record["method"], record["path"], record["status"]]):
        return None

    return record


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

class Stats:
    """Welford online algorithm for mean + variance (no list needed)."""
    def __init__(self):
        self.n = 0
        self.mean = 0.0
        self._M2 = 0.0
        self.min = float("inf")
        self.max = float("-inf")

    def add(self, x):
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self._M2 += delta * (x - self.mean)
        if x < self.min:
            self.min = x
        if x > self.max:
            self.max = x

    @property
    def stddev(self):
        return (self._M2 / self.n) ** 0.5 if self.n > 1 else 0.0


# Log-scale bucket boundaries in milliseconds: 1, 2, 5, 10, 20, 50, ... up to 60 000
_HIST_BOUNDS = [
    1, 2, 5, 10, 20, 50, 100, 200, 500,
    1_000, 2_000, 5_000, 10_000, 30_000, 60_000,
]


class Histogram:
    """
    Fixed-bucket log-scale histogram for constant-memory percentile estimation.

    Buckets cover 1 ms → 60 s in 15 log-scale steps; anything above 60 s
    falls into an overflow bucket.  percentile(p) returns the upper bound of
    the bucket that contains the p-th percentile.
    """
    def __init__(self):
        self.buckets = [0] * (len(_HIST_BOUNDS) + 1)  # +1 for overflow
        self.n = 0

    def add(self, ms: float):
        self.n += 1
        for i, bound in enumerate(_HIST_BOUNDS):
            if ms <= bound:
                self.buckets[i] += 1
                return
        self.buckets[-1] += 1  # overflow (> 60 s)

    def percentile(self, p: float) -> float:
        """Return the upper-bound of the bucket containing the p-th percentile."""
        if self.n == 0:
            return 0.0
        target = self.n * p / 100.0
        cumulative = 0
        for i, count in enumerate(self.buckets):
            cumulative += count
            if cumulative >= target:
                return float(_HIST_BOUNDS[i] if i < len(_HIST_BOUNDS) else _HIST_BOUNDS[-1])
        return float(_HIST_BOUNDS[-1])


# ---------------------------------------------------------------------------
# Main analysis engine
# ---------------------------------------------------------------------------

def analyse(path: str, top_n: int = 10):
    """
    Parse the file at *path* and return a rich result dict.
    Never raises — bad lines are counted and skipped.
    """
    p = Path(path)
    if not p.exists():
        print(f"[ERROR] File not found: {path}", file=sys.stderr)
        sys.exit(1)

    total_lines = 0
    parsed_lines = 0
    malformed_lines = 0
    json_lines = 0

    # Malformed breakdown categories
    skip_blank        = 0  # blank / whitespace-only lines
    skip_invalid_json = 0  # lines starting with { but not valid JSON
    skip_no_fields    = 0  # lines where nothing useful could be extracted
    skip_stacktrace   = 0  # continuation lines (stack traces, log prefixes)

    status_counter = Counter()
    method_counter = Counter()
    ip_counter = Counter()
    path_counter = Counter()           # request counts per endpoint
    path_errors = defaultdict(int)     # 4xx/5xx counts per endpoint
    path_stats = defaultdict(Stats)    # Welford stats per endpoint
    path_hist = defaultdict(Histogram) # log-scale histogram per endpoint
    overall_stats = Stats()
    overall_hist = Histogram()

    first_ts = None
    last_ts = None

    # Key: (year, month, day, hour) — avoids merging same-hour slots across days
    hourly_requests: Counter = Counter()
    hourly_errors:   Counter = Counter()

    with p.open(encoding="utf-8", errors="replace") as log_fh:
      for raw_line in log_fh:
        total_lines += 1
        stripped = raw_line.strip()

        # Categorise malformed lines before parse_line discards them
        if not stripped:
            malformed_lines += 1
            skip_blank += 1
            continue
        if stripped.startswith("{"):
            try:
                json.loads(stripped)
            except json.JSONDecodeError:
                malformed_lines += 1
                skip_invalid_json += 1
                continue
        # Stack traces / continuation lines: no HTTP method, no path, no status
        _is_stacktrace = (
            not any(c in stripped for c in ('/', ':'))
            and not stripped[0].isdigit()
            and stripped.split()[0].upper() not in _HTTP_METHODS
        ) if stripped else False

        record = parse_line(raw_line)
        if record is None:
            malformed_lines += 1
            if _is_stacktrace:
                skip_stacktrace += 1
            else:
                skip_no_fields += 1
            continue

        parsed_lines += 1
        if record["source"] == "json":
            json_lines += 1

        ts = record["timestamp"]
        if ts:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
            bucket = (ts.year, ts.month, ts.day, ts.hour)
            hourly_requests[bucket] += 1

        status = record["status"]
        if status:
            status_counter[status] += 1
            if status >= 400:
                if ts:
                    bucket = (ts.year, ts.month, ts.day, ts.hour)
                    hourly_errors[bucket] += 1
                if record["path"]:
                    path_errors[record["path"]] += 1

        if record["method"]:
            method_counter[record["method"]] += 1

        if record["ip"]:
            ip_counter[record["ip"]] += 1

        ep = record["path"]
        if ep:
            path_counter[ep] += 1

        rt = record["response_ms"]
        if rt is not None and ep:
            path_stats[ep].add(rt)
            path_hist[ep].add(rt)
            overall_stats.add(rt)
            overall_hist.add(rt)

    # ── Derived metrics ──────────────────────────────────────────────────────

    total_requests = parsed_lines
    error_count = sum(v for k, v in status_counter.items() if k >= 400)
    error_rate = error_count / total_requests * 100 if total_requests else 0

    # Slowest endpoints by mean response time (min 5 requests for significance)
    slowest = sorted(
        [(ep, s) for ep, s in path_stats.items() if s.n >= 5],
        key=lambda x: x[1].mean,
        reverse=True,
    )[:top_n]

    # Busiest endpoints
    busiest = path_counter.most_common(top_n)

    # Most error-prone endpoints (by error count)
    error_endpoints = sorted(path_errors.items(), key=lambda x: x[1], reverse=True)[:top_n]

    # Top IPs
    top_ips = ip_counter.most_common(top_n)

    # Status code groups
    status_groups = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "other": 0}
    for code, cnt in status_counter.items():
        key = f"{code // 100}xx"
        if key in status_groups:
            status_groups[key] += cnt
        else:
            status_groups["other"] += cnt

    # Peak traffic hour — keyed by (year, month, day, hour) to avoid cross-day merging
    peak_bucket = max(hourly_requests, key=hourly_requests.get) if hourly_requests else None
    if peak_bucket:
        peak_hour_dt = datetime(
            peak_bucket[0], peak_bucket[1], peak_bucket[2], peak_bucket[3],
            tzinfo=timezone.utc
        )
        peak_hour_str  = peak_hour_dt.strftime("%Y-%m-%d %H:00 UTC")
        peak_hour_count = hourly_requests[peak_bucket]
    else:
        peak_hour_str  = None
        peak_hour_count = 0

    # Endpoint percentile table — endpoints sorted by p95 descending (min 5 req)
    endpoint_percentiles = []
    for ep, hist in path_hist.items():
        if hist.n >= 5:
            s = path_stats[ep]
            endpoint_percentiles.append({
                "path":      ep,
                "requests":  hist.n,
                "mean_ms":   round(s.mean, 1),
                "p50_ms":    hist.percentile(50),
                "p95_ms":    hist.percentile(95),
                "p99_ms":    hist.percentile(99),
                "max_ms":    round(s.max, 1),
                "stddev_ms": round(s.stddev, 1),
            })
    endpoint_percentiles.sort(key=lambda x: x["p95_ms"], reverse=True)

    # Overall percentiles
    overall_p = {
        "mean": round(overall_stats.mean, 1) if overall_stats.n else None,
        "min":  round(overall_stats.min,  1) if overall_stats.n else None,
        "max":  round(overall_stats.max,  1) if overall_stats.n else None,
        "p50":  overall_hist.percentile(50)  if overall_hist.n  else None,
        "p95":  overall_hist.percentile(95)  if overall_hist.n  else None,
        "p99":  overall_hist.percentile(99)  if overall_hist.n  else None,
    }

    return {
        "file": str(p.resolve()),
        "total_lines": total_lines,
        "parsed_lines": parsed_lines,
        "malformed_lines": malformed_lines,
        "malformed_breakdown": {
            "blank_lines":   skip_blank,
            "invalid_json":  skip_invalid_json,
            "no_fields":     skip_no_fields,
            "stack_traces":  skip_stacktrace,
        },
        "json_lines": json_lines,
        "parse_rate_pct": parsed_lines / total_lines * 100 if total_lines else 0,
        "time_range": {
            "first": first_ts.isoformat() if first_ts else None,
            "last":  last_ts.isoformat()  if last_ts  else None,
        },
        "traffic": {
            "total_requests":  total_requests,
            "error_count":     error_count,
            "error_rate_pct":  round(error_rate, 2),
            "peak_hour":       peak_hour_str,
            "peak_hour_count": peak_hour_count,
            # Serialise hourly buckets as "YYYY-MM-DD HH" strings for JSON output
            "hourly_requests": {
                f"{y:04d}-{mo:02d}-{d:02d} {h:02d}": cnt
                for (y, mo, d, h), cnt in sorted(hourly_requests.items())
            },
            "hourly_errors": {
                f"{y:04d}-{mo:02d}-{d:02d} {h:02d}": cnt
                for (y, mo, d, h), cnt in sorted(hourly_errors.items())
            },
        },
        "status_groups":    status_groups,
        "status_breakdown": dict(status_counter.most_common(20)),
        "methods":          dict(method_counter.most_common()),
        "top_ips":          top_ips,
        "busiest_endpoints":   busiest,
        "error_endpoints":     error_endpoints,
        "slowest_endpoints": [
            {
                "path":      ep,
                "requests":  s.n,
                "mean_ms":   round(s.mean, 1),
                "max_ms":    round(s.max, 1),
                "min_ms":    round(s.min, 1),
                "stddev_ms": round(s.stddev, 1),
            }
            for ep, s in slowest
        ],
        "endpoint_percentiles": endpoint_percentiles[:top_n],
        "overall_response_ms":  overall_p,
    }


# ---------------------------------------------------------------------------
# Pretty-print report
# ---------------------------------------------------------------------------

CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"
DIM    = "\033[2m"

def _bar(value, max_value, width=30, char="█"):
    if max_value == 0:
        return ""
    filled = int(value / max_value * width)
    return char * filled + DIM + "░" * (width - filled) + RESET


def _pct_colour(ms):
    if ms >= 1000:
        return RED
    if ms >= 500:
        return YELLOW
    return GREEN


def print_report(result: dict, top_n: int = 10, percentile: int = 95):
    r = result
    t = r["traffic"]
    sep = CYAN + "─" * 70 + RESET

    print(f"\n{BOLD}{CYAN}{'═'*70}{RESET}")
    print(f"{BOLD}  LOG ANALYSER REPORT{RESET}")
    print(f"  File : {r['file']}")
    tr = r["time_range"]
    if tr["first"]:
        print(f"  Range: {tr['first']}  →  {tr['last']}")
    print(f"{CYAN}{'═'*70}{RESET}\n")

    # Parse quality
    colour = GREEN if r["parse_rate_pct"] >= 90 else YELLOW if r["parse_rate_pct"] >= 75 else RED
    print(f"{BOLD}PARSE QUALITY{RESET}")
    print(f"  Total lines   : {r['total_lines']:,}")
    print(f"  Parsed        : {colour}{r['parsed_lines']:,}{RESET}  ({r['parse_rate_pct']:.1f}%)")
    print(f"  Malformed     : {RED if r['malformed_lines'] else DIM}{r['malformed_lines']:,}{RESET}")
    if r['malformed_lines'] > 0:
        bd = r["malformed_breakdown"]
        labels = [
            ("blank lines",   bd["blank_lines"]),
            ("invalid JSON",  bd["invalid_json"]),
            ("no fields",     bd["no_fields"]),
            ("stack traces",  bd["stack_traces"]),
        ]
        parts = [f"{label}: {cnt:,}" for label, cnt in labels if cnt > 0]
        if parts:
            print(f"  {DIM}  → {',  '.join(parts)}{RESET}")
    print(f"  JSON lines    : {r['json_lines']:,}")
    print()

    # Traffic overview
    err_colour = RED if t["error_rate_pct"] > 10 else YELLOW if t["error_rate_pct"] > 2 else GREEN
    print(f"{BOLD}TRAFFIC OVERVIEW{RESET}")
    print(f"  Total requests : {t['total_requests']:,}")
    print(f"  Errors (≥400)  : {err_colour}{t['error_count']:,}  ({t['error_rate_pct']}%){RESET}")
    if t["peak_hour"]:
        print(f"  Peak hour      : {t['peak_hour']}  ({t['peak_hour_count']:,} req)")
    print()

    # Status code groups
    print(f"{BOLD}STATUS CODES{RESET}")
    sg = r["status_groups"]
    for label, cnt in sorted(sg.items()):
        if cnt == 0:
            continue
        colour = GREEN if label == "2xx" else YELLOW if label == "3xx" else RED
        bar = _bar(cnt, t["total_requests"])
        print(f"  {colour}{label}{RESET}  {bar}  {cnt:,}")
    print()

    # HTTP methods
    print(f"{BOLD}HTTP METHODS{RESET}")
    mx = max(r["methods"].values(), default=1)
    for method, cnt in sorted(r["methods"].items(), key=lambda x: -x[1]):
        bar = _bar(cnt, mx, width=20)
        print(f"  {method:<8} {bar}  {cnt:,}")
    print()

    # Overall response time + percentiles
    ort = r["overall_response_ms"]
    if ort["mean"] is not None:
        print(f"{BOLD}RESPONSE TIMES (overall){RESET}")
        print(f"  Mean : {ort['mean']} ms   p50 : {ort['p50']} ms   "
              f"p95 : {ort['p95']} ms   p99 : {ort['p99']} ms   Max : {ort['max']} ms")
        print()

    # Endpoint percentile ranking (sorted by chosen percentile)
    pct_key = f"p{percentile}_ms"
    eps = [e for e in r["endpoint_percentiles"] if pct_key in e]
    eps_sorted = sorted(eps, key=lambda x: x[pct_key], reverse=True)[:top_n]
    if eps_sorted:
        print(f"{BOLD}TOP {top_n} ENDPOINTS BY p{percentile} LATENCY  (min 5 requests){RESET}")
        max_val = eps_sorted[0][pct_key]
        for ep in eps_sorted:
            val = ep[pct_key]
            bar = _bar(val, max_val, width=25)
            c = _pct_colour(val)
            print(f"  {bar} {c}{val:>8.0f} ms{RESET}  {ep['path']}")
            print(f"  {' '*25}   mean={ep['mean_ms']} ms  "
                  f"p50={ep['p50_ms']:.0f}  p95={ep['p95_ms']:.0f}  p99={ep['p99_ms']:.0f}  "
                  f"max={ep['max_ms']} ms  n={ep['requests']}")
        print()

    # Busiest endpoints
    print(f"{BOLD}TOP {top_n} BUSIEST ENDPOINTS{RESET}")
    if r["busiest_endpoints"]:
        mx = r["busiest_endpoints"][0][1]
        for ep, cnt in r["busiest_endpoints"]:
            bar = _bar(cnt, mx, width=25)
            print(f"  {bar} {cnt:>7,}  {ep}")
    print()

    # Most error-prone endpoints
    print(f"{BOLD}TOP {top_n} ERROR-PRONE ENDPOINTS{RESET}")
    if r["error_endpoints"]:
        mx = r["error_endpoints"][0][1]
        for ep, cnt in r["error_endpoints"]:
            bar = _bar(cnt, mx, width=25, char="▓")
            print(f"  {RED}{bar}{RESET} {cnt:>7,}  {ep}")
    else:
        print("  (none)")
    print()

    # Top IPs
    print(f"{BOLD}TOP {top_n} CLIENT IPs{RESET}")
    if r["top_ips"]:
        mx = r["top_ips"][0][1]
        for ip, cnt in r["top_ips"]:
            bar = _bar(cnt, mx, width=20)
            print(f"  {bar} {cnt:>7,}  {ip}")
    print()

    # Hourly traffic sparkline — collapse (date, hour) buckets back to hour-of-day
    if r["traffic"]["hourly_requests"]:
        print(f"{BOLD}HOURLY TRAFFIC (UTC){RESET}")
        # Sum across all days for the sparkline (visual overview only)
        hour_totals: Counter = Counter()
        for key, cnt in r["traffic"]["hourly_requests"].items():
            hour = int(key.split()[1])
            hour_totals[hour] += cnt
        mx = max(hour_totals.values(), default=1)
        bars = "▁▂▃▄▅▆▇█"
        spark = ""
        for h in range(24):
            v = hour_totals.get(h, 0)
            idx = min(int(v / mx * (len(bars) - 1)), len(bars) - 1) if mx else 0
            spark += bars[idx]
        print(f"  00  {spark}  23")
        print()

    print(f"{CYAN}{'═'*70}{RESET}")
    print(f"{DIM}  Parsed {r['parsed_lines']:,} of {r['total_lines']:,} lines  |  "
          f"{r['malformed_lines']:,} skipped as malformed{RESET}")
    print(f"{CYAN}{'═'*70}{RESET}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyse a web-server log file and print a summary report."
    )
    parser.add_argument("logfile", help="Path to the log file")
    parser.add_argument("--json", action="store_true",
                        help="Emit raw JSON instead of the human-readable report")
    parser.add_argument("--top", type=int, default=10, metavar="N",
                        help="How many entries to show in each ranked list (default: 10)")
    parser.add_argument("--percentile", type=int, default=95, metavar="N",
                        choices=[50, 75, 90, 95, 99],
                        help="Percentile to rank endpoints by (default: 95)")
    args = parser.parse_args()

    result = analyse(args.logfile, top_n=args.top)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print_report(result, top_n=args.top, percentile=args.percentile)


if __name__ == "__main__":
    main()

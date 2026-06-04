![Python](https://img.shields.io/badge/Python-3.8+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Tests](https://img.shields.io/badge/Tests-50_Passing-brightgreen)
![Status](https://img.shields.io/badge/Status-Production_Ready-success)

# Log Analyser

Reads a web server log file and produces a useful summary — error rates, slow endpoints, top IPs, traffic by hour. Handles mixed timestamp formats, malformed lines, and JSON logs without crashing.

---

## Requirements

Python 3.8+. No pip installs needed — stdlib only.

---

## How to run

```bash
# generate a sample log to test with
python scripts/generate_logs.py --lines 10000 --out sample.log

# run the analyser
python analyze.py sample.log

# options
python analyze.py access.log --top 20        # show top 20 per section (default 10)
python analyze.py access.log --percentile 99 # rank endpoints by p99 instead of p95
python analyze.py access.log --json          # JSON output instead of the report
python analyze.py access.log --json > out.json
```

---

## Example output

```
══════════════════════════════════════════════════════════════════════
  LOG ANALYSER REPORT
  File : sample.log
  Range: 2024-03-15T00:00:00+00:00  →  2024-03-15T00:03:36+00:00
══════════════════════════════════════════════════════════════════════

PARSE QUALITY
  Total lines   : 15,000
  Parsed        : 14,359  (95.7%)
  Malformed     : 641
  JSON lines    : 1,078

TRAFFIC OVERVIEW
  Total requests : 14,359
  Errors (>=400) : 3,585  (24.97%)
  Peak hour      : 2024-03-15 00:00 UTC  (14,274 req)

STATUS CODES
  2xx  ████████████████████░░░░░░░░░░  9,574
  3xx  ██░░░░░░░░░░░░░░░░░░░░░░░░░░░░  1,115
  4xx  ██████░░░░░░░░░░░░░░░░░░░░░░░░  2,901
  5xx  █░░░░░░░░░░░░░░░░░░░░░░░░░░░░░    684

RESPONSE TIMES (overall)
  Mean : 147.8 ms   p50 : 200.0 ms   p95 : 1000.0 ms   p99 : 5000.0 ms   Max : 7285.0 ms

TOP 10 ENDPOINTS BY p95 LATENCY  (min 5 requests)
  █████████████████████████     1000 ms  /api/health
                               mean=157.2 ms  p50=200  p95=1000  p99=5000  max=4142.0 ms  n=951

TOP 10 ERROR-PRONE ENDPOINTS
  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓     281  /api/logout

HOURLY TRAFFIC (UTC)
  00  █▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁  23

══════════════════════════════════════════════════════════════════════
  Parsed 14,359 of 15,000 lines  |  641 skipped as malformed
══════════════════════════════════════════════════════════════════════
```

---

## Running tests

```bash
python tests/test_parser.py
```

All tests should pass without any third-party dependencies. 50 tests, all passing. Covers timestamp formats, JSON lines, malformed input, response time parsing, status vs response time disambiguation, dash status codes, empty files, and integration tests.

---

## Generating test data

```bash
python scripts/generate_logs.py                          # 10k lines → sample.log
python scripts/generate_logs.py --lines 100000 --out big.log
```

The generator intentionally includes all the messy variants: mixed timestamp formats, JSON-formatted lines, extra appended fields (user agents in quotes), blank lines, stack trace fragments, partial writes, response times in seconds and as bare integers, and status codes replaced with `-`.

---

## Project structure

```
log_analyzer/
├── analyze.py                  ← main tool
├── tests/
│   └── test_parser.py          ← 50 tests
├── scripts/
│   └── generate_logs.py        ← test data generator
├── sample.log                  ← generated file (gitignored)
├── README.md
└── ANSWERS.md
```

---

## What the report covers

- **Parse quality** — lines parsed vs skipped, JSON line count
- **Traffic overview** — total requests, error count and rate, peak hour (exact date + hour, correct across multi-day logs)
- **Status codes** — 2xx/3xx/4xx/5xx breakdown with bar chart
- **HTTP methods** — split by method with bar chart
- **Response times** — overall mean/p50/p95/p99/max; per-endpoint p50/p95/p99 ranked by chosen percentile (`--percentile`)
- **Busiest endpoints** — by request count
- **Error-prone endpoints** — by 4xx+5xx count
- **Top client IPs** — most active
- **Hourly sparkline** — traffic pattern across the day

---

## Known limitations

- JSON numeric durations: bare floats (e.g. `2.5`) are treated as seconds; bare integers (e.g. `142`) as milliseconds. Strings with explicit units (`"93ms"`, `"0.5s"`) are always parsed correctly.
- IPv4 only — logs with IPv6 addresses will parse fine but IPs won't be extracted
- Single-line JSON only — multi-line JSON objects aren't handled
- Tested on UTF-8 logs; other encodings get replacement characters

# ANSWERS.md

---

## 1. How to Run

Python 3.8+ required, no pip installs needed.

```bash
# generate a test file first
python scripts/generate_logs.py --lines 10000 --out sample.log

# run the analyser
python analyze.py sample.log

# other options
python analyze.py access.log --top 20        # show top 20 per section
python analyze.py access.log --json          # raw JSON output
python analyze.py access.log --json > report.json  # save to file
```

Just point it at any log file. It doesn't care what the file is named or how many lines it has.

---

## 2. Stack Choice

**Python, stdlib only.**

The choice was straightforward for this task. Python has `re`, `json`, `collections`, and `datetime` all built in, which covers everything needed for text parsing and aggregation. No pip install means it runs on any machine with Python — no setup friction, no dependency conflicts.

I also know Python well enough to move fast, which mattered here.

**What would have been worse:**

Bash/awk was tempting because you naturally reach for grep when parsing logs. But the moment you need to handle JSON lines, compute standard deviation, and deal with four different timestamp formats in the same tool, bash becomes a mess. I tried a quick spike in awk and gave up after 30 minutes — the quoting rules alone were a problem.

Node.js would have worked but streaming large files line-by-line is more awkward than Python's default iteration, Date parsing in JS is notoriously inconsistent, and it would have required shipping a `node_modules` folder or adding a build step.

---

## 3. One Real Edge Case

**The status code vs response time ambiguity.**

**File:** `analyze.py` — function `parse_line()`, response time parsing section

```python
# Response time — token ending in ms/s or bare integer after status
found_status = False
for t in tokens:
    if _STATUS_RE.fullmatch(t):
        found_status = True
        continue
    if found_status:
        rt = _parse_response_time(t)
        if rt is not None:
            record["response_ms"] = rt
            break
```

The problem: a bare integer like `401` or `500` is a valid status code AND a valid response time in milliseconds. A naive scanner that just looks for "any number" will grab the status code token and treat it as the response time.

For example:
```
2024-03-15T14:23:01Z 10.0.0.7 POST /api/login 401 89ms
```

Without this fix, a naive scanner sees `401` first, decides that's the response time (401ms), and never finds `89ms`. Every 4xx and 5xx endpoint would show wildly inflated response times and the slowest endpoints report would be completely wrong.

The fix tracks when a status code token has already been consumed, and only then starts looking for the response time in the next token. Simple, but getting it wrong would silently corrupt the most useful part of the report.

**A second edge case also handled:** `analyze.py` — function `parse_line()`, status code parsing section

Status codes replaced with `-` (as the spec describes) are now treated as "status missing" rather than malformed. The line still contributes to path counts, method counts, and response time stats — it just doesn't count toward any status group. Without this, those lines would be silently discarded and the parse rate would appear lower than it actually is.

---

## 4. AI Usage

I used Claude throughout this project.

**a) Architecture planning**

Asked: *"I asked Claude for common problems when parsing messy web server logs."*

It gave a solid list: mixed timestamps, the integer ambiguity problem, JSON-interleaved lines, multi-line stack traces, large file memory. This shaped the overall structure — separate functions for each concern rather than one giant regex.

What I changed: Claude suggested using `dateutil.parser.parse()` for timestamps. I replaced that with explicit regex patterns and `strptime` because (1) `dateutil` is a third-party package and I wanted zero dependencies, and (2) `dateutil` can behave unexpectedly with ambiguous dates like `03/04/2024`. My version either matches a known pattern or returns `None` — no surprises.

**b) Welford's algorithm**

Asked how to compute running mean and stddev without storing all values in memory.

It explained Welford's online algorithm. I implemented the `Stats` class based on that explanation. What I changed: Claude's example was a standalone function. I turned it into a class so I could use `defaultdict(Stats)` to track per-endpoint stats without pre-initialising every key — cleaner for this use case.

**c) Sparkline characters**

Asked what Unicode block characters look good for a terminal sparkline. It suggested `▁▂▃▄▅▆▇█`. I used those directly but wrote my own binning logic — Claude's version had an off-by-one error in the index calculation that would crash on the maximum value.

**d) Test case design**

Asked what edge cases a log parser reviewer would specifically look for. It flagged the status/response ambiguity (which I had already found independently), plus empty files, stack trace continuation lines, and JSON with alternate field names. Helped me fill out the test suite to 50 cases.

---

## 5. Percentile Reporting

**Implemented: constant-memory p50/p95/p99 via log-scale histograms.**

Mean response time is easily skewed by a handful of slow outliers. An endpoint with 99 requests completing in 20ms and one in 8000ms shows a mean of ~99ms — which hides the actual tail latency entirely. p95 and p99 are what SLAs are actually measured against.

The implementation uses a `Histogram` class with 15 fixed log-scale buckets covering 1ms to 60s (1, 2, 5, 10, 20, 50, 100, 200, 500ms, 1s, 2s, 5s, 10s, 30s, 60s) plus an overflow bucket. Memory usage is constant regardless of log file size — one `Histogram` per endpoint, each using 16 integers.

The analyser now reports p50/p95/p99 per endpoint and overall, and exposes a `--percentile` flag so you can rank endpoints by whichever percentile matters:

```bash
python analyze.py access.log --percentile 99   # rank by p99
python analyze.py access.log --percentile 95   # default
```

The trade-off vs a t-digest: bucket boundaries are fixed, so precision is limited to the bucket granularity (e.g. a true p95 of 480ms reports as ≤500ms). For operational use — "is p95 above 500ms?" — that's sufficient. A t-digest would give exact percentiles but is significantly more complex to implement correctly from scratch.

"""按 label 聚合 JMeter .jtl：TPS / mean / p90 / p95 / p99 / max / errors。
用法：python -m portal.perf.jtl_stats <a.jtl> [<b.jtl> ...]
"""
import csv
import sys
from collections import defaultdict


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = min(len(sorted_vals) - 1, int(round((p / 100) * (len(sorted_vals) - 1))))
    return float(sorted_vals[k])


def load(path):
    rows = defaultdict(list)  # label -> [(elapsed, success)]
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            try:
                lbl = r["label"]
                el = float(r["elapsed"])
                ok = r.get("success", "true").strip().lower() != "false"
            except (KeyError, ValueError):
                continue
            rows[lbl].append((el, ok))
    return rows


def dur(path):
    # 用首末时间戳估算总时长，算 TPS
    ts = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            try:
                ts.append(int(r["timeStamp"]))
            except (KeyError, ValueError):
                continue
    if len(ts) < 2:
        return 1.0
    t0, t1 = min(ts), max(ts)
    span = (t1 + 1000 - t0) / 1000.0  # 末样本再加 ~1s 覆盖其自身耗时
    return max(span, 1.0)


def report(path):
    rows = load(path)
    seconds = dur(path)
    print(f"\n==== {path}  (≈{seconds:.1f}s) ====")
    grand = sum(len(v) for v in rows.values())
    for lbl in sorted(rows):
        vals = rows[lbl]
        el = sorted(e for e, _ in vals)
        errs = sum(1 for _, ok in vals if not ok)
        n = len(el)
        mean = sum(el) / n
        print(f"  {lbl:8s} n={n:5d} TPS={n/seconds:6.2f}/s "
              f"mean={mean:6.1f} p50={pct(el,50):5.0f} p90={pct(el,90):5.0f} "
              f"p95={pct(el,95):5.0f} p99={pct(el,99):5.0f} max={el[-1]:5.0f} err={errs}")
    print(f"  {'TOTAL':8s} n={grand:5d} TPS={grand/seconds:6.2f}/s")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        report(p)

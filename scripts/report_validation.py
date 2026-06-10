# -*- coding: utf-8 -*-
"""
シグナル検証レポート（docs/validation_protocol.md の集計実装）

読み取り専用・本体パイプライン無改変・stdlib のみ。
  python scripts/report_validation.py

- 確定データ: signal_history.jsonl + signal_history_*.jsonl（月次アーカイブ、glob読み）
- カバレッジ: signal_pending_*.jsonl（未確定含む全記録）
- バケット: divergence(別枠) / critical(|conv|>=60) / normal(30-59) / reference(15-29) / other(<15)
- 指標: 方向一致率 + Wilson 95%CI（自前実装）、平均/中央値/winsorized平均 %リターン
- divergence は TA方向・ニュース方向の両方の一致率を出す
- データ不足セルは n 表示で空欄（エラーにしない）
"""
import glob
import json
import math
import os
import statistics
import sys

HORIZONS = ["1h", "24h", "72h", "168h"]
BUCKETS = ["critical", "normal", "reference", "other", "divergence"]
# 本判定/interim の n 基準（protocol §5c）
N_FULL = 100
N_INTERIM = 30

_DIR_SIGN = {"bullish": 1, "bearish": -1, "neutral": 0}


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_history():
    """history + 全月次アーカイブ（signal_id で重複排除）。"""
    rows, seen = [], set()
    for p in ["signal_history.jsonl"] + sorted(glob.glob("signal_history_*.jsonl")):
        for r in read_jsonl(p):
            sid = r.get("signal_id") or (r.get("timestamp"), r.get("symbol"), r.get("timeframe"))
            if sid in seen:
                continue
            seen.add(sid)
            rows.append(r)
    return rows


def load_pending():
    rows = []
    for p in sorted(glob.glob("signal_pending_*.jsonl")):
        rows.extend(read_jsonl(p))
    return rows


def bucket_of(rec):
    if rec.get("is_divergence"):
        return "divergence"
    c = abs(rec.get("conviction_score") or 0)
    if c >= 60:
        return "critical"
    if c >= 30:
        return "normal"
    if c >= 15:
        return "reference"
    return "other"


def wilson_ci(hits, n, z=1.96):
    """Wilson 95% 信頼区間 (lower, upper)。n=0 は None。"""
    if n == 0:
        return None
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def winsorized_mean(vals, pct=0.95):
    """|値| の上位5%を95パーセンタイルで丸めた平均（protocol §4）。"""
    if not vals:
        return None
    if len(vals) < 20:
        return statistics.mean(vals)  # 丸め対象が立たない規模はそのまま
    abs_sorted = sorted(abs(v) for v in vals)
    cap = abs_sorted[max(0, math.ceil(pct * len(abs_sorted)) - 1)]
    return statistics.mean(max(-cap, min(cap, v)) for v in vals)


def raw_return(rec, hz):
    """記録の符号付きリターンから生リターン（価格変化率そのもの）を復元。"""
    rp = rec.get("horizons", {}).get(hz, {}).get("return_pct")
    if rp is None:
        return None
    sign = _DIR_SIGN.get(rec.get("direction"), 0)
    return rp * sign if sign != 0 else rp  # bearishは再反転で生値に戻る


def status_label(n):
    if n >= N_FULL:
        return "本判定"
    if n >= N_INTERIM:
        return "interim"
    return "参考(n不足)"


def fmt_pct(x):
    return f"{x * 100:.0f}%" if x is not None else "—"


def fmt_num(x, nd=2):
    return f"{x:+.{nd}f}" if x is not None else "—"


def hit_cell(recs, hz, hit_fn):
    """一致率セル: 'hit% [CI下限-上限] n=N (状態)'。"""
    hits = n = 0
    for r in recs:
        h = hit_fn(r, hz)
        if h is None:
            continue
        n += 1
        hits += 1 if h else 0
    if n == 0:
        return "— (n=0)"
    ci = wilson_ci(hits, n)
    return f"{fmt_pct(hits / n)} [{fmt_pct(ci[0])}–{fmt_pct(ci[1])}] n={n} ({status_label(n)})"


def ta_hit(rec, hz):
    """TA方向（記録 direction）の一致。neutral は除外（None）。"""
    return rec.get("horizons", {}).get(hz, {}).get("dir_hit")


def news_hit(rec, hz):
    """ニュース方向（sign(net_direction)）の一致。中立は除外（None）。"""
    nd = rec.get("net_direction") or 0
    if nd == 0:
        return None
    raw = raw_return(rec, hz)
    if raw is None or raw == 0:
        return None
    return (raw > 0) == (nd > 0)


def returns_of(recs, hz):
    vals = [r.get("horizons", {}).get(hz, {}).get("return_pct") for r in recs]
    return [v for v in vals if v is not None]


def section_hit_table(recs, title, hit_fn=ta_hit):
    print(f"\n### {title}\n")
    print("| バケット | " + " | ".join(HORIZONS) + " |")
    print("|---|" + "---|" * len(HORIZONS))
    for b in BUCKETS:
        sub = [r for r in recs if bucket_of(r) == b]
        cells = [hit_cell(sub, hz, hit_fn) for hz in HORIZONS]
        print(f"| {b} | " + " | ".join(cells) + " |")


def section_return_table(recs, title):
    print(f"\n### {title}（主168h・副72h）\n")
    print("| バケット | 168h 平均 | 168h 中央値 | 168h wins平均 | 72h 平均 | 72h 中央値 | 72h wins平均 |")
    print("|---|---|---|---|---|---|---|")
    for b in BUCKETS:
        sub = [r for r in recs if bucket_of(r) == b]
        row = [b]
        for hz in ("168h", "72h"):
            vals = returns_of(sub, hz)
            row += [
                fmt_num(statistics.mean(vals)) if vals else "— (n=0)",
                fmt_num(statistics.median(vals)) if vals else "—",
                fmt_num(winsorized_mean(vals)) if vals else "—",
            ]
        print("| " + " | ".join(row) + " |")


def section_divergence(recs):
    div = [r for r in recs if bucket_of(r) == "divergence"]
    print(f"\n## divergence 両面検証（TA方向 vs ニュース方向、n={len(div)}）\n")
    if not div:
        print("確定 divergence レコードなし（n=0）。")
        return
    print("| 一致率の軸 | " + " | ".join(HORIZONS) + " |")
    print("|---|" + "---|" * len(HORIZONS))
    print("| TA方向 | " + " | ".join(hit_cell(div, hz, ta_hit) for hz in HORIZONS) + " |")
    print("| ニュース方向 | " + " | ".join(hit_cell(div, hz, news_hit) for hz in HORIZONS) + " |")


def section_coverage(pending, history):
    print("\n## カバレッジ（記録件数: pending=未確定 + history=確定）\n")
    combos = {}
    for label, rows in (("pending", pending), ("history", history)):
        for r in rows:
            key = (r.get("symbol") or "?", r.get("timeframe") or "?")
            combos.setdefault(key, {"pending": 0, "history": 0})[label] += 1
    print("| symbol | timeframe | pending | history | 計 |")
    print("|---|---|---|---|---|")
    for (sym, tf), c in sorted(combos.items()):
        print(f"| {sym} | {tf} | {c['pending']} | {c['history']} | {c['pending'] + c['history']} |")
    if not combos:
        print("| — | — | 0 | 0 | 0 |")
    # バケット分布（pending+history合算、確信度の偏り監視）
    print("\n| バケット | 件数 |")
    print("|---|---|")
    allrows = pending + history
    for b in BUCKETS:
        print(f"| {b} | {sum(1 for r in allrows if bucket_of(r) == b)} |")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    history = load_history()
    pending = load_pending()

    print("# シグナル検証レポート")
    print(f"\n- プロトコル: docs/validation_protocol.md（2026-06-11 固定）")
    print(f"- 確定レコード(history): {len(history)} / 未確定(pending): {len(pending)}")
    print(f"- 本判定は各バケット n>={N_FULL}。n>={N_INTERIM} は interim（判断材料にしない）。")

    print("\n## 方向一致率（Wilson 95%CI）")
    section_hit_table(history, "全 timeframe 合算")
    for tf in ("4時間", "日足"):
        sub = [r for r in history if r.get("timeframe") == tf]
        section_hit_table(sub, f"timeframe = {tf}")

    print("\n## 符号付き%リターン")
    section_return_table(history, "全 timeframe 合算")
    for tf in ("4時間", "日足"):
        sub = [r for r in history if r.get("timeframe") == tf]
        section_return_table(sub, f"timeframe = {tf}")

    section_divergence(history)
    section_coverage(pending, history)


if __name__ == "__main__":
    main()

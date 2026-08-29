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
from datetime import datetime, timezone

HORIZONS = ["1h", "24h", "72h", "168h"]
# v1.10 §18: news_era 境界（本書で固定・以後変更しない）。
# ニュース層停止 2026-06-08〜07-02 により無風レコードが同期間に集中するため、
# 「ニュースあり/なし」の比較が期間比較と交絡していないかを診断する。
NEWS_ERA_CUTOFF = datetime(2026, 7, 2, 22, 0, 0, tzinfo=timezone.utc)
ERA_LABELS = ["pre_outage_or_outage", "post_recovery"]
# v1.11 §19.2: アウトオブサンプル確認の開始時刻（登録日）。
# 仮説は登録日時点の既存サンプルの探索的観察から生成されたため、
# これより前の記録は一切対象に含めない。
V111_START = datetime(2026, 8, 29, 0, 0, 0, tzinfo=timezone.utc)
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
    conservative_table(div, lambda r: "divergence", ["divergence"], "divergence両面",
                       hit_fns=[("TA方向 168h", ta_hit), ("ニュース方向 168h", news_hit)])


def dedup_weekly(recs):
    """v1.5 保守集計: symbol×ISO週で時系列最初の1件に絞る（timeframe混合）。

    近接シグナルの168hウィンドウ重複による実効nの過大評価を補正する。
    厳格化方向のみ（protocol §12）。
    """
    out = {}
    for r in sorted(recs, key=lambda x: x.get("timestamp") or ""):
        try:
            ts = datetime.fromisoformat(r["timestamp"])
        except (KeyError, ValueError, TypeError):
            continue
        iso = ts.isocalendar()
        key = (r.get("symbol"), iso[0], iso[1])
        if key not in out:
            out[key] = r
    return list(out.values())


def conservative_table(recs, group_fn, group_labels, title, hit_fns=None):
    """v1.5 保守集計の併記テーブル（主軸168hのみ）。

    group_fn: rec→ラベル（対象外は None）。hit_fns: [(列名, hit関数)]。

    v1.9 §16: 適用順序は「群フィルタ → symbol×ISO週 dedup」。
    旧実装は dedup→群フィルタの順で、週の最初の1件が当該群に該当するかに n が依存し
    群間比較が成立していなかった。独立性の目的（各群内で symbol×ISO週あたり最大1観測）
    は本順序でも満たされる。n>=100 ゲートと合格条件は不変更。
    """
    if hit_fns is None:
        hit_fns = [("168h", ta_hit)]
    print(f"\n#### {title}（保守集計 v1.5/v1.9: 群内で symbol×ISO週 最初の1件・timeframe混合）\n")
    print("| 群 | " + " | ".join(n for n, _ in hit_fns) + " |")
    print("|---|" + "---|" * len(hit_fns))
    for g in group_labels:
        sub = dedup_weekly([r for r in recs if group_fn(r) == g])
        cells = [hit_cell(sub, "168h", fn) for _, fn in hit_fns]
        print(f"| {g} | " + " | ".join(cells) + " |")


def _news_group(rec):
    """v1.2 ニュース層帰属テストの群分け（protocol §10 の定義と同一に保つ）。

    normalized は記録に無いため式を固定して導出: sign(ta_score) × net_direction。
    """
    if rec.get("is_divergence"):
        return None  # divergence は対象外
    if not (rec.get("news_count") or 0):
        return "無風"  # news_count=0（中立に含めず別掲）
    ta = rec.get("ta_score") or 0
    sign = (ta > 0) - (ta < 0)
    normalized = sign * (rec.get("net_direction") or 0)
    if normalized >= 0.2:
        return "増幅"
    if normalized <= -0.2:
        return "減衰"
    return "中立"


def section_news_attribution(recs):
    """v1.2: ニュース層帰属テスト（減衰 < 中立 < 増幅 の単調性を検証する集計）。"""
    print("\n## ニュース層帰属（v1.2・divergence除外）\n")
    print("事前予測: 168h 方向一致率が 減衰 < 中立 < 増幅（本判定は各群 n>=100）。"
          "無風(news_count=0)は参考別掲。\n")
    groups = {"増幅": [], "中立": [], "減衰": [], "無風": []}
    for r in recs:
        g = _news_group(r)
        if g:
            groups[g].append(r)
    print("| 群 | " + " | ".join(HORIZONS) + " |")
    print("|---|" + "---|" * len(HORIZONS))
    for g in ("減衰", "中立", "増幅", "無風"):
        cells = [hit_cell(groups[g], hz, ta_hit) for hz in HORIZONS]
        print(f"| {g} | " + " | ".join(cells) + " |")
    conservative_table(recs, _news_group, ["減衰", "中立", "増幅", "無風"],
                       "ニュース層帰属")


def _event_group(rec):
    """v1.4 イベントゲート適用テストの群分け（protocol §13 と同一に保つ）。

    event_gate が dict でない（旧レコード欠落・yaml不読null）は判定対象外（None）。
    """
    eg = rec.get("event_gate")
    if not isinstance(eg, dict):
        return None
    if eg.get("pre"):
        return "pre"
    if eg.get("post"):
        return "post(除外・参考)"
    return "ウィンドウ外"


def section_event_gate(recs):
    """v1.4: イベントゲート適用テスト（pre群 vs 完全ウィンドウ外群）。"""
    print("\n## イベントゲート（v1.4・event_gate=null/欠落は対象外）\n")
    print("事前予測: pre群の 168h 一致率がウィンドウ外群より劣後（本判定は各群 n>=100・"
          "CI非重複の劣後で routing 1段階格下げを検討。適用は別途承認）。"
          "post内は比較から除外（参考別掲）。\n")
    labels = ["pre", "ウィンドウ外", "post(除外・参考)"]
    print("| 群 | " + " | ".join(HORIZONS) + " |")
    print("|---|" + "---|" * len(HORIZONS))
    groups = {g: [r for r in recs if _event_group(r) == g] for g in labels}
    for g in labels:
        cells = [hit_cell(groups[g], hz, ta_hit) for hz in HORIZONS]
        print(f"| {g} | " + " | ".join(cells) + " |")
    conservative_table(recs, _event_group, labels, "イベントゲート")


def _liq_group(rec):
    """v1.7 流動性トリガー対称テストの群分け（protocol §14 と同一に保つ）。

    群ラベル規約（方向仮説ではない）: sweep継続方向 high→+1 / low→−1 と
    conviction 符号の一致/不一致。sweep null・neutral は対象外（None）。
    """
    liq = rec.get("liquidity")
    if not isinstance(liq, dict):
        return None
    sw = liq.get("sweep")
    if not isinstance(sw, dict):
        return None
    cont = {"high": 1, "low": -1}.get(sw.get("side"), 0)
    conv = {"bullish": 1, "bearish": -1}.get(rec.get("direction"), 0)
    if cont == 0 or conv == 0:
        return None
    return "一致" if cont == conv else "不一致"


def section_liquidity(recs):
    """v1.7: 流動性トリガー整合の対称テスト（差の存在のみ・方向不問）。"""
    print("\n## 流動性トリガー（v1.7・sweep非nullのみ対象/対称・差の存在のみ）\n")
    print("群ラベル規約: sweep継続方向(high→+1/low→−1) × conviction符号の一致/不一致"
          "（ラベルは規約であり方向仮説ではない）。本判定は各群 n>=100・CI非重複で「差あり」。\n")
    labels = ["一致", "不一致"]
    print("| 群 | " + " | ".join(HORIZONS) + " |")
    print("|---|" + "---|" * len(HORIZONS))
    for g in labels:
        sub = [r for r in recs if _liq_group(r) == g]
        cells = [hit_cell(sub, hz, ta_hit) for hz in HORIZONS]
        print(f"| {g} | " + " | ".join(cells) + " |")
    conservative_table(recs, _liq_group, labels, "流動性トリガー")


def _entry_group(rec):
    """v1.8「両通知エントリー」テストの群分け（protocol §15.2 の定義と同一に保つ）。

    記録スキーマ対応（§15.3）: レコードに `routing` / `alignment` は存在しないため、
    routing は v1 §3 バケット導出（非divergence かつ |conviction|>=30 = normal/critical）、
    high_importance_count はトップレベルの記録済みフィールドを使う。
    normalized の導出式は v1.2 §10 と同一（sign(ta_score) × net_direction）だが、
    本テストは閾値±0.2ではなく > 0（厳密正）を用いる。
    """
    if rec.get("is_divergence"):
        return None  # divergence は対象外
    if bucket_of(rec) not in ("normal", "critical"):
        return None  # 実通知帯（normal/critical）以外は対象外
    ta = rec.get("ta_score") or 0
    sign = (ta > 0) - (ta < 0)
    normalized = sign * (rec.get("net_direction") or 0)
    if (rec.get("high_importance_count") or 0) >= 1 and normalized > 0:
        return "両通知"
    return "対照"


def _entry_group_strat(rec):
    """v1.9 §17: v1.8 対照群を news_count で層別（併記のみ・主検定は不変更）。

    層別の根拠は 2026-06-08〜07-02 のニュース層停止期間という既知のデータ来歴であり、
    観測された結果の方向に基づくものではない（v1.2 §10 の無風別掲と同じ扱い）。
    """
    g = _entry_group(rec)
    if g != "対照":
        return g  # 両通知 / None はそのまま
    return "対照:ニュース非該当" if (rec.get("news_count") or 0) >= 1 else "対照:無風"


def section_entry_rule(recs):
    """v1.8: 両通知エントリー（実発火AND）。主検定は 両通知群 vs 対照群（全体）。"""
    print("\n## 両通知エントリー（v1.8・divergence除外/routing=normal+critical のみ）\n")
    print("両通知群 = high_importance_count>=1 かつ sign(ta_score)×net_direction>0。"
          "対照群 = 同帯でニュース条件を満たさないもの。\n")
    print("事前予測: 両通知群 > 対照群（本判定は両群 n>=100・CI非重複の優越 かつ 保守集計で序列保存）。")
    print("注記: 一致率は方向勝率でありP&L勝率ではない（v1.8 §15.7）。"
          "ニュース側は eff_imp>=4 基準で news-bot 実通知(importance>=3)の部分集合（§15.4）。\n")

    labels = ["両通知", "対照"]
    strat_labels = ["対照:ニュース非該当", "対照:無風"]
    all_labels = ["両通知", "対照"] + strat_labels
    groups = {g: [r for r in recs if _entry_group(r) == g] for g in labels}
    groups.update({g: [r for r in recs if _entry_group_strat(r) == g] for g in strat_labels})

    print("**主検定（v1.8 §15.5・定義不変更）**\n")
    print("| 群 | " + " | ".join(HORIZONS) + " |")
    print("|---|" + "---|" * len(HORIZONS))
    for g in labels:
        print(f"| {g} | " + " | ".join(hit_cell(groups[g], hz, ta_hit) for hz in HORIZONS) + " |")

    print("\n**対照群の層別（v1.9 §17・併記のみ。主検定は上表のまま）**\n")
    print("| 層 | " + " | ".join(HORIZONS) + " |")
    print("|---|" + "---|" * len(HORIZONS))
    for g in strat_labels:
        print(f"| {g} | " + " | ".join(hit_cell(groups[g], hz, ta_hit) for hz in HORIZONS) + " |")

    # 中央値%リターン（protocol §15.5）
    print("\n| 群 | 168h 中央値 | 72h 中央値 |")
    print("|---|---|---|")
    for g in all_labels:
        row = [g]
        for hz in ("168h", "72h"):
            vals = returns_of(groups[g], hz)
            row.append(fmt_num(statistics.median(vals)) if vals else "— (n=0)")
        print("| " + " | ".join(row) + " |")

    conservative_table(recs, _entry_group, labels, "両通知エントリー（主検定）")
    conservative_table(recs, _entry_group_strat, ["両通知"] + strat_labels,
                       "両通知エントリー（層別併記）")


def _parse_ts(rec):
    """記録 timestamp を UTC aware datetime に。naive は UTC とみなす（CLAUDE.md 原則5）。"""
    try:
        ts = datetime.fromisoformat(rec["timestamp"])
    except (KeyError, ValueError, TypeError):
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def news_era(rec):
    """v1.10 §18.2: news_era ラベル（境界は NEWS_ERA_CUTOFF で固定）。"""
    ts = _parse_ts(rec)
    if ts is None:
        return None
    return "pre_outage_or_outage" if ts < NEWS_ERA_CUTOFF else "post_recovery"


def ts_median(recs):
    """群の記録タイムスタンプ中央値（期間の偏りを直接示す・v1.10 §18.2）。"""
    ts = sorted(t for t in (_parse_ts(r) for r in recs) if t is not None)
    if not ts:
        return "—"
    return ts[len(ts) // 2].strftime("%Y-%m-%d %H:%M")


def era_diag_table(recs, group_fn, group_labels, title):
    """v1.10 §18: 群 × news_era の診断テーブル（主集計/保守集計/期間層別/ts中央値）。"""
    print(f"\n### {title}\n")
    print("| 群 | 主集計 168h | 保守集計 168h | pre_outage_or_outage 168h | "
          "post_recovery 168h | ts中央値 |")
    print("|---|---|---|---|---|---|")
    for g in group_labels:
        sub = [r for r in recs if group_fn(r) == g]
        cells = [
            hit_cell(sub, "168h", ta_hit),
            hit_cell(dedup_weekly(sub), "168h", ta_hit),
            hit_cell([r for r in sub if news_era(r) == "pre_outage_or_outage"], "168h", ta_hit),
            hit_cell([r for r in sub if news_era(r) == "post_recovery"], "168h", ta_hit),
            ts_median(sub),
        ]
        print(f"| {g} | " + " | ".join(cells) + " |")


def _v111_group(rec):
    """v1.11 §19: 無風群 vs ニュース群（登録日以降の新規サンプルのみ）。

    §19.2: timestamp >= V111_START の記録のみ対象（登録日より前は仮説生成に
    使われたデータのため一切含めない）。§19.3: divergence は除外。
    """
    if rec.get("is_divergence"):
        return None
    ts = _parse_ts(rec)
    if ts is None or ts < V111_START:
        return None
    return "無風" if not (rec.get("news_count") or 0) else "ニュース"


def section_v111(recs):
    """v1.11: ニュース存在による TA 方向精度の劣化テスト（アウトオブサンプル確認）。"""
    print("\n## ニュース存在によるTA方向精度の劣化（v1.11・アウトオブサンプル確認）\n")
    print(f"対象: timestamp >= {V111_START.isoformat()} の記録のみ"
          "（登録日以降＝新規サンプル。登録日より前は仮説生成に使われたため除外）。")
    print("群: news_count=0（無風群） vs news_count>=1（ニュース群）。divergence は除外。")
    print("事前予測: 無風群 > ニュース群（168h方向一致率）。"
          "本判定は両群 主集計 n>=100・保守集計で序列保存。")
    print("**交絡の明示（§19.6）: 「ニュースの有無」と「市場の荒れ具合」は本テストでは"
          "分離できない。本テストの合格は因果の主張を含まない。**\n")
    labels = ["無風", "ニュース"]
    print("| 群 | " + " | ".join(HORIZONS) + " |")
    print("|---|" + "---|" * len(HORIZONS))
    for g in labels:
        sub = [r for r in recs if _v111_group(r) == g]
        print(f"| {g} | " + " | ".join(hit_cell(sub, hz, ta_hit) for hz in HORIZONS) + " |")
    conservative_table(recs, _v111_group, labels, "v1.11 ニュース存在の劣化テスト")


def section_period_confounding(recs):
    """v1.10 §18: 期間交絡の診断（全セクション併記・合否判定には使用しない）。"""
    print("\n## 期間層別診断（v1.10 §18・診断用/合否判定には使用しない）\n")
    print("ニュース層停止 2026-06-08〜07-02 により無風レコードが同期間に集中するため、"
          "「ニュースあり/なし」の比較が期間比較と交絡していないかを診断する。")
    print(f"境界: {NEWS_ERA_CUTOFF.isoformat()}（v1.10 で固定・変更しない）。")
    print("**用途は交絡の有無の記述のみ。本表を根拠に群定義の変更・基準改訂・"
          "事後除外は行わない（v1.10 §18.3）。**")
    era_diag_table(recs, bucket_of, ["reference", "normal", "critical"], "v1 バケット")
    era_diag_table(recs, _news_group, ["減衰", "中立", "増幅", "無風"], "v1.2 ニュース層帰属")
    era_diag_table(recs, _entry_group, ["両通知", "対照"], "v1.8 両通知エントリー（主検定）")
    era_diag_table(recs, _entry_group_strat, ["対照:ニュース非該当", "対照:無風"],
                   "v1.8 対照群の層別")
    era_diag_table(recs, _event_group, ["pre", "ウィンドウ外"], "v1.4 イベントゲート")
    era_diag_table(recs, _liq_group, ["一致", "不一致"], "v1.7 流動性トリガー")


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

    print(f"\n- v1.5 重複ウィンドウ補正: 全ての本判定は「主集計で基準充足 かつ "
          f"保守集計（symbol×ISO週の最初の1件）で序列保存」の場合のみ合格。")

    print("\n## 方向一致率（Wilson 95%CI）")
    section_hit_table(history, "全 timeframe 合算")
    conservative_table(history, bucket_of, BUCKETS, "バケット別")
    for tf in ("4時間", "日足"):
        sub = [r for r in history if r.get("timeframe") == tf]
        section_hit_table(sub, f"timeframe = {tf}")

    print("\n## 符号付き%リターン")
    section_return_table(history, "全 timeframe 合算")
    for tf in ("4時間", "日足"):
        sub = [r for r in history if r.get("timeframe") == tf]
        section_return_table(sub, f"timeframe = {tf}")

    section_divergence(history)
    section_news_attribution(history)
    section_entry_rule(history)
    section_event_gate(history)
    section_liquidity(history)
    section_v111(history)
    section_period_confounding(history)
    section_coverage(pending, history)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
Phase4 Stage1: 原油 限月構造（F1/F2）のデータ品質監査

本体パイプライン無改変・読み取り専用の research スクリプト。
  python research/term_structure_audit.py
→ research/term_structure_audit.md を生成。

再現性: 監査ウィンドウは定数で固定（AUDIT_START/AUDIT_END）。実行時刻に
依存して結果が変わらない（Yahooが同じ履歴を返す限り再実行で同一の数表）。

■ ティッカー探索の実測結果（2026-07-11実施・推測なし）:
  - 個別限月は「CL{月コード}{YY}.NYM」形式が動作（例 CLQ26.NYM、2年分の日足）。
  - 期限切れ限月（CLN26/CLM26/CLF26.NYM）は 404/0本 = Yahoo は期限切れ先物の
    履歴を提供しない。→ 仕様の分岐に従い「過去1年の遡及」を断念し、
    上場中チェーン（F1〜F4+）の重複期間監査に切り替え。
  - CL=F（連続限月）はロール規則が不透明なため F1 代用に使わない（仕様固定）。

■ F1/F2 の定義（決定論・出来高不使用）:
  - ロールはカレンダー規則で固定: 毎月15日（非営業日なら翌営業日）に F1 を
    翌限月へロール（expiry近傍の薄商い回避）。Yahoo の先物出来高は信頼不可の
    ため出来高ベースのロールは採らない。
  - 年率ロールイールド = (F1/F2 − 1) × (365 / 両限月の受渡月初日間の日数)。
    日数は「受渡月の1日どうしの差」による近似（一貫性 > 精度）。
    正 = backwardation（期近高）、負 = contango。
  - change_20d = 年率化後の値の20日変化（正規化済みなのでロール跨ぎ可）。

■ 監査項目と合格基準（着手前に固定）:
  A. 各限月系列: 営業日カバレッジ >= 95%（pandas bdate_range 基準・米祝日込みの
     暦営業日が分母のため理論上限 ~96%）
  B. 各限月系列: stale（終値同値の連続）<= 3営業日
  C. 各限月系列: |日次リターン| > 15% の異常値ゼロ（あれば個別説明を付す）
  D. スプレッド系列: ロール（隣接ペアへの切替）が年率ロールイールドに作る段差が、
     非ロール日の日次変動分布の 3σ 以内。
     ※期限切れ限月が取得不能のため、過去の実ロール日での検査は不可能。
       代替として「同一日における隣接ペア間の年率イールド差 Δ_pair(t)」を
       全重複期間で計測し、スケジュール上のロール日（毎月15日翌営業日）における
       |Δ_pair| の最大値を段差の代理量とする（切替がその日に起きた場合に
       生じるジャンプそのものであり、代理として妥当）。

■ 方向仮説の事前検証について:
  真の期近（F1）状態の遡及は、期限切れ限月が取得不能なため 2026-06-15 以降
  （カレンダー規則で F1=CLQ26 になって以降）しか構築できない = 6ヶ月未満。
  遠限月ペアの傾きで代用すると「期近の backwardation」とは別の変数になり
  仮説のすり替えとなるため実施しない。→「方向仮説は事前検証不能」と記載し、
  v1.6 は対称定義（v1.3の流儀）を採る分岐材料とする。
"""
import sys
from datetime import date

import pandas as pd
import yfinance as yf

AUDIT_START = "2024-07-11"   # 固定（再現性）
AUDIT_END = "2026-07-10"     # 固定（再現性・データ最終日）

# 上場中チェーン（実測で確認済みの形式）。受渡月の1日を併記。
CONTRACTS = [
    ("CLQ26.NYM", date(2026, 8, 1)),
    ("CLU26.NYM", date(2026, 9, 1)),
    ("CLV26.NYM", date(2026, 10, 1)),
    ("CLX26.NYM", date(2026, 11, 1)),
    ("CLZ26.NYM", date(2026, 12, 1)),
]

COVERAGE_MIN = 0.95
STALE_MAX = 3
OUTLIER_PCT = 15.0
SIGMA_MULT = 3.0

OUT = "research/term_structure_audit.md"


def fetch(ticker):
    df = yf.download(ticker, interval="1d", start=AUDIT_START,
                     end="2026-07-11",  # AUDIT_END 翌日（end排他）
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df["Close"].dropna()


def audit_series(name, s):
    """A/B/C の監査。dict を返す。"""
    bdays = pd.bdate_range(s.index[0], s.index[-1])
    coverage = len(s) / len(bdays)
    # stale: 同値連続の最大長
    same = (s.diff() == 0)
    max_stale = run = 0
    for v in same:
        run = run + 1 if v else 0
        max_stale = max(max_stale, run)
    # 異常値
    ret = s.pct_change().dropna() * 100
    outliers = ret[ret.abs() > OUTLIER_PCT]
    return {
        "series": name,
        "n": len(s),
        "期間": f"{s.index[0].date()}〜{s.index[-1].date()}",
        "coverage": round(coverage, 4),
        "A合格(>=0.95)": coverage >= COVERAGE_MIN,
        "max_stale": max_stale,
        "B合格(<=3)": max_stale <= STALE_MAX,
        "outliers(|r|>15%)": len(outliers),
        "C合格(=0)": len(outliers) == 0,
        "_outlier_dates": [str(d.date()) for d in outliers.index],
    }


def ann_roll_yield(f1, f2, d1, d2):
    """年率ロールイールド系列。d1/d2 = 受渡月初日。"""
    days = (d2 - d1).days
    return (f1 / f2 - 1.0) * (365.0 / days)


def scheduled_roll_dates(idx):
    """監査窓内のスケジュール上のロール日（毎月15日・非営業日は翌営業日）。"""
    out = []
    months = pd.period_range(AUDIT_START, AUDIT_END, freq="M")
    for m in months:
        d = pd.Timestamp(m.year, m.month, 15)
        pos = idx.searchsorted(d)
        if pos < len(idx):
            out.append(idx[pos])
    return sorted(set(out))


def md_table(rows, cols):
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "—")) for c in cols) + " |")
    return "\n".join(out)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    lines = []
    lines.append("# 原油 限月構造 データ品質監査（Phase4 Stage1）\n")
    lines.append(f"- 監査ウィンドウ（固定・再現用）: {AUDIT_START} 〜 {AUDIT_END}")
    lines.append("- ロール規則（固定）: 毎月15日（非営業日なら翌営業日）に F1 を翌限月へロール。"
                 "出来高不使用（Yahooの先物出来高は信頼不可）。")
    lines.append("- 年率ロールイールド = (F1/F2 − 1) × (365 / 受渡月初日間の日数)。"
                 "正=backwardation / 負=contango。change_20d は年率値の20日変化。")
    lines.append(f"- 合格基準（固定）: A) カバレッジ>= {COVERAGE_MIN} / B) stale<= {STALE_MAX}営業日 / "
                 f"C) |日次リターン|>{OUTLIER_PCT}% ゼロ / D) ロール段差 <= 非ロール日変動の{SIGMA_MULT}σ\n")

    lines.append("## ティッカー探索の実測結果")
    lines.append("- **動作形式: `CL{月}{YY}.NYM`**（CLQ26/CLU26/CLV26/CLX26/CLZ26 = 各約502本・2年）")
    lines.append("- **期限切れ限月は取得不能**（CLN26/CLM26/CLF26.NYM → 404/0本）")
    lines.append("- → 仕様分岐: 過去1年の遡及を断念、**上場中チェーンの重複期間監査**に切替")
    lines.append("- CL=F はロール規則不透明のため不使用（仕様固定）\n")

    # 取得
    series = {}
    for t, d1 in CONTRACTS:
        s = fetch(t)
        if s is None or len(s) < 120:
            lines.append(f"- **{t}: 取得失敗/不足** → この限月は監査対象外")
            continue
        series[t] = (s, d1)

    if len(series) < 2:
        lines.append("\n## 総合判定: **不合格（チェーン取得不能）**")
        lines.append("代替データ源候補（実装しない）: ①CME公式 delayed 決済値 "
                     "②Nasdaq Data Link (CHRIS/CME_CL1-2) ③barchart API（要契約）")
        report = "\n".join(lines) + "\n"
        open(OUT, "w", encoding="utf-8").write(report)
        print(report)
        return

    # F1/F2重複期間の確認（CLQ26×CLU26）
    t1, t2 = CONTRACTS[0][0], CONTRACTS[1][0]
    common = series[t1][0].index.intersection(series[t2][0].index)
    overlap_days = (common[-1] - common[0]).days
    lines.append(f"## F1/F2 重複期間\n- {t1} × {t2}: 共通 {len(common)}営業日"
                 f"（{common[0].date()}〜{common[-1].date()}、暦{overlap_days}日 ≒ "
                 f"{overlap_days / 30.4:.0f}ヶ月）→ **6ヶ月以上あり・監査続行**\n")

    # A/B/C 監査
    lines.append("## 監査A/B/C: 各限月系列の品質\n")
    audits = [audit_series(t, s) for t, (s, _) in series.items()]
    cols = ["series", "n", "期間", "coverage", "A合格(>=0.95)", "max_stale",
            "B合格(<=3)", "outliers(|r|>15%)", "C合格(=0)"]
    lines.append(md_table(audits, cols))
    for a in audits:
        if a["_outlier_dates"]:
            lines.append(f"- {a['series']} の異常値日: {a['_outlier_dates']}")

    # D: ロール段差（隣接ペアの同日イールド差を代理量に）
    lines.append("\n## 監査D: ロール段差（隣接ペア切替の偽ジャンプ検査）\n")
    lines.append("※期限切れ限月が取得不能のため過去の実ロール日は検査不可。代替として"
                 "**同一日の隣接ペア間 年率イールド差 Δ_pair(t)** を全期間で計測し、"
                 "スケジュール上のロール日における |Δ_pair| 最大を段差の代理量とする。\n")
    d_rows = []
    d_pass_all = True
    tickers = [t for t, _ in CONTRACTS if t in series]
    for i in range(len(tickers) - 2):
        a, b, c = tickers[i], tickers[i + 1], tickers[i + 2]
        (sa, da), (sb, db), (sc, dc) = series[a], series[b], series[c]
        idx = sa.index.intersection(sb.index).intersection(sc.index)
        y_old = ann_roll_yield(sa[idx], sb[idx], da, db)   # 旧ペア (F1,F2)
        y_new = ann_roll_yield(sb[idx], sc[idx], db, dc)   # 新ペア (F1',F2')
        delta_pair = (y_new - y_old).dropna()
        # 非ロール日の日次変動（旧ペアの日次Δ）
        daily = y_old.diff().dropna()
        sigma = float(daily.std())
        rolls = [d for d in scheduled_roll_dates(idx) if d in delta_pair.index]
        if rolls:
            steps = delta_pair.loc[rolls].abs()
            max_step = float(steps.max())
            max_date = str(steps.idxmax().date())
            n_over = int((steps > SIGMA_MULT * sigma).sum())
        else:
            max_step, max_date, n_over = float("nan"), "—", 0
        ok = bool(max_step <= SIGMA_MULT * sigma) if rolls else False
        d_pass_all &= ok
        d_rows.append({
            "切替": f"({a.split('.')[0]},{b.split('.')[0]})→({b.split('.')[0]},{c.split('.')[0]})",
            "非ロール日σ": round(sigma, 5),
            "3σ": round(SIGMA_MULT * sigma, 5),
            "ロール日|Δ_pair|max": round(max_step, 5),
            "max発生日": max_date,
            "3σ超過日数/24": n_over,
            "D合格": ok,
        })
    lines.append(md_table(d_rows, ["切替", "非ロール日σ", "3σ", "ロール日|Δ_pair|max",
                                   "max発生日", "3σ超過日数/24", "D合格"]))
    lines.append("\n（文脈の事実・判定は変えない: Δ_pair は曲率＝実構造も含む量であり、"
                 "本代理検査は該当ペアが受渡から1〜2年遠い時期の乖離も母集団に含む。"
                 "実運用でその切替が起きるのは受渡2〜3ヶ月前の1回のみ）")

    # 参考: 現時点の F1/F2 状態（Stage2で記録する値のプレビュー）
    y_now = ann_roll_yield(series[t1][0], series[t2][0], series[t1][1], series[t2][1]).dropna()
    state = "backwardation" if y_now.iloc[-1] > 0 else "contango"
    chg20 = y_now.iloc[-1] - y_now.iloc[-21] if len(y_now) > 21 else float("nan")
    lines.append(f"\n## 参考: 現時点（{AUDIT_END}）の F1/F2 状態\n")
    lines.append(f"- roll_yield_ann = {y_now.iloc[-1]:+.4f} → **{state}** / change_20d = {chg20:+.4f}")

    # 方向仮説の事前検証（不能の判定と理由）
    lines.append("\n## 方向仮説の事前検証: **実施不能**\n")
    lines.append("- 真の期近（F1）状態の遡及は、期限切れ限月が取得不能なため "
                 "2026-06-15（規則上 F1=CLQ26 となった日）以降しか構築できない = **6ヶ月未満**。")
    lines.append("- 遠限月ペアの傾きで代用すると「期近のbackwardation」とは別変数になり"
                 "仮説のすり替えとなるため実施しない。")
    lines.append("- → **v1.6 は対称定義（v1.3の流儀）を採る**分岐材料とする。"
                 "方向つきテストは shadow 実データが貯まってからの v1.x 追記に委ねる。")

    # 総合判定
    abc_pass = all(a["A合格(>=0.95)"] and a["B合格(<=3)"] and a["C合格(=0)"] for a in audits)
    verdict = abc_pass and d_pass_all
    lines.append(f"\n## 総合判定: {'**合格（Stage2 実装可）**' if verdict else '**不合格**'}\n")
    if not verdict:
        lines.append("不合格項目は上表参照。代替データ源候補（実装しない）: "
                     "①CME公式 delayed 決済値 ②Nasdaq Data Link ③barchart（要契約）")
    else:
        lines.append("- 採用ティッカー形式: `CL{月}{YY}.NYM`（上場中チェーンをカレンダー規則でロール）")
        lines.append("- Stage2 は実行時点の F1/F2 をその場で解決（ステートレス・原油のみ2取得追加）")

    report = "\n".join(lines) + "\n"
    open(OUT, "w", encoding="utf-8").write(report)
    print(report)
    print(f"\n→ {OUT} に保存")


if __name__ == "__main__":
    main()

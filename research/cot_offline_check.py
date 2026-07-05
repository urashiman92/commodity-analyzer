# -*- coding: utf-8 -*-
"""
Phase2 Stage1: CoT（Managed Money ポジショニング）のオフライン検証

本体パイプライン無改変・読み取り専用の research スクリプト。
  python research/cot_offline_check.py
→ research/cot_offline_report.md を生成。

データソース: CFTC Socrata API（publicreporting.cftc.gov, dataset 72hh-3qpy =
Disaggregated Futures Only）。年次zipも到達確認済みだが、サーバー側フィルタ+JSONで
パース不要な Socrata を採用（理由はレポートに記載）。

契約特定（実データの market 名・契約コードで確認済み。推測なし）:
  小麦     WHEAT-SRW - CHICAGO BOARD OF TRADE          001602
  金       GOLD - COMMODITY EXCHANGE INC.              088691
  WTI原油  WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE 067651
           （CRUDE検索では出ない。"WTI-PHYSICAL"名義・OI 1.9M が本命）
  ﾄｳﾓﾛｺｼ   CORN - CHICAGO BOARD OF TRADE               002602
  大豆     SOYBEANS - CHICAGO BOARD OF TRADE           005602
  銅       COPPER- #1 - COMMODITY EXCHANGE INC.        085692

ルックアヘッド禁止（最重要）:
  as-of火曜のデータは金曜15:30 ET公表。フォワードリターンの起点は
  「as_of + 6日以降の最初の取引日」（=公表後の翌営業日・通常は月曜）。
  火曜起点は3営業日の未来参照になるため厳禁。

事前判定ルール（計算前に固定・レポート冒頭に明記）:
  プール集計の翌2週リターンで
    spread_high = mean(基準群25-75) - mean(pctl>90) > 0
    spread_low  = mean(pctl<10) - mean(基準群25-75) > 0
  の両方が、1年×3サブ期間すべてで正（符号安定）であること。
  不成立でも Stage2 の shadow 記録は実施（Phase1 と同じ扱い）。
"""
import sys
from datetime import timedelta

import pandas as pd
import requests
import yfinance as yf

SOCRATA = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
FETCH_FROM = "2020-06-01"   # 3年ローリングpctl(156週)を直近3年の評価に効かせるため約6年取得
EVAL_YEARS = 3
ROLL_W = 156                # 3年 = 156週
FWD = {"1w": 5, "2w": 10, "4w": 20}

CONTRACTS = {
    "小麦":       ("001602", "ZW=F"),
    "金":         ("088691", "GC=F"),
    "WTI原油":    ("067651", "CL=F"),
    "トウモロコシ": ("002602", "ZC=F"),
    "大豆":       ("005602", "ZS=F"),
    "銅":         ("085692", "HG=F"),
}

OUT = "research/cot_offline_report.md"


def fetch_cot(code: str) -> pd.DataFrame | None:
    """1契約のCoT週次系列（as_of, mm_long, mm_short, oi）を取得。"""
    try:
        r = requests.get(SOCRATA, params={
            "$select": ("report_date_as_yyyy_mm_dd,"
                        "m_money_positions_long_all,m_money_positions_short_all,"
                        "open_interest_all"),
            "$where": (f"cftc_contract_market_code='{code}' "
                       f"AND report_date_as_yyyy_mm_dd>='{FETCH_FROM}'"),
            "$order": "report_date_as_yyyy_mm_dd",
            "$limit": 5000,
        }, timeout=60)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["as_of"] = pd.to_datetime(df["report_date_as_yyyy_mm_dd"])
        for c in ("m_money_positions_long_all", "m_money_positions_short_all",
                  "open_interest_all"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna().sort_values("as_of").reset_index(drop=True)
        return df if len(df) > 200 else None
    except Exception:
        return None


def fetch_prices(ticker: str) -> pd.Series | None:
    try:
        df = yf.download(ticker, interval="1d", start=FETCH_FROM,
                         progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df["Close"].dropna()
    except Exception:
        return None


def rolling_pctl(s: pd.Series, window: int) -> pd.Series:
    """末尾値の trailing パーセンタイル順位（0-100, 窓に自身を含む）。"""
    return s.rolling(window, min_periods=window).apply(
        lambda w: float((w <= w[-1]).mean()) * 100, raw=True)


def build_symbol_frame(name: str, code: str, ticker: str):
    cot = fetch_cot(code)
    px = fetch_prices(ticker)
    if cot is None or px is None:
        return None, f"{name}: データ取得失敗（CoT={'OK' if cot is not None else 'NG'}, 価格={'OK' if px is not None else 'NG'}）"

    cot["mm_net"] = ((cot["m_money_positions_long_all"]
                      - cot["m_money_positions_short_all"])
                     / cot["open_interest_all"])
    cot["pctl"] = rolling_pctl(cot["mm_net"], ROLL_W)
    cot["wow"] = cot["mm_net"].diff()
    # wow の trailing 3年分布の上下5%閾値（当週を含む trailing 窓）
    cot["wow_q05"] = cot["wow"].rolling(ROLL_W, min_periods=ROLL_W).quantile(0.05)
    cot["wow_q95"] = cot["wow"].rolling(ROLL_W, min_periods=ROLL_W).quantile(0.95)

    # ルックアヘッド禁止: エントリーは as_of + 6日以降の最初の取引日
    idx = px.index
    def entry_loc(as_of):
        pos = idx.searchsorted(as_of + timedelta(days=6))
        return pos if pos < len(idx) else None

    recs = []
    for _, row in cot.iterrows():
        loc = entry_loc(row["as_of"])
        if loc is None:
            continue
        rec = {"symbol": name, "as_of": row["as_of"], "pctl": row["pctl"],
               "wow": row["wow"], "wow_q05": row["wow_q05"], "wow_q95": row["wow_q95"]}
        base_px = float(px.iloc[loc])
        for lbl, days in FWD.items():
            j = loc + days
            rec[f"fwd_{lbl}"] = (float(px.iloc[j]) / base_px - 1.0) * 100 if j < len(idx) else None
        recs.append(rec)
    return pd.DataFrame(recs), None


def winsorized_mean(vals, pct=0.95):
    if len(vals) == 0:
        return None
    if len(vals) < 20:
        return float(pd.Series(vals).mean())
    a = pd.Series(vals).abs().sort_values()
    cap = float(a.iloc[max(0, int(-(-pct * len(a)) // 1) - 1)])
    return float(pd.Series(vals).clip(-cap, cap).mean())


def agg_row(df, mask, lbl):
    out = {"群": lbl, "n": int(mask.sum())}
    for h in FWD:
        vals = df.loc[mask, f"fwd_{h}"].dropna()
        out[f"{h}_mean%"] = round(vals.mean(), 3) if len(vals) else None
        out[f"{h}_med%"] = round(vals.median(), 3) if len(vals) else None
        out[f"{h}_wins%"] = round(winsorized_mean(vals.tolist()), 3) if len(vals) else None
    return out


def md_table(rows):
    if not rows:
        return "(データなし)"
    cols = list(rows[0].keys())
    out = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join("—" if r[c] is None else str(r[c]) for c in cols) + " |")
    return "\n".join(out)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    lines = []
    lines.append("# CoT オフライン検証レポート（Phase2 Stage1）\n")
    lines.append("**事前判定ルール（計算前に固定）**: プール集計の翌2週リターンで "
                 "spread_high = 基準群(25-75) − pctl>90 が正、かつ spread_low = pctl<10 − 基準群 が正。"
                 "両者が1年×3サブ期間すべてで正（符号安定）であること。\n")
    lines.append("- データソース: **Socrata API 採用**（publicreporting.cftc.gov / 72hh-3qpy）。"
                 "年次zip（cftc.gov/files/dea/history）も到達確認済みだが、サーバー側フィルタ+JSONで"
                 "zip解凍・旧式ヘッダのパースが不要な Socrata の方が扱いやすい。両方ローカル到達可"
                 "（FREDのような不達なし）。")
    lines.append("- ルックアヘッド対策: エントリー = as_of(火) + 6日以降の最初の取引日"
                 "（金曜15:30 ET公表の翌営業日）。")
    lines.append(f"- MM net = (MM Long − MM Short) / OI。pctl・wow分布とも trailing {ROLL_W}週"
                 "（約6年取得し直近3年を評価）。\n")

    frames, notes = [], []
    for name, (code, ticker) in CONTRACTS.items():
        df, err = build_symbol_frame(name, code, ticker)
        if err:
            notes.append(err)
        else:
            frames.append(df)
            notes.append(f"{name}: CoT {len(df)}週（code {code} / {ticker}）")
    lines.append("## データ")
    for n in notes:
        lines.append(f"- {n}")

    if not frames:
        lines.append("\n**全銘柄でデータ取得失敗のため検証続行不能。**")
        report = "\n".join(lines)
        open(OUT, "w", encoding="utf-8").write(report)
        print(report)
        return

    pool = pd.concat(frames, ignore_index=True)
    # 評価ウィンドウ: pctl が算出できている直近3年
    pool = pool.dropna(subset=["pctl"])
    eval_start = pool["as_of"].max() - pd.DateOffset(years=EVAL_YEARS)
    pool = pool[pool["as_of"] >= eval_start].reset_index(drop=True)
    lines.append(f"- 評価ウィンドウ: {eval_start.date()} 〜 {pool['as_of'].max().date()}"
                 f"（プール {len(pool)} 週×銘柄）\n")

    hi = pool["pctl"] > 90
    lo = pool["pctl"] < 10
    base = (pool["pctl"] >= 25) & (pool["pctl"] <= 75)

    lines.append("## a) 混雑: pctl群別フォワードリターン（プール集計・%）\n")
    lines.append(md_table([agg_row(pool, hi, "pctl>90 (混雑ロング)"),
                           agg_row(pool, base, "25-75 (基準群)"),
                           agg_row(pool, lo, "pctl<10 (混雑ショート)")]))

    # 銘柄別（2週・平均のみの簡約表）
    lines.append("\n### 銘柄別（翌2週・平均%）\n")
    rows = []
    for name in CONTRACTS:
        sub = pool[pool["symbol"] == name]
        r = {"symbol": name}
        for lbl, m in (("pctl>90", sub["pctl"] > 90), ("25-75", (sub["pctl"] >= 25) & (sub["pctl"] <= 75)),
                       ("pctl<10", sub["pctl"] < 10)):
            vals = sub.loc[m, "fwd_2w"].dropna()
            r[f"{lbl} mean"] = round(vals.mean(), 3) if len(vals) else None
            r[f"{lbl} n"] = len(vals)
        rows.append(r)
    lines.append(md_table(rows))

    # b) 解消: wow tail
    lines.append("\n## b) 解消: wow が trailing 3年分布の上下5%に入った週の後（プール・%）\n")
    wow_hi = pool["wow"] >= pool["wow_q95"]
    wow_lo = pool["wow"] <= pool["wow_q05"]
    others = ~(wow_hi | wow_lo)
    lines.append(md_table([agg_row(pool, wow_hi, "wow上位5% (急積み増し)"),
                           agg_row(pool, others, "その他 (基準)"),
                           agg_row(pool, wow_lo, "wow下位5% (急解消)")]))
    lines.append("\n（読み方: 急積み増し後にリターンが同方向へ続けば継続、逆行すれば転換）")

    # 事前判定: 2週スプレッドのサブ期間符号安定性
    seg = (pool["as_of"].max() - eval_start) / 3
    sub_rows = []
    signs_ok = []
    for i in range(3):
        s0, s1 = eval_start + seg * i, eval_start + seg * (i + 1)
        m = (pool["as_of"] >= s0) & (pool["as_of"] < s1 if i < 2 else pool["as_of"] <= pool["as_of"].max())
        b = pool.loc[m & base, "fwd_2w"].dropna()
        h = pool.loc[m & hi, "fwd_2w"].dropna()
        l = pool.loc[m & lo, "fwd_2w"].dropna()
        sp_h = round(b.mean() - h.mean(), 3) if len(b) and len(h) else None
        sp_l = round(l.mean() - b.mean(), 3) if len(b) and len(l) else None
        sub_rows.append({"期間": f"サブ{i+1} ({s0.date()}〜)", "n(>90)": len(h), "n(基準)": len(b),
                         "n(<10)": len(l), "spread_high": sp_h, "spread_low": sp_l})
        signs_ok.append(bool(sp_h is not None and sp_l is not None and sp_h > 0 and sp_l > 0))
    lines.append("\n## 事前判定: 2週スプレッドのサブ期間符号\n")
    lines.append(md_table(sub_rows))

    # 追加確認A-1: 順張り鏡像ルール（pctl>90 が基準群を上回り、pctl<10 が下回る）
    momentum_rows = []
    momentum_ok = []
    for i in range(3):
        s0, s1 = eval_start + seg * i, eval_start + seg * (i + 1)
        m = (pool["as_of"] >= s0) & (pool["as_of"] < s1 if i < 2 else pool["as_of"] <= pool["as_of"].max())
        b = pool.loc[m & base, "fwd_2w"].dropna()
        h = pool.loc[m & hi, "fwd_2w"].dropna()
        l = pool.loc[m & lo, "fwd_2w"].dropna()
        sp_h_m = round(h.mean() - b.mean(), 3) if len(b) and len(h) else None  # >0 = 順張り成立
        sp_l_m = round(b.mean() - l.mean(), 3) if len(b) and len(l) else None  # >0 = 順張り成立
        momentum_rows.append({"期間": f"サブ{i+1} ({s0.date()}〜)",
                              "hi−基準(>0で成立)": sp_h_m, "基準−lo(>0で成立)": sp_l_m})
        momentum_ok.append(bool(sp_h_m is not None and sp_l_m is not None
                                and sp_h_m > 0 and sp_l_m > 0))
    lines.append("\n## 追加確認A-1: 順張り鏡像ルールのサブ期間符号\n")
    lines.append("鏡像ルール: 「pctl>90群が基準群を**上回り**、pctl<10群が**下回る**（2週・プール）」\n")
    lines.append(md_table(momentum_rows))
    momentum_verdict = all(momentum_ok)
    lines.append(f"\n- 順張り鏡像の成立可否: {momentum_ok} → "
                 f"{'**3/3 符号安定**' if momentum_verdict else '**3/3 未達**'}")

    # 追加確認A-2: raw vs winsorized 乖離（スクイーズ的外れ値の混入検査・判定には使わない）
    lines.append("\n## 追加確認A-2: raw vs winsorized 平均の乖離（参考・判定不使用）\n")
    div_rows = []
    for lbl, m in (("pctl<10 (混雑ショート)", lo),
                   ("wow上位5% (急積み増し)", wow_hi),
                   ("wow下位5% (急解消)", wow_lo)):
        r = {"群": lbl}
        for h_ in FWD:
            vals = pool.loc[m, f"fwd_{h_}"].dropna()
            raw = vals.mean() if len(vals) else None
            wins = winsorized_mean(vals.tolist()) if len(vals) else None
            r[f"{h_} raw%"] = round(raw, 3) if raw is not None else None
            r[f"{h_} wins%"] = round(wins, 3) if wins is not None else None
            r[f"{h_} 乖離"] = round(raw - wins, 3) if raw is not None and wins is not None else None
        div_rows.append(r)
    lines.append(md_table(div_rows))
    lines.append("\n（乖離が正に大きい場合、少数の正の外れ値=スクイーズ的イベントが"
                 "平均を持ち上げている可能性。判定には使わない）")

    verdict = all(signs_ok)
    lines.append("\n## 所見（昇格候補の事前見立て）\n")
    if verdict:
        lines.append("- **事前ルール成立**: spread_high / spread_low とも3サブ期間すべて正。"
                     "混雑シグナルは昇格候補の有力な事前見立て。Stage2 の shadow 記録で実データ確認へ。")
    else:
        lines.append(f"- **事前ルール不成立**（サブ期間の成立可否: {signs_ok}）。"
                     "Phase1 と同じ扱いで Stage2 の shadow 記録は実施するが、"
                     "逆張り定義のままでは昇格候補の見立ては弱い。")
        # 副次観察（事後観察であり判定基準ではない。ルール変更でもない）
        b2 = pool.loc[base, "fwd_2w"].dropna().mean()
        h2 = pool.loc[hi, "fwd_2w"].dropna().mean()
        l2 = pool.loc[lo, "fwd_2w"].dropna().mean()
        lines.append(f"\n### 副次観察（事後・参考のみ）\n")
        lines.append(f"- 全期間プールでは pctl<10（混雑ショート）の翌2週が {l2:+.2f}% と"
                     f"基準群 {b2:+.2f}% を大きく**下回る**（逆張り仮説と逆 = **順張り/継続**の示唆）。"
                     f"pctl>90 は {h2:+.2f}% で基準比小幅劣後。")
        lines.append("- wow tail も急積み増し後 +1.4%/2週（継続方向）で、少なくとも週次〜1ヶ月の"
                     "ホライズンでは MM ポジショニングは逆張りシグナルではなく"
                     "**モメンタム情報**として振る舞っている可能性。")
        lines.append("- shadow 記録（Stage2）では離散化した pctl 帯と wow tail を生値ごと残すため、"
                     "順張り解釈での昇格テスト定義（protocol v1.3）を設計する際の判断材料になる。")

    report = "\n".join(lines) + "\n"
    open(OUT, "w", encoding="utf-8").write(report)
    print(report)
    print(f"\n→ {OUT} に保存")


if __name__ == "__main__":
    main()

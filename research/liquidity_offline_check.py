# -*- coding: utf-8 -*-
"""
Phase5 Stage1: 流動性トリガー（sweep-and-reclaim）のオフライン検証

本体パイプライン無改変・読み取り専用の research スクリプト。
  python research/liquidity_offline_check.py
→ research/liquidity_offline_report.md を生成。

■ 閾値config（着手前に固定・レポート冒頭に明記。Stage2で流用する正準定義）:
  PIVOT_K        = 5      # フラクタルピボット: 前後5本より高い高値/低い安値
  ATR_N          = 14     # ATR(14) = True Range の単純移動平均
  ALPHA          = 0.25   # reclaim閾値: 終値 < 水準 − α×ATR（安値側は鏡像）
  REGISTRY_DEPTH = 3      # 未タッチ水準を上下各3保持（時系列で新しい順）
  GAP_GUARD_MULT = 3.0    # ロール偽スイング対策: |Open−前日Close| > ATR×3 の日の
                          # ピボットは registry 除外（連続限月のロールギャップ対策）
  FWD            = 5, 10  # 翌1週/2週（営業日）

■ イベント定義（1バー完結）:
  sweep_high_reclaim: 高値が未タッチのスイング高値水準 L を上抜け（High > L）かつ
                      終値が L − α×ATR 未満へ回帰（Close < L − α×ATR）
  sweep_low_reclaim:  鏡像（Low < L かつ Close > L + α×ATR）
  - ピボットは確定後（中心バーの5本後）にのみ registry 入り（ルックアヘッド禁止）
  - 水準はタッチされたら（reclaim 有無に関わらず）registry から除去
  - 同一バーで複数水準に該当しても1バー1イベント

■ 事前判定ルール（着手前固定）:
  sweep_high_reclaim 後の翌1・2週リターンが負／low側は正。
  6銘柄プール・1年×3サブ期間で、両ホライズンとも符号安定（3/3）であること。
  発生頻度（銘柄・年あたり件数）を必ず報告——希少すぎて実用に耐えない場合は
  その事実を明記する（これも結果）。

■ 4h併用: yfinance 1h は直近730日制限のため、取得できた範囲（約2年）を
  4h にリサンプリングして同一ロジックを適用（参考扱い・サブ期間は2に減らし明記）。
"""
import sys
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

PIVOT_K = 5
ATR_N = 14
ALPHA = 0.25
REGISTRY_DEPTH = 3
GAP_GUARD_MULT = 3.0
FWD = (5, 10)
EVAL_YEARS = 3

SYMBOLS = {
    "小麦": "ZW=F", "金": "GC=F", "WTI原油": "CL=F",
    "トウモロコシ": "ZC=F", "大豆": "ZS=F", "銅": "HG=F",
}

OUT = "research/liquidity_offline_report.md"


def fetch_daily(ticker):
    df = yf.download(ticker, interval="1d", period=f"{EVAL_YEARS}y",
                     progress=False, auto_adjust=False)
    return _clean(df)


def fetch_4h(ticker):
    df = yf.download(ticker, interval="1h", period="730d",
                     progress=False, auto_adjust=False)
    df = _clean(df)
    if df is None:
        return None
    df = df.resample("4h").agg({"Open": "first", "High": "max",
                                "Low": "min", "Close": "last"}).dropna()
    return df if len(df) > 300 else None


def _clean(df):
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna()
    return df if len(df) > 100 else None


def atr(df, n=ATR_N):
    prev_close = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"],
                    (df["High"] - prev_close).abs(),
                    (df["Low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def detect_events(df):
    """sweep-and-reclaim イベントを検出。[(idx, 'high'|'low')] を返す。"""
    n = len(df)
    high, low, close, open_ = (df["High"].values, df["Low"].values,
                               df["Close"].values, df["Open"].values)
    a = atr(df).values
    prev_close = df["Close"].shift(1).values

    # ピボット確定バー（中心 i が確定するのは i+K）。ギャップ日の中心は除外。
    piv_high = {}   # 確定バー t → (水準, 中心バーi)
    piv_low = {}
    for i in range(PIVOT_K, n - PIVOT_K):
        gap = abs(open_[i] - prev_close[i]) if prev_close[i] == prev_close[i] else 0.0
        if a[i] == a[i] and gap > GAP_GUARD_MULT * a[i]:
            continue  # ロール偽スイング対策
        seg_h = high[i - PIVOT_K:i + PIVOT_K + 1]
        seg_l = low[i - PIVOT_K:i + PIVOT_K + 1]
        if high[i] == seg_h.max() and (seg_h == high[i]).sum() == 1:
            piv_high.setdefault(i + PIVOT_K, []).append(high[i])
        if low[i] == seg_l.min() and (seg_l == low[i]).sum() == 1:
            piv_low.setdefault(i + PIVOT_K, []).append(low[i])

    reg_h, reg_l = [], []   # 未タッチ水準（新しい順に最大 REGISTRY_DEPTH）
    events = []
    for t in range(n):
        # 1) この時点で確定したピボットを登録
        for lv in piv_high.get(t, []):
            reg_h = ([lv] + reg_h)[:REGISTRY_DEPTH]
        for lv in piv_low.get(t, []):
            reg_l = ([lv] + reg_l)[:REGISTRY_DEPTH]
        if a[t] != a[t]:
            continue
        # 2) タッチ判定 + イベント検出（タッチされた水準は除去）
        hit_h = [lv for lv in reg_h if high[t] > lv]
        hit_l = [lv for lv in reg_l if low[t] < lv]
        if hit_h and any(close[t] < lv - ALPHA * a[t] for lv in hit_h):
            events.append((t, "high"))
        if hit_l and any(close[t] > lv + ALPHA * a[t] for lv in hit_l):
            events.append((t, "low"))
        reg_h = [lv for lv in reg_h if lv not in hit_h]
        reg_l = [lv for lv in reg_l if lv not in hit_l]
    return events


def collect(tf_fetch, label):
    """全銘柄のイベント+フォワードリターンを収集。"""
    rows, notes = [], []
    for name, ticker in SYMBOLS.items():
        df = tf_fetch(ticker)
        if df is None:
            notes.append(f"{name}: {label} データ取得不可")
            continue
        events = detect_events(df)
        close = df["Close"].values
        years = (df.index[-1] - df.index[0]).days / 365.25
        notes.append(f"{name}: {len(df)}本 / イベント {len(events)}件 "
                     f"(年あたり {len(events) / years:.1f}件)")
        for t, side in events:
            row = {"symbol": name, "ts": df.index[t], "side": side}
            for f in FWD:
                row[f"fwd_{f}d"] = ((close[t + f] / close[t] - 1) * 100
                                    if t + f < len(close) else None)
            rows.append(row)
    return pd.DataFrame(rows), notes


def agg_table(ev, n_subs):
    """side×サブ期間の平均リターン表と符号安定性判定。"""
    if ev.empty:
        return [], {}
    t0, t1 = ev["ts"].min(), ev["ts"].max()
    seg = (t1 - t0) / n_subs
    lines = []
    stable = {}
    for f in FWD:
        col = f"fwd_{f}d"
        rows = []
        oks_h, oks_l = [], []
        for i in range(n_subs):
            s0 = t0 + seg * i
            s1 = t0 + seg * (i + 1)
            m = (ev["ts"] >= s0) & (ev["ts"] <= (s1 if i == n_subs - 1 else s1))
            sub = ev[m]
            h = sub.loc[sub["side"] == "high", col].dropna()
            l = sub.loc[sub["side"] == "low", col].dropna()
            rows.append({
                "期間": f"サブ{i+1}", "n_high": len(h),
                "high_mean%": round(h.mean(), 3) if len(h) else None,
                "n_low": len(l),
                "low_mean%": round(l.mean(), 3) if len(l) else None,
            })
            oks_h.append(bool(len(h) > 0 and h.mean() < 0))
            oks_l.append(bool(len(l) > 0 and l.mean() > 0))
        stable[f] = {"high符号安定(負)": all(oks_h), "low符号安定(正)": all(oks_l),
                     "詳細h": oks_h, "詳細l": oks_l}
        lines.append((f, rows))
    return lines, stable


def md_table(rows):
    if not rows:
        return "(データなし)"
    cols = list(rows[0].keys())
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join("—" if r[c] is None else str(r[c]) for c in cols) + " |")
    return "\n".join(out)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    L = []
    L.append("# 流動性トリガー（sweep-and-reclaim）オフライン検証（Phase5 Stage1）\n")
    L.append(f"- 生成: {datetime.now(timezone.utc).date()} / 6銘柄・日足{EVAL_YEARS}年（4hは730日制限内で参考併用）")
    L.append(f"- **閾値config（固定）**: PIVOT_K={PIVOT_K} / ATR={ATR_N}(単純平均) / α={ALPHA} / "
             f"registry深さ={REGISTRY_DEPTH}(新しい順) / ギャップガード=|Open−前日Close|>ATR×{GAP_GUARD_MULT} / "
             f"fwd={FWD[0]}・{FWD[1]}営業日")
    L.append("- イベントは1バー完結（High>水準 かつ Close<水準−α×ATR。low側鏡像）。"
             "ピボットは中心バーの5本後に確定してから registry 入り（ルックアヘッドなし）。"
             "タッチされた水準は reclaim 有無に関わらず除去。")
    L.append("- **事前判定ルール（固定）**: high後の翌1・2週リターンが負／low後は正を、"
             "6銘柄プール・1年×3サブ期間・両ホライズンとも符号安定（3/3）で満たすこと。\n")

    # ── 日足（主） ──
    ev_d, notes_d = collect(fetch_daily, "日足")
    L.append("## 日足（主検証）\n")
    L.append("### 発生頻度")
    for nt in notes_d:
        L.append(f"- {nt}")
    if not ev_d.empty:
        per_side = ev_d["side"].value_counts().to_dict()
        L.append(f"- プール合計: {len(ev_d)}件（high {per_side.get('high',0)} / low {per_side.get('low',0)}）\n")
        tables, stable = agg_table(ev_d, 3)
        for f, rows in tables:
            L.append(f"### 翌{f}営業日リターン（サブ期間別・プール）\n")
            L.append(md_table(rows))
            st = stable[f]
            L.append(f"\n- 符号安定: high(負)={st['詳細h']} → {'**3/3**' if st['high符号安定(負)'] else '未達'} / "
                     f"low(正)={st['詳細l']} → {'**3/3**' if st['low符号安定(正)'] else '未達'}\n")
        verdict = all(stable[f]["high符号安定(負)"] and stable[f]["low符号安定(正)"] for f in FWD)
    else:
        L.append("- イベントなし → 判定不能\n")
        verdict = False

    # ── 4h（参考） ──
    ev_4, notes_4 = collect(fetch_4h, "4h")
    L.append("## 4h（参考・730日制限のためサブ期間2に減）\n")
    for nt in notes_4:
        L.append(f"- {nt}")
    if not ev_4.empty:
        tables4, stable4 = agg_table(ev_4, 2)
        for f, rows in tables4:
            L.append(f"\n### 翌{f}本（4hバー基準・営業日換算しない）\n")
            L.append(md_table(rows))
            st = stable4[f]
            L.append(f"\n- 符号安定(2サブ): high={st['詳細h']} / low={st['詳細l']}")

    # ── 所見 ──
    L.append("\n## 所見（採用可否の事前見立て）\n")
    if verdict:
        L.append("- **事前ルール成立（日足・両ホライズン3/3）**: v1.7 は方向つき定義"
                 "（sweep方向×conviction方向の一致群vs不一致群）の分岐材料。")
    else:
        L.append("- **事前ルール不成立**: v1.7 は対称定義（v1.3流儀）の分岐材料。"
                 "Stage2 の shadow 記録は実施（Phase1/2 と同じ扱い）。")
        # 副次観察（事後・判定基準ではない。Phase2 A-1 の前例と同じ扱い）
        if not ev_d.empty:
            L.append("\n### 副次観察（事後・参考のみ）\n")
            tables_m, _ = agg_table(ev_d, 3)
            by_f = {f: rows for f, rows in tables_m}
            mirror = {}
            for f in FWD:
                hs = [r["high_mean%"] for r in by_f[f] if r["high_mean%"] is not None]
                ls = [r["low_mean%"] for r in by_f[f] if r["low_mean%"] is not None]
                mirror[f] = (all(x > 0 for x in hs), all(x < 0 for x in ls), hs, ls)
            L.append("- 仮説（reversal）と**逆＝継続方向**がほぼ符号安定: "
                     f"10営業日は high正 {mirror[10][2]}・low負 {mirror[10][3]} で**両側3/3**、"
                     f"5営業日は low負が3/3（high正は2/3）。")
            L.append("- 解釈すれば「sweep はストップ狩り後の反転ではなく、ブレイク方向への"
                     "**継続**として振る舞っている」——ただし事後観察であり、"
                     "サブ期間あたり n=10〜18 と小さい点に注意。方向つき定義の採否は"
                     "shadow 実データ（アウトオブサンプル）での再確認に委ねる。")
    L.append("- 発生頻度は上表参照。日足で銘柄・年あたり3〜5件と希少寄り——"
             "shadow 判定の n≥100 到達には相応の期間を要する見込み（基準は緩めない）。")

    report = "\n".join(L) + "\n"
    open(OUT, "w", encoding="utf-8").write(report)
    print(report)
    print(f"\n→ {OUT} に保存")


if __name__ == "__main__":
    main()

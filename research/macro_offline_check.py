# -*- coding: utf-8 -*-
"""
Phase1 Stage1: マクロレジーム（DXY/実質金利/USDCNH）のオフライン検証

本体パイプライン無改変・読み取り専用の research スクリプト。
  python research/macro_offline_check.py
→ research/macro_offline_report.md を生成（テーブル＋採用可否所見）。

データ（Secret不要の経路）:
  - DXY: yfinance "DX-Y.NYB"（不可なら "DX=F" にフォールバック）
  - 実質金利: FRED fredgraph.csv?id=DFII10（10年TIPS利回り・APIキー不要）
  - USDCNH: yfinance "CNH=X"（銅用。不可なら銅は対象外）
  - 対象銘柄: 金 GC=F / 銅 HG=F

レジーム定義（Stage2 の shadow フィールドと同一にする正準定義）:
  trend(s) = +1 if 20日変化>0 かつ 60日変化>0
             -1 if 20日変化<0 かつ 60日変化<0
              0 それ以外（短期と中期が不一致 = 方向感なし）

事前判定ルール（このスクリプトを書いた時点で固定）:
  金の1週フォワードリターンについて
  「実質金利低下(-1)×DXY下落(-1)」 − 「実質金利上昇(+1)×DXY上昇(+1)」の差(spread)が
  全期間で正、かつ 1年×3サブ期間すべてで同符号（正）であること。
  満たさなければ「採用不可（shadow記録は実施するが昇格候補ではない）」と明記する。
"""
import io
import sys
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf

EVAL_YEARS = 3
FWD_1W = 5    # 営業日
FWD_1M = 21

OUT_PATH = "research/macro_offline_report.md"


def fetch_close(ticker: str, fallbacks: list = None, period: str = "4y") -> pd.Series | None:
    """日足Closeを取得。失敗/データ不足時は fallbacks を順に試し、駄目なら None。"""
    for t in [ticker] + (fallbacks or []):
        try:
            df = yf.download(t, interval="1d", period=period,
                             progress=False, auto_adjust=False)
            if df is None or df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            s = df["Close"].dropna()
            if len(s) > 250:
                s.attrs["ticker_used"] = t
                return s
        except Exception:
            continue
    return None


def fetch_fred_dfii10(timeout: int = 20) -> pd.Series | None:
    """FRED から10年実質金利(DFII10)。APIキー不要のCSVエンドポイント。

    検証時このネットワークからは不達（curl/requests とも timeout）だったため、
    呼び出し側は TIP ETF 逆相関プロキシへフォールバックする。
    """
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10"
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        # 列名は DATE/observation_date 等の揺れに耐える
        date_col = df.columns[0]
        val_col = df.columns[1]
        df[val_col] = pd.to_numeric(df[val_col], errors="coerce")  # "." → NaN
        s = pd.Series(df[val_col].values,
                      index=pd.to_datetime(df[date_col])).dropna()
        if len(s) > 250:
            return s
    except Exception:
        pass
    return None


def trend(s: pd.Series) -> pd.Series:
    """±1/0 のレジーム方向（20日変化の符号を60日トレンドで確認）。"""
    import numpy as np
    t20 = np.sign(s - s.shift(20))
    t60 = np.sign(s - s.shift(60))
    return pd.Series(np.where(t20 == t60, t20, 0.0), index=s.index)


def fwd_return(s: pd.Series, days: int) -> pd.Series:
    return s.shift(-days) / s - 1.0


def regime_table(target_fwd: pd.Series, t_a: pd.Series, t_b: pd.Series,
                 label_a: str, label_b: str) -> pd.DataFrame:
    """レジーム(±1/0 × ±1/0)別の平均フォワードリターン(%)と件数。"""
    rows = []
    for va in (-1, 0, 1):
        for vb in (-1, 0, 1):
            mask = (t_a == va) & (t_b == vb)
            vals = target_fwd[mask].dropna()
            rows.append({
                label_a: int(va), label_b: int(vb),
                "n": len(vals),
                "mean_%": round(vals.mean() * 100, 3) if len(vals) else None,
                "median_%": round(vals.median() * 100, 3) if len(vals) else None,
            })
    return pd.DataFrame(rows)


def spread_by_period(target_fwd, t_ry, t_dxy, periods):
    """サブ期間ごとの spread = mean[(-1,-1)] - mean[(+1,+1)]（金の判定用）。"""
    out = []
    for name, (start, end) in periods.items():
        m = (target_fwd.index >= start) & (target_fwd.index < end)
        fav = target_fwd[m & (t_ry == -1) & (t_dxy == -1)].dropna()
        adv = target_fwd[m & (t_ry == 1) & (t_dxy == 1)].dropna()
        spread = (fav.mean() - adv.mean()) * 100 if len(fav) and len(adv) else None
        out.append({"期間": name, "n(追い風)": len(fav), "n(逆風)": len(adv),
                    "spread_1w_%": round(spread, 3) if spread is not None else None})
    return pd.DataFrame(out)


def md_table(df: pd.DataFrame) -> str:
    """Markdownテーブル自前実装（tabulate依存を避ける）。"""
    cols = list(df.columns)
    out = ["| " + " | ".join(str(c) for c in cols) + " |",
           "|" + "---|" * len(cols)]
    for _, row in df.iterrows():
        cells = ["—" if pd.isna(v) else str(v) for v in row]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    lines = []
    notes = []

    # ── データ取得 ──
    dxy = fetch_close("DX-Y.NYB", fallbacks=["DX=F"])
    gold = fetch_close("GC=F")
    copper = fetch_close("HG=F")
    # USDCNH: CNH=X / USDCNH=X は yfinance が1本しか返さない(実測)ため CNY=X(オンショア)で代替。
    # 20/60日 trend の符号レベルでは CNH と CNY はほぼ同一に動く。
    cnh = fetch_close("CNH=X", fallbacks=["USDCNH=X", "CNY=X"])

    # 実質金利: FRED DFII10 が第一候補。このネットワークからは不達(curl/requestsともtimeout)
    # のため、TIP ETF(物価連動国債・デュレーション約7年)の「逆相関プロキシ」で代替する。
    # 本検証で使うのは 20/60日変化の符号のみであり、TIP価格のトレンド符号の反転は
    # 実質金利のトレンド符号の妥当な代理（インフレ調整後のキャリーは符号レベルでは無視可能）。
    ry = fetch_fred_dfii10()
    ry_source = "FRED DFII10"
    tip = None
    if ry is None:
        tip = fetch_close("TIP")
        ry_source = "TIPプロキシ(逆相関)" if tip is not None else None

    if dxy is None:
        notes.append("**DXY が取得不可**（DX-Y.NYB / DX=F とも失敗）→ 検証続行不能。")
    else:
        notes.append(f"DXY: `{dxy.attrs.get('ticker_used')}` を使用（{len(dxy)}本）。")
    if ry is not None:
        notes.append(f"実質金利: FRED DFII10（{len(ry)}本・最終 {ry.index[-1].date()}）。")
    elif tip is not None:
        notes.append("実質金利: **FRED DFII10 不達（このネットワークから curl/requests とも timeout）**。"
                     "**TIP ETF の逆相関プロキシで代替**（trend符号のみ使用のため妥当。"
                     "Stage 2 は GitHub Actions ランナーから FRED 到達を再判定し、"
                     "DFII10→TIPプロキシのフォールバック連鎖にする想定）。")
    else:
        notes.append("**実質金利が取得不可**（FRED不達かつTIPも失敗）→ 金の検証続行不能。")
    if cnh is not None:
        used = cnh.attrs.get("ticker_used")
        if used != "CNH=X":
            notes.append(f"USDCNH: CNH=X/USDCNH=X は1本しか返らないため **`{used}` で代替**（{len(cnh)}本）。")
        else:
            notes.append(f"USDCNH: CNH=X（{len(cnh)}本）。")
    else:
        notes.append("**USDCNH系列が全滅 → 銅は今回対象外**。")
    have_ry_trend = (ry is not None) or (tip is not None)
    if gold is None or dxy is None or not have_ry_trend:
        notes.append("金の検証に必要な系列が欠落のため判定不能。")

    now = datetime.now(timezone.utc)
    lines.append("# マクロレジーム オフライン検証レポート（Phase1 Stage1）\n")
    lines.append(f"- 生成: {now.date()} / 直近{EVAL_YEARS}年・日足")
    lines.append("- レジーム定義: trend = +1(20日変化>0 かつ 60日変化>0) / -1(両方<0) / 0(不一致)")
    lines.append("- 事前判定ルール: 金の1週fwdで「実質金利↓×DXY↓」−「↑×↑」の spread が"
                 "全期間で正、かつ1年×3サブ期間すべて同符号（正）\n")
    lines.append("## データソース")
    for n in notes:
        lines.append(f"- {n}")

    if gold is not None and dxy is not None and have_ry_trend:
        # 金の取引日に整列（FRED/ETF は休日欠損があるため ffill）
        idx = gold.index
        dxy_a = dxy.reindex(idx).ffill()

        t_dxy = trend(dxy_a)
        if ry is not None:
            t_ry = trend(ry.reindex(idx).ffill())
        else:
            # TIP価格は実質金利と逆相関 → trend符号を反転して実質金利trendとする
            t_ry = -trend(tip.reindex(idx).ffill())

        # 評価ウィンドウ: 直近3年（fwd分の末尾欠損は dropna で自然に外れる）
        start = idx.max() - pd.DateOffset(years=EVAL_YEARS)
        win = idx >= start

        g1w = fwd_return(gold, FWD_1W)[win]
        g1m = fwd_return(gold, FWD_1M)[win]
        t_dxy_w, t_ry_w = t_dxy[win], t_ry[win]

        lines.append("\n## 金（GC=F）: レジーム別フォワードリターン")
        lines.append("\n### 1週（5営業日）\n")
        lines.append(md_table(regime_table(g1w, t_ry_w, t_dxy_w, "real_yield", "dxy")))
        lines.append("\n### 1ヶ月（21営業日）\n")
        lines.append(md_table(regime_table(g1m, t_ry_w, t_dxy_w, "real_yield", "dxy")))

        # 全期間 spread + サブ期間安定性
        fav_all = g1w[(t_ry_w == -1) & (t_dxy_w == -1)].dropna()
        adv_all = g1w[(t_ry_w == 1) & (t_dxy_w == 1)].dropna()
        spread_all = (fav_all.mean() - adv_all.mean()) * 100 if len(fav_all) and len(adv_all) else None

        eval_start = pd.Timestamp(start)
        eval_end = idx.max()
        seg = (eval_end - eval_start) / 3
        periods = {f"サブ{i+1} ({(eval_start + seg*i).date()}〜)":
                   (eval_start + seg * i, eval_start + seg * (i + 1)) for i in range(3)}
        sp = spread_by_period(g1w, t_ry_w, t_dxy_w, periods)

        lines.append("\n### 事前判定: spread（追い風 − 逆風、1週fwd）\n")
        lines.append(f"- 全期間 spread: **{spread_all:+.3f}%**"
                     f"（追い風 n={len(fav_all)} / 逆風 n={len(adv_all)}）" if spread_all is not None
                     else "- 全期間 spread: 算出不能（レジーム該当日不足）")
        lines.append("")
        lines.append(md_table(sp))

        signs = [s for s in sp["spread_1w_%"] if s is not None]
        sign_stable = len(signs) == 3 and all(s > 0 for s in signs)
        overall_pos = spread_all is not None and spread_all > 0
        verdict_ok = sign_stable and overall_pos

        lines.append("\n## 所見（採用可否）\n")
        if verdict_ok:
            lines.append("- **判定: 合格（shadow 採用候補）**。全期間 spread 正・3サブ期間とも正で符号安定。")
        else:
            why = []
            if not overall_pos:
                why.append("全期間 spread が正でない")
            if not sign_stable:
                why.append(f"サブ期間の符号が不安定（{signs}）")
            lines.append(f"- **判定: 事前ルール不成立**（{'、'.join(why)}）。"
                         "Stage2 の shadow 記録自体は実施するが、本レジーム定義のままでは昇格候補ではない。")
    else:
        lines.append("\n## 所見\n- 必須系列の欠落により判定不能。")

    # ── 銅（参考・判定対象外）──
    if copper is not None and dxy is not None and cnh is not None:
        idx = copper.index
        t_dxy_c = trend(dxy.reindex(idx).ffill())
        t_cnh_c = trend(cnh.reindex(idx).ffill())
        start = idx.max() - pd.DateOffset(years=EVAL_YEARS)
        win = idx >= start
        c1w = fwd_return(copper, FWD_1W)[win]
        c1m = fwd_return(copper, FWD_1M)[win]
        lines.append("\n## 銅（HG=F）: レジーム別フォワードリターン（参考・判定対象外）")
        lines.append("\n### 1週（5営業日）: 行=USDCNH trend, 列=DXY trend\n")
        lines.append(md_table(regime_table(c1w, t_cnh_c[win], t_dxy_c[win], "usdcnh", "dxy")))
        lines.append("\n### 1ヶ月（21営業日）\n")
        lines.append(md_table(regime_table(c1m, t_cnh_c[win], t_dxy_c[win], "usdcnh", "dxy")))
    elif copper is not None:
        lines.append("\n## 銅（HG=F）\n- USDCNH 不可のため対象外（上記データソース欄参照）。")

    report = "\n".join(lines) + "\n"
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    print(f"\n→ {OUT_PATH} に保存")


if __name__ == "__main__":
    main()

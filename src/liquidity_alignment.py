"""
流動性トリガー（swing registry + sweep-and-reclaim）の shadow 算出モジュール（Phase5）

conviction / routing には一切適用しない（CLAUDE.md 原則③）。
シグナル記録の `liquidity` フィールドに添付するだけ。

■ cost-zero: analyze_one が既に取得済みの OHLC DataFrame（config の
  lookback=200本 の窓。4h/日足とも同じ経路）内で完結する。**追加fetchなし**。
  窓が浅く水準が3本未満しか無ければある分だけ記録する。

■ 閾値は research/liquidity_offline_check.py（Stage1）と同一の正準定義:
  PIVOT_K=5 / ATR(14)単純平均 / α=0.25 / registry上下各3(新しい順) /
  ギャップガード |Open−前日Close| > ATR×3（連続限月のロール偽スイング除外）

■ sweep フィールドは「直近確定バー（=渡された df の最終バー）が
  sweep-and-reclaim を完成させた場合のみ非null」。過去バーの遡及検索はしない。

■ 算出失敗・データ不足は None を返す（呼び出し側は liquidity: null で記録継続。
  macro/cot/event_gate と同型）。例外を外に漏らさない。
"""
import logging

import pandas as pd

logger = logging.getLogger(__name__)

PIVOT_K = 5
ATR_N = 14
ALPHA = 0.25
REGISTRY_DEPTH = 3
GAP_GUARD_MULT = 3.0
MIN_BARS = 40   # ATR14 + ピボット確定(K*2+1) + 余裕


def _atr(df, n=ATR_N):
    prev_close = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"],
                    (df["High"] - prev_close).abs(),
                    (df["Low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def _build_state(df):
    """registry を最終バーまで走らせ、(reg_h, reg_l, 最終バーのsweep) を返す。

    Stage1（research/liquidity_offline_check.py）と同一のロジック:
    - ピボットは中心バーの PIVOT_K 本後に確定してから registry 入り
    - ギャップ日（|Open−前日Close| > ATR×3）の中心バーは registry 除外
    - タッチされた水準は reclaim の有無に関わらず除去
    """
    n = len(df)
    high, low, close, open_ = (df["High"].values, df["Low"].values,
                               df["Close"].values, df["Open"].values)
    a = _atr(df).values
    prev_close = df["Close"].shift(1).values

    piv_high, piv_low = {}, {}
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

    reg_h, reg_l = [], []
    sweep_last = None
    for t in range(n):
        for lv in piv_high.get(t, []):
            reg_h = ([lv] + reg_h)[:REGISTRY_DEPTH]
        for lv in piv_low.get(t, []):
            reg_l = ([lv] + reg_l)[:REGISTRY_DEPTH]
        if a[t] != a[t]:
            continue
        hit_h = [lv for lv in reg_h if high[t] > lv]
        hit_l = [lv for lv in reg_l if low[t] < lv]
        ev = None
        rec_h = [lv for lv in hit_h if close[t] < lv - ALPHA * a[t]]
        rec_l = [lv for lv in hit_l if close[t] > lv + ALPHA * a[t]]
        if rec_h:
            lv = max(rec_h)
            ev = {"side": "high", "level": round(float(lv), 4),
                  "excess_atr": round(float((high[t] - lv) / a[t]), 3),
                  "reclaim_atr": round(float((lv - close[t]) / a[t]), 3)}
        elif rec_l:
            lv = min(rec_l)
            ev = {"side": "low", "level": round(float(lv), 4),
                  "excess_atr": round(float((lv - low[t]) / a[t]), 3),
                  "reclaim_atr": round(float((close[t] - lv) / a[t]), 3)}
        reg_h = [lv for lv in reg_h if lv not in hit_h]
        reg_l = [lv for lv in reg_l if lv not in hit_l]
        # sweep は「最終バーで完成した場合のみ」採用（遡及検索しない）
        sweep_last = ev if t == n - 1 else None
    return reg_h, reg_l, sweep_last, a


def compute_liquidity(df) -> dict | None:
    """analyze_one の OHLC 窓から liquidity shadow 値を算出。失敗は None。"""
    try:
        if df is None or len(df) < MIN_BARS:
            return None
        need = {"Open", "High", "Low", "Close"}
        if not need.issubset(df.columns):
            return None
        reg_h, reg_l, sweep, a = _build_state(df)
        atr_last = float(a[-1])
        if atr_last != atr_last or atr_last <= 0:
            return None
        close_last = float(df["Close"].iloc[-1])
        nearest_high = min(reg_h) if reg_h else None   # 未タッチ高値のうち最も近い
        nearest_low = max(reg_l) if reg_l else None    # 未タッチ安値のうち最も近い
        return {
            "nearest_high": round(nearest_high, 4) if nearest_high is not None else None,
            "nearest_low": round(nearest_low, 4) if nearest_low is not None else None,
            "dist_high_atr": (round((nearest_high - close_last) / atr_last, 3)
                              if nearest_high is not None else None),
            "dist_low_atr": (round((close_last - nearest_low) / atr_last, 3)
                             if nearest_low is not None else None),
            "sweep": sweep,
        }
    except Exception:
        logger.warning("liquidity算出に失敗（null で記録継続）", exc_info=True)
        return None

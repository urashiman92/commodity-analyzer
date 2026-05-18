"""
テクニカル指標からTA期待度スコアを算出するモジュール

スコア = -100..+100 (正=強気期待, 負=弱気期待)
構成要素:
  RSI       最大 ±30
  MACD hist 最大 ±25
  EMA配列   最大 ±25
  BB位置    最大 ±20
"""
import numpy as np
import pandas as pd


def calc_expectation(df: pd.DataFrame, indicators: dict) -> dict:
    """
    Args:
        df: 指標計算済みDataFrame
        indicators: build_indicator_snapshot() の出力

    Returns:
        {
            'score': float,       # -100..+100
            'direction': str,     # 'bullish' | 'bearish' | 'neutral'
            'components': dict,   # 内訳
        }
    """
    score = 0.0
    components = {}

    # --- RSI (max ±30) ---
    # RSI30→+30(売られすぎ=強気期待), RSI50→0, RSI70→-30(買われすぎ=弱気期待)
    rsi = indicators.get('RSI')
    if rsi is not None:
        rsi_raw = (50.0 - rsi) / 20.0 * 30.0
        rsi_score = max(-30.0, min(30.0, rsi_raw))
        score += rsi_score
        components['rsi'] = round(rsi_score, 1)

    # --- MACDヒストグラム (max ±25) ---
    # 直近3本の方向一致+トレンドで強弱判定
    if len(df) >= 4 and 'MACD_hist' in df.columns:
        hist = df['MACD_hist'].iloc[-3:].values
        if not np.isnan(hist).any():
            all_pos = all(h > 0 for h in hist)
            all_neg = all(h < 0 for h in hist)
            if all_pos or all_neg:
                direction = 1.0 if all_pos else -1.0
                # ヒストグラムが方向に拡大中か縮小中か
                expanding = (hist[-1] - hist[0]) * direction >= 0
                macd_score = direction * (25.0 if expanding else 12.0)
            else:
                macd_score = 0.0
            score += macd_score
            components['macd'] = round(macd_score, 1)

    # --- EMA配列 (max ±25) ---
    # 3条件: EMA20>50, EMA50>200, price>EMA20
    ema20 = indicators.get('EMA20')
    ema50 = indicators.get('EMA50')
    ema200 = indicators.get('EMA200')
    price = indicators.get('現在価格')
    if all(v is not None for v in [ema20, ema50, ema200, price]):
        votes = [
            1 if ema20 > ema50 else -1,
            1 if ema50 > ema200 else -1,
            1 if price > ema20 else -1,
        ]
        ema_score = sum(votes) / 3.0 * 25.0
        score += ema_score
        components['ema'] = round(ema_score, 1)

    # --- BB位置 (max ±20) ---
    # %B=0(下限)→+20, %B=0.5(中央)→0, %B=1(上限)→-20
    bb_upper = indicators.get('BB上限')
    bb_lower = indicators.get('BB下限')
    if all(v is not None for v in [price, bb_upper, bb_lower]):
        band_width = bb_upper - bb_lower
        if band_width > 0:
            pct_b = (price - bb_lower) / band_width
            bb_raw = (0.5 - pct_b) * 40.0
            bb_score = max(-20.0, min(20.0, bb_raw))
            score += bb_score
            components['bb'] = round(bb_score, 1)

    score = max(-100.0, min(100.0, score))

    if score >= 15.0:
        direction = 'bullish'
    elif score <= -15.0:
        direction = 'bearish'
    else:
        direction = 'neutral'

    return {
        'score': round(score, 1),
        'direction': direction,
        'components': components,
    }

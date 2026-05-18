"""
テクニカル指標からシグナルを検出するモジュール
"""
import pandas as pd
import numpy as np


def detect_signals(df: pd.DataFrame, thresholds: dict) -> dict:
    """
    DataFrame（指標計算済み）からシグナルを抽出

    Returns:
        {
            'signals': [シグナル名のリスト],
            'has_signal': bool,
            'importance': 'normal' | 'high',
            'summary': テキスト,
        }
    """
    if len(df) < 30:
        return _empty_result()

    signals = []
    latest = df.iloc[-1]
    prev = df.iloc[-2]

    # === RSI 過熱/反転 ===
    if pd.notna(latest['RSI']):
        rsi = latest['RSI']
        rsi_prev = prev['RSI']

        if rsi >= thresholds['rsi_overbought']:
            signals.append(f"RSI買われすぎ ({rsi:.1f})")
        elif rsi <= thresholds['rsi_oversold']:
            signals.append(f"RSI売られすぎ ({rsi:.1f})")

        # RSIが70/30から戻ってきた瞬間（反転兆候）
        if rsi_prev >= thresholds['rsi_overbought'] and rsi < thresholds['rsi_overbought']:
            signals.append(f"RSI買われすぎから反落 ({rsi:.1f})")
        elif rsi_prev <= thresholds['rsi_oversold'] and rsi > thresholds['rsi_oversold']:
            signals.append(f"RSI売られすぎから反発 ({rsi:.1f})")

    # === MACD クロス ===
    lookback = thresholds['macd_cross_lookback']
    macd_cross = _detect_cross(df['MACD'].tail(lookback + 1).values,
                                df['MACD_signal'].tail(lookback + 1).values)
    if macd_cross == 'golden':
        signals.append("MACDゴールデンクロス")
    elif macd_cross == 'dead':
        signals.append("MACDデッドクロス")

    # MACDゼロライン突破
    if pd.notna(latest['MACD']) and pd.notna(prev['MACD']):
        if prev['MACD'] < 0 < latest['MACD']:
            signals.append("MACDゼロライン上抜け")
        elif prev['MACD'] > 0 > latest['MACD']:
            signals.append("MACDゼロライン下抜け")

    # === EMAクロス（20と50） ===
    if 'EMA20' in df.columns and 'EMA50' in df.columns:
        ema_lookback = thresholds['ema_cross_lookback']
        ema_cross = _detect_cross(df['EMA20'].tail(ema_lookback + 1).values,
                                   df['EMA50'].tail(ema_lookback + 1).values)
        if ema_cross == 'golden':
            signals.append("EMA20/50ゴールデンクロス")
        elif ema_cross == 'dead':
            signals.append("EMA20/50デッドクロス")

    # === ボリンジャーバンドタッチ/ブレイク ===
    if pd.notna(latest['BB_upper']) and pd.notna(latest['BB_lower']):
        bb_touch = thresholds['bb_touch_threshold']
        if latest['Close'] >= latest['BB_upper'] * bb_touch:
            if latest['Close'] > latest['BB_upper']:
                signals.append("BB上限ブレイク")
            else:
                signals.append("BB上限タッチ")
        elif latest['Close'] <= latest['BB_lower'] / bb_touch:
            if latest['Close'] < latest['BB_lower']:
                signals.append("BB下限ブレイク")
            else:
                signals.append("BB下限タッチ")

    # === 値幅ブレイク（直近20本の高安を更新） ===
    if len(df) >= 21:
        recent_high = df['High'].iloc[-21:-1].max()
        recent_low = df['Low'].iloc[-21:-1].min()
        if latest['Close'] > recent_high:
            signals.append("直近20本高値ブレイク")
        elif latest['Close'] < recent_low:
            signals.append("直近20本安値ブレイク")

    # === 重要度判定 ===
    importance = 'high' if len(signals) >= 2 else 'normal'

    return {
        'signals': signals,
        'has_signal': len(signals) > 0,
        'importance': importance,
        'summary': " / ".join(signals) if signals else "シグナルなし",
    }


def _detect_cross(fast_vals: np.ndarray, slow_vals: np.ndarray) -> str | None:
    """配列内でクロス発生を検出"""
    if len(fast_vals) < 2 or np.isnan(fast_vals).any() or np.isnan(slow_vals).any():
        return None
    for i in range(1, len(fast_vals)):
        if fast_vals[i-1] <= slow_vals[i-1] and fast_vals[i] > slow_vals[i]:
            return 'golden'
        if fast_vals[i-1] >= slow_vals[i-1] and fast_vals[i] < slow_vals[i]:
            return 'dead'
    return None


def _empty_result():
    return {
        'signals': [],
        'has_signal': False,
        'importance': 'normal',
        'summary': "データ不足",
    }


def build_indicator_snapshot(df: pd.DataFrame) -> dict:
    """
    Geminiに渡す指標の現在値スナップショットを作成
    """
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else latest

    # 直近20本の高安
    recent_high = df['High'].iloc[-20:].max() if len(df) >= 20 else float('nan')
    recent_low = df['Low'].iloc[-20:].min() if len(df) >= 20 else float('nan')

    def fmt(v):
        return round(float(v), 2) if pd.notna(v) else None

    return {
        '現在価格': fmt(latest['Close']),
        '前足からの変化': fmt(latest['Close'] - prev['Close']),
        'EMA20': fmt(latest.get('EMA20')),
        'EMA50': fmt(latest.get('EMA50')),
        'EMA200': fmt(latest.get('EMA200')),
        'RSI': fmt(latest.get('RSI')),
        'MACD': fmt(latest.get('MACD')),
        'MACDシグナル': fmt(latest.get('MACD_signal')),
        'MACDヒストグラム': fmt(latest.get('MACD_hist')),
        'BB上限': fmt(latest.get('BB_upper')),
        'BBミドル': fmt(latest.get('BB_middle')),
        'BB下限': fmt(latest.get('BB_lower')),
        '直近20本高値': fmt(recent_high),
        '直近20本安値': fmt(recent_low),
    }

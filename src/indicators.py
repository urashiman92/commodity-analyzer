"""
テクニカル指標を計算するモジュール
（pandas-taに依存せず、numpy/pandasのみで実装）
"""
import pandas as pd
import numpy as np


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    """EMA（指数移動平均）"""
    return series.ewm(span=period, adjust=False).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI（相対力指数）"""
    delta = series.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)

    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calc_macd(series: pd.Series, fast: int = 12, slow: int = 26,
              signal: int = 9) -> dict:
    """MACD"""
    ema_fast = calc_ema(series, fast)
    ema_slow = calc_ema(series, slow)
    macd = ema_fast - ema_slow
    signal_line = calc_ema(macd, signal)
    histogram = macd - signal_line
    return {
        'macd': macd,
        'signal': signal_line,
        'histogram': histogram,
    }


def calc_bbands(series: pd.Series, period: int = 20, std: float = 2.0) -> dict:
    """ボリンジャーバンド"""
    middle = series.rolling(window=period).mean()
    stdev = series.rolling(window=period).std()
    upper = middle + std * stdev
    lower = middle - std * stdev
    return {
        'upper': upper,
        'middle': middle,
        'lower': lower,
    }


def add_all_indicators(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """
    DataFrameに全テクニカル指標を追加
    """
    df = df.copy()
    close = df['Close']

    # EMA（複数期間）
    for period in config['ema_periods']:
        df[f'EMA{period}'] = calc_ema(close, period)

    # RSI
    df['RSI'] = calc_rsi(close, config['rsi_period'])

    # MACD
    macd_data = calc_macd(close, config['macd_fast'],
                         config['macd_slow'], config['macd_signal'])
    df['MACD'] = macd_data['macd']
    df['MACD_signal'] = macd_data['signal']
    df['MACD_hist'] = macd_data['histogram']

    # ボリンジャーバンド
    bb = calc_bbands(close, config['bb_period'], config['bb_std'])
    df['BB_upper'] = bb['upper']
    df['BB_middle'] = bb['middle']
    df['BB_lower'] = bb['lower']

    return df

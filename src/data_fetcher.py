"""
yfinanceからOHLCVデータを取得するモジュール
"""
import logging
import time
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


def fetch_ohlcv(ticker: str, interval: str, period: str,
                resample: str = None, max_retries: int = 3) -> pd.DataFrame | None:
    """
    指定銘柄のOHLCVデータを取得

    Args:
        ticker: yfinanceのシンボル（例: 'CL=F'）
        interval: '15m', '1h', '1d' など
        period: '5d', '60d', '2y' など
        resample: '4H' などを指定すると集約
        max_retries: リトライ回数

    Returns:
        DataFrame（カラム: Open/High/Low/Close/Volume）、失敗時None
    """
    for attempt in range(max_retries):
        try:
            df = yf.download(
                ticker,
                interval=interval,
                period=period,
                progress=False,
                auto_adjust=False,
            )

            if df is None or df.empty:
                logger.warning(f"{ticker} {interval}: データが空です (試行{attempt + 1})")
                time.sleep(2 ** attempt)
                continue

            # MultiIndexの場合フラット化
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # 必要カラムのみ
            df = df[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
            df.dropna(inplace=True)

            # リサンプリング処理（4時間足など）
            # pandas は頻度を小文字で要求（'4H' は廃止 → 'invalid frequency'）。
            # config に大文字が混じっても落ちないよう正規化する。
            if resample:
                df = df.resample(resample.lower()).agg({
                    'Open': 'first',
                    'High': 'max',
                    'Low': 'min',
                    'Close': 'last',
                    'Volume': 'sum',
                })
                df.dropna(inplace=True)

            if len(df) < 50:
                logger.warning(f"{ticker} {interval}: データ不足 ({len(df)}本)")
                return None

            return df

        except Exception as e:
            logger.error(f"{ticker} {interval} 取得エラー: {e} (試行{attempt + 1})")
            time.sleep(2 ** attempt)

    logger.error(f"{ticker} {interval}: 全リトライ失敗")
    return None

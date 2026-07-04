"""
マクロレジーム算出モジュール（Phase1 shadow・記録専用）

conviction / routing には一切適用しない（CLAUDE.md 原則③: shadow開始）。
signal_pending の `macro` フィールドに記録するだけの読み取り専用ファクター。

設計:
- ステートレス: 実行時にその場で日足3〜4系列を取得して算出。新stateファイルなし・
  書き手マップ無変更。
- プロセス内キャッシュで1回だけ算出し、同一runの全銘柄シグナルに同一dictを添付
  （日次粒度の状態量なので銘柄間で共有してよい）。
- どこかで失敗しても None を返すだけで、シグナル記録は継続する（呼び出し側は
  macro: null で記録）。このモジュールは例外を外に漏らさない。

レジーム定義（research/macro_offline_check.py の Stage1 と同一。変更する場合は
shadowデータでの再分析後に新版として定義し直す。ここでは変えない）:
  trend = +1 (20日変化>0 かつ 60日変化>0) / -1 (両方<0) / 0 (不一致)

実質金利ソース: FRED DFII10（第一候補・APIキー不要）→ TIP ETF 逆相関プロキシ。
実際に使ったソースを real_yield_source に必ず記録する（ソース切替起因の
レジーム揺れを後から分離可能にするため）。

chg20d の単位（real_yield_source で解釈を分ける）:
  - dxy_chg20d / cny_chg20d: 20営業日の変化率(%)
  - real_yield_chg20d: DFII10 のとき利回りの変化幅(パーセントポイント)。
    TIP_proxy のとき TIP価格20日変化率(%)の符号反転値（実質金利方向に揃えた代理値。
    単位は利回り幅ではない点に注意）。
"""
import io
import logging

import pandas as pd
import requests
import yfinance as yf

logger = logging.getLogger(__name__)

FRED_DFII10_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10"

# プロセス内キャッシュ（1 run = 1算出）
_CACHE = {"computed": False, "value": None}


def _fetch_close_chain(tickers: list, period: str = "6mo",
                       min_bars: int = 70,
                       auto_adjust: bool = False) -> pd.Series | None:
    """日足Closeをフォールバック連鎖で取得。全滅なら None。

    min_bars=70: 60日トレンド算出(61本)+余裕。CNH=X が1本しか返さない事故を
    ここで自然に弾く（実測: CNH=X/USDCNH=X は1本 → CNY=X で代替）。
    auto_adjust: 分配のあるETF(TIP等)は True（調整後終値）を指定すること。
    未調整だと分配落ちの階段下落が trend 符号を騙す。指数/FXはどちらでも同じ。
    """
    for t in tickers:
        try:
            df = yf.download(t, interval="1d", period=period,
                             progress=False, auto_adjust=auto_adjust)
            if df is None or df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            s = df["Close"].dropna()
            if len(s) >= min_bars:
                s.attrs["ticker_used"] = t
                return s
        except Exception:
            continue
    return None


def _fetch_dfii10(timeout: int = 20) -> pd.Series | None:
    """FRED から10年実質金利(DFII10)。不達環境があるため timeout は短めに。"""
    try:
        r = requests.get(FRED_DFII10_URL, timeout=timeout,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        date_col, val_col = df.columns[0], df.columns[1]
        df[val_col] = pd.to_numeric(df[val_col], errors="coerce")  # "." → NaN
        s = pd.Series(df[val_col].values,
                      index=pd.to_datetime(df[date_col])).dropna()
        if len(s) >= 70:
            return s
    except Exception:
        pass
    return None


def _trend(s: pd.Series) -> int | None:
    """±1/0 のレジーム方向（Stage1と同一定義）。データ不足は None。"""
    if s is None or len(s) < 61:
        return None
    c20 = float(s.iloc[-1] - s.iloc[-21])
    c60 = float(s.iloc[-1] - s.iloc[-61])
    s20 = (c20 > 0) - (c20 < 0)
    s60 = (c60 > 0) - (c60 < 0)
    return s20 if s20 == s60 else 0


def _pct_chg20d(s: pd.Series) -> float | None:
    if s is None or len(s) < 21:
        return None
    return round(float(s.iloc[-1] / s.iloc[-21] - 1.0) * 100, 4)


def _diff_chg20d(s: pd.Series) -> float | None:
    if s is None or len(s) < 21:
        return None
    return round(float(s.iloc[-1] - s.iloc[-21]), 4)


def _compute() -> dict | None:
    # DXY
    dxy = _fetch_close_chain(["DX-Y.NYB", "DX=F"])
    dxy_trend = _trend(dxy)
    dxy_chg = _pct_chg20d(dxy)

    # 実質金利: DFII10 → TIP逆相関プロキシ
    ry_source = None
    ry_trend = None
    ry_chg = None
    ry = _fetch_dfii10()
    if ry is not None:
        ry_source = "DFII10"
        ry_trend = _trend(ry)
        ry_chg = _diff_chg20d(ry)  # パーセントポイント
    else:
        # TIP は月次分配ETF: 未調整終値は分配落ちで階段下落し、逆相関プロキシの
        # trend 符号を「実質金利上昇」側へ系統的に騙すため、調整後終値を使う。
        tip = _fetch_close_chain(["TIP"], auto_adjust=True)
        if tip is not None:
            ry_source = "TIP_proxy"
            t = _trend(tip)
            ry_trend = -t if t is not None else None  # 価格と実質金利は逆相関
            c = _pct_chg20d(tip)
            ry_chg = round(-c, 4) if c is not None else None

    # 人民元（銅用）: CNH=X/USDCNH=X は実測1本しか返らないため CNY=X が実質の主力
    cny = _fetch_close_chain(["CNH=X", "USDCNH=X", "CNY=X"])
    cny_trend = _trend(cny)
    cny_chg = _pct_chg20d(cny)

    # 全ソース失敗 → None（呼び出し側が macro: null で記録継続）
    if dxy_trend is None and ry_trend is None and cny_trend is None:
        return None

    regime = (f"ry{ry_trend:+d}_dxy{dxy_trend:+d}"
              if ry_trend is not None and dxy_trend is not None else None)

    return {
        "dxy_trend": dxy_trend,
        "dxy_chg20d": dxy_chg,
        "real_yield_trend": ry_trend,
        "real_yield_chg20d": ry_chg,
        "real_yield_source": ry_source,
        "cny_trend": cny_trend,
        "cny_chg20d": cny_chg,
        "regime": regime,
    }


def get_macro_state(force_recompute: bool = False) -> dict | None:
    """マクロレジーム状態を返す（プロセス内で1回だけ算出・以後キャッシュ）。

    例外を外に漏らさない。全ソース失敗・想定外エラーは None。
    """
    if _CACHE["computed"] and not force_recompute:
        return _CACHE["value"]
    try:
        value = _compute()
    except Exception:
        logger.warning("macro状態の算出に失敗（シグナル記録は継続）", exc_info=True)
        value = None
    if value is not None:
        logger.info(f"  macro: regime={value['regime']}  "
                    f"ry_src={value['real_yield_source']}  "
                    f"dxy={value['dxy_trend']}({value['dxy_chg20d']}%)  "
                    f"cny={value['cny_trend']}")
    else:
        logger.warning("  macro: 全ソース取得失敗 → null で記録継続")
    _CACHE["computed"] = True
    _CACHE["value"] = value
    return value

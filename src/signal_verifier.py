"""
シグナル照合モジュール（検証基盤・読み手兼整理係）

日次単独ジョブ（verify-signals.yml）から実行される。pending と history の
唯一の書き手であり、他に書き手がいないためコミット競合がゼロ。

処理フロー（verify_pending）:
  1. signal_pending.jsonl を読む
  2. 各シグナルの照合可能なホライズン（経過時間が到達済み かつ price=null）を
     yfinance の実価格で埋める（dir_hit / return_pct を算出）
  3. 全4ホライズンが埋まったシグナル → signal_history.jsonl へ移動
  4. 未照合ホライズンが残るシグナル → pending に残す
  5. 両ファイルを書き戻す（呼び出し側のワークフローがコミット&push）

符号処理（重要）:
  raw_return = (price_at_horizon - price_at_signal) / price_at_signal * 100
  bullish はそのまま、bearish は符号反転 → 「方向が合っていれば正のリターン」。
  dir_hit = return_pct > 0（neutral は記録するが集計時に除外）。

ローテーション注意:
  168h(1週間)は照合完了まで7日かかるため、月またぎでアーカイブされたシグナルが
  まだ未照合の 168h を持ちうる。そのため照合対象は現行 pending に加え、
  pending は最長7日で縮むので pending 単独で足りるが、history 側のアーカイブは
  summarize でのみ「直近2ファイル」を読む。
"""
import sys
import os
from datetime import datetime, timezone, timedelta

# src 直下を import パスに（ワークフローから `python src/signal_verifier.py` 実行）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signal_logger import (  # noqa: E402
    HORIZON_HOURS, read_jsonl, write_jsonl, rotate_history_if_needed,
    PENDING_PATH,
)

HISTORY_PATH = "signal_history.jsonl"

# direction 文字列 → 符号
_DIR_SIGN = {"bullish": 1, "bearish": -1, "neutral": 0}


def _price_at(symbol_ticker: str, target_time: datetime) -> float | None:
    """target_time に最も近い（それ以前で直近の）足の終値を yfinance で取得。

    取得失敗・該当足なしは None（呼び出し側で null のままスキップ）。
    """
    import yfinance as yf

    # target の前後に余裕を持たせて日足を取る（1週間幅）。intraday 精緻化は将来。
    start = (target_time - timedelta(days=10)).strftime("%Y-%m-%d")
    end = (target_time + timedelta(days=2)).strftime("%Y-%m-%d")
    try:
        df = yf.download(symbol_ticker, start=start, end=end,
                         interval="1d", progress=False, auto_adjust=False)
    except Exception:
        return None
    if df is None or df.empty:
        return None

    # MultiIndex 列を平坦化
    try:
        import pandas as pd
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
    except Exception:
        pass
    if "Close" not in df.columns:
        return None

    # target 時刻以前で最も新しい足の Close
    try:
        idx = df.index
        # tz 揃え（yfinance index は tz-naive のことが多い）
        target_naive = target_time.replace(tzinfo=None)
        mask = [ (_to_naive(ts) <= target_naive) for ts in idx ]
        candidates = df[mask]
        if candidates.empty:
            return None
        return float(candidates["Close"].iloc[-1])
    except Exception:
        return None


def _to_naive(ts):
    """pandas Timestamp / datetime を tz-naive な datetime に。"""
    try:
        ts = ts.to_pydatetime()
    except AttributeError:
        pass
    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.replace(tzinfo=None)
    return ts


# symbol 名 → yfinance ティッカー（config と同期。verifier は config を読まず自前で持つ）
SYMBOL_TICKER = {
    "小麦": "ZW=F",
    "金": "GC=F",
    "WTI原油": "CL=F",
    "トウモロコシ": "ZC=F",
    "大豆": "ZS=F",
    "銅": "HG=F",
}


def _fill_horizon(rec: dict, key: str, now: datetime) -> bool:
    """1レコードの1ホライズンを照合して埋める。埋めたら True。

    条件: 経過時間 >= horizon かつ price が None。失敗時は False（null 維持）。
    """
    hz = rec.get("horizons", {}).get(key)
    if hz is None or hz.get("price") is not None:
        return False
    try:
        sig_time = datetime.fromisoformat(rec["timestamp"])
    except (KeyError, ValueError, TypeError):
        return False
    if sig_time.tzinfo is None:
        sig_time = sig_time.replace(tzinfo=timezone.utc)

    horizon_h = HORIZON_HOURS[key]
    target_time = sig_time + timedelta(hours=horizon_h)
    if now - sig_time < timedelta(hours=horizon_h):
        return False  # まだ到達していない

    ticker = SYMBOL_TICKER.get(rec.get("symbol"))
    if not ticker:
        return False
    price_h = _price_at(ticker, target_time)
    if price_h is None:
        return False  # 取得失敗 → null のまま次回再試行

    p_s = rec.get("price_at_signal")
    if not p_s:
        return False
    raw_return = (price_h - p_s) / p_s * 100.0
    sign = _DIR_SIGN.get(rec.get("direction"), 0)
    # bullish はそのまま、bearish は符号反転、neutral は raw のまま記録（集計時除外）
    return_pct = raw_return * sign if sign != 0 else raw_return
    dir_hit = (return_pct > 0) if sign != 0 else None

    hz["price"] = round(price_h, 4)
    hz["return_pct"] = round(return_pct, 4)
    hz["dir_hit"] = dir_hit
    return True


def _all_filled(rec: dict) -> bool:
    """全ホライズンの price が埋まっていれば True（完結）。"""
    hzs = rec.get("horizons", {})
    return all(hzs.get(k, {}).get("price") is not None for k in HORIZON_HOURS)


def verify_pending(pending_path: str = PENDING_PATH,
                   history_path: str = HISTORY_PATH,
                   now: datetime = None) -> dict:
    """pending を照合し、完結したものを history へ移動。両ファイルを書き戻す。

    Returns: 集計 dict（filled/moved/remaining 件数）。
    """
    if now is None:
        now = datetime.now(timezone.utc)

    pending = read_jsonl(pending_path)
    history = read_jsonl(history_path)

    filled_count = 0
    for rec in pending:
        for key in HORIZON_HOURS:
            if _fill_horizon(rec, key, now):
                filled_count += 1

    still_pending, newly_done = [], []
    for rec in pending:
        (newly_done if _all_filled(rec) else still_pending).append(rec)

    history.extend(newly_done)
    write_jsonl(pending_path, still_pending)
    write_jsonl(history_path, history)

    # history 側のみ月次アーカイブ
    archived = rotate_history_if_needed(history_path, now=now)

    return {
        "filled_horizons": filled_count,
        "moved_to_history": len(newly_done),
        "remaining_pending": len(still_pending),
        "history_total": len(history),
        "archived": archived,
    }


def summarize(history_path: str = HISTORY_PATH) -> dict:
    """完結済み history を対象に、ホライズン別の方向一致率・平均リターンを出す。

    照合対象は history（完結済み）+ 直近アーカイブ（168h が月またぎで
    アーカイブされうるため）。neutral 方向は方向一致率から除外。
    """
    import glob
    paths = [history_path]
    archives = sorted(glob.glob("signal_history_*.jsonl"))
    paths += archives[-1:]  # 直近1アーカイブ（= 直近2ファイル分）

    rows = []
    seen = set()
    for p in paths:
        for r in read_jsonl(p):
            key = (r.get("symbol"), r.get("timestamp"), r.get("timeframe"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)

    out = {"total_signals": len(rows), "horizons": {}}
    for key in HORIZON_HOURS:
        hits, dirs, rets = 0, 0, []
        for r in rows:
            hz = r.get("horizons", {}).get(key, {})
            if hz.get("price") is None:
                continue
            rp = hz.get("return_pct")
            if rp is not None:
                rets.append(rp)
            dh = hz.get("dir_hit")
            if dh is not None:  # neutral(None) は除外
                dirs += 1
                if dh:
                    hits += 1
        out["horizons"][key] = {
            "n_directional": dirs,
            "hit_rate": round(hits / dirs, 3) if dirs else None,
            "avg_return_pct": round(sum(rets) / len(rets), 3) if rets else None,
            "n_priced": len(rets),
        }
    return out


def main():
    import json
    result = verify_pending()
    print("=== verify_pending 結果 ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n=== summarize（途中経過）===")
    print(json.dumps(summarize(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

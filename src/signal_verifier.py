"""
シグナル照合モジュール（検証基盤・history の唯一の書き手）

日次単独ジョブ（verify-signals.yml）から実行される。

書き手マップ（一ファイル一ライターの完全適用）:
  signal_pending_4h.jsonl ← ta-4h 実行の analyzer 本体のみ（append 専用）
  signal_pending_1d.jsonl ← ta-1d 実行の analyzer 本体のみ（append 専用）
  signal_history.jsonl    ← verify-signals（このモジュール）のみ（append 専用）

verifier は pending を**読み取り専用**で走査する。pending からの削除・移動は
誰もしない（追記専用ログ）。pending と history の重複は signal_id の
idempotency（history 既載はスキップ）で防ぐ。

照合方式: age >= 168h のエントリだけを対象に、全4ホライズン
(1h/24h/72h/168h) を価格履歴から一括遡及計算して history へ追記する。
部分 fill の中間保存はしない（どれか1つでも価格が取れなければ次回再試行）。

符号処理:
  raw_return = (price_at_horizon - price_at_signal) / price_at_signal * 100
  bullish はそのまま、bearish は符号反転 → 「方向が合っていれば正のリターン」。
  dir_hit = return_pct > 0（neutral は記録するが集計時に除外）。
"""
import glob
import os
import sys
from datetime import datetime, timezone, timedelta

# src 直下を import パスに（ワークフローから `python src/signal_verifier.py` 実行）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signal_logger import (  # noqa: E402
    HORIZON_HOURS, read_jsonl, write_jsonl, rotate_history_if_needed,
    make_signal_id,
)

HISTORY_PATH = "signal_history.jsonl"

# 全ホライズン確定に必要な経過時間（=最長ホライズン）
VERIFY_AFTER_HOURS = max(HORIZON_HOURS.values())

# direction 文字列 → 符号
_DIR_SIGN = {"bullish": 1, "bearish": -1, "neutral": 0}

# symbol 名 → yfinance ティッカー（config と同期。verifier は config を読まず自前で持つ）
SYMBOL_TICKER = {
    "小麦": "ZW=F",
    "金": "GC=F",
    "WTI原油": "CL=F",
    "トウモロコシ": "ZC=F",
    "大豆": "ZS=F",
    "銅": "HG=F",
}


def _price_at(symbol_ticker: str, target_time: datetime) -> float | None:
    """target_time 以前で直近の足の終値を yfinance で取得。

    取得失敗・該当足なしは None（呼び出し側でエントリごとスキップ→次回再試行）。
    """
    import yfinance as yf

    start = (target_time - timedelta(days=10)).strftime("%Y-%m-%d")
    end = (target_time + timedelta(days=2)).strftime("%Y-%m-%d")
    try:
        df = yf.download(symbol_ticker, start=start, end=end,
                         interval="1d", progress=False, auto_adjust=False)
    except Exception:
        return None
    if df is None or df.empty:
        return None

    try:
        import pandas as pd
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
    except Exception:
        pass
    if "Close" not in df.columns:
        return None

    try:
        target_naive = target_time.replace(tzinfo=None)
        mask = [(_to_naive(ts) <= target_naive) for ts in df.index]
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


def _signal_id_of(rec: dict) -> str:
    """レコードの signal_id。旧レコード（フィールド欠落）は同じ式で導出。"""
    return rec.get("signal_id") or make_signal_id(
        rec.get("timestamp", ""), rec.get("symbol", ""), rec.get("timeframe", ""))


def _known_signal_ids() -> set:
    """history + 全アーカイブに既載の signal_id 集合（idempotency 用）。

    アーカイブも見るのは、月またぎで history からアーカイブへ退避済みの
    シグナルを二重追記しないため。
    """
    ids = set()
    for p in [HISTORY_PATH] + sorted(glob.glob("signal_history_*.jsonl")):
        for r in read_jsonl(p):
            ids.add(_signal_id_of(r))
    return ids


def _compute_all_horizons(rec: dict, sig_time: datetime) -> dict | None:
    """全4ホライズンを遡及計算。1つでも価格が取れなければ None（部分fillしない）。"""
    ticker = SYMBOL_TICKER.get(rec.get("symbol"))
    if not ticker:
        return None
    p_s = rec.get("price_at_signal")
    if not p_s:
        return None
    sign = _DIR_SIGN.get(rec.get("direction"), 0)

    horizons = {}
    for key, hours in HORIZON_HOURS.items():
        price_h = _price_at(ticker, sig_time + timedelta(hours=hours))
        if price_h is None:
            return None
        raw_return = (price_h - p_s) / p_s * 100.0
        return_pct = raw_return * sign if sign != 0 else raw_return
        horizons[key] = {
            "price": round(price_h, 4),
            "return_pct": round(return_pct, 4),
            "dir_hit": (return_pct > 0) if sign != 0 else None,
        }
    return horizons


def verify_pending(now: datetime = None) -> dict:
    """pending(読み取り専用) を走査し、確定済みシグナルを history へ追記する。

    対象: age >= 168h かつ history 未載（signal_id）。
    pending ファイルは一切書き換えない。

    Returns: 集計 dict。対象0件でも正常終了（例外を投げない）。
    """
    if now is None:
        now = datetime.now(timezone.utc)

    known = _known_signal_ids()
    pending_files = sorted(glob.glob("signal_pending_*.jsonl"))

    appended = []
    stats = {"scanned": 0, "already_in_history": 0, "not_ready": 0,
             "price_unavailable": 0, "bad_record": 0}

    for pf in pending_files:
        for rec in read_jsonl(pf):
            stats["scanned"] += 1
            try:
                sig_time = datetime.fromisoformat(rec["timestamp"])
            except (KeyError, ValueError, TypeError):
                stats["bad_record"] += 1
                continue
            if sig_time.tzinfo is None:
                sig_time = sig_time.replace(tzinfo=timezone.utc)

            sid = _signal_id_of(rec)
            if sid in known:
                stats["already_in_history"] += 1
                continue
            if now - sig_time < timedelta(hours=VERIFY_AFTER_HOURS):
                stats["not_ready"] += 1
                continue

            horizons = _compute_all_horizons(rec, sig_time)
            if horizons is None:
                stats["price_unavailable"] += 1
                continue  # null のまま次回再試行（pending は残る）

            out = dict(rec)
            out["signal_id"] = sid
            out["horizons"] = horizons
            appended.append(out)
            known.add(sid)

    if appended:
        history = read_jsonl(HISTORY_PATH)
        history.extend(appended)
        write_jsonl(HISTORY_PATH, history)

    archived = rotate_history_if_needed(HISTORY_PATH, now=now)

    stats.update({
        "appended_to_history": len(appended),
        "pending_files": pending_files,
        "archived": archived,
    })
    return stats


def summarize(history_path: str = HISTORY_PATH) -> dict:
    """確定済み history を対象に、ホライズン別の方向一致率・平均リターンを出す。

    history + 直近アーカイブを読む（月またぎ直後も切れ目なく見えるように）。
    neutral 方向は方向一致率から除外。
    """
    paths = [history_path]
    archives = sorted(glob.glob("signal_history_*.jsonl"))
    paths += archives[-1:]

    rows = []
    seen = set()
    for p in paths:
        for r in read_jsonl(p):
            sid = _signal_id_of(r)
            if sid in seen:
                continue
            seen.add(sid)
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
            if dh is not None:
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

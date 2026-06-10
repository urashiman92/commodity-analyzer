"""
シグナル永続記録モジュール（検証基盤・書き手）

analyzer 本体が出したシグナルを時間軸別の signal_pending_*.jsonl に追記する。

書き手マップ（一ファイル一ライターの完全適用）:
  signal_pending_4h.jsonl ← ta-4h 実行の analyzer 本体のみ（append 専用）
  signal_pending_1d.jsonl ← ta-1d 実行の analyzer 本体のみ（append 専用）
  signal_history.jsonl    ← verify-signals のみ（append 専用）
pending は追記専用ログ。verifier は読み取り専用で、pending からの削除・移動は誰もしない。

horizons は 1h / 24h / 72h(3日) / 168h(1週間) の4つ。
スイング〜長期評価が本命のため 72h/168h が主軸、1h/24h は織り込み速度確認用。
"""
import hashlib
import json
import os
from datetime import datetime, timezone

# 照合する4ホライズン（時間, ラベル）。verifier と共有する正準定義。
HORIZON_HOURS: dict[str, float] = {
    "1h": 1.0,
    "24h": 24.0,
    "72h": 72.0,
    "168h": 168.0,
}

# 時間軸ラベル → pending ファイル。書き手はそのワークフローの analyzer 本体のみ。
# 15分/1時間は現在 schedule 無効（手動復活時のためのマッピングだけ用意）。
TF_PENDING_FILES: dict[str, str] = {
    "4時間": "signal_pending_4h.jsonl",
    "日足": "signal_pending_1d.jsonl",
    "15分": "signal_pending_15m.jsonl",
    "1時間": "signal_pending_1h.jsonl",
}


def pending_path_for(timeframe: str) -> str:
    """時間軸ラベルから pending ファイル名を解決。未知ラベルは other に隔離。"""
    return TF_PENDING_FILES.get(timeframe, "signal_pending_other.jsonl")


def make_signal_id(timestamp: str, symbol: str, timeframe: str) -> str:
    """シグナルのユニークキー。verifier の idempotency（history 既載スキップ）に使う。"""
    return hashlib.sha1(f"{timestamp}|{symbol}|{timeframe}".encode("utf-8")).hexdigest()


def _empty_horizons() -> dict:
    """各ホライズンを null 初期化（未照合）。"""
    return {k: {"price": None, "dir_hit": None, "return_pct": None}
            for k in HORIZON_HOURS}


def record_signal(signal: dict, price_at_signal: float,
                  jsonl_path: str | None = None) -> None:
    """1シグナルを時間軸別 pending JSONL に1行追記する。

    Args:
        signal: 以下のキーを持つ dict（main.py が conviction/alignment から組む）:
            symbol, timeframe, ta_score, conviction_score, coefficient,
            direction, is_divergence, net_direction, news_count,
            high_importance_count
        price_at_signal: 照合の基準点（シグナル時点の終値）
        jsonl_path: 追記先。省略時は signal["timeframe"] から解決
                    （4時間→_4h, 日足→_1d）
    """
    if jsonl_path is None:
        jsonl_path = pending_path_for(signal.get("timeframe", ""))
    ts = datetime.now(timezone.utc).isoformat()
    record = {
        "signal_id": make_signal_id(ts, signal.get("symbol", ""),
                                    signal.get("timeframe", "")),
        "timestamp": ts,
        "symbol": signal.get("symbol"),
        "timeframe": signal.get("timeframe"),
        "price_at_signal": float(price_at_signal),
        "ta_score": signal.get("ta_score"),
        "conviction_score": signal.get("conviction_score"),
        "coefficient": signal.get("coefficient"),
        "direction": signal.get("direction"),
        "is_divergence": signal.get("is_divergence"),
        "net_direction": signal.get("net_direction"),
        "news_count": signal.get("news_count"),
        "high_importance_count": signal.get("high_importance_count"),
        "horizons": _empty_horizons(),
    }
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: str) -> list[dict]:
    """JSONL を全読み。存在しなければ空リスト。壊れた行はスキップ。"""
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def write_jsonl(path: str, rows: list[dict]) -> None:
    """JSONL を全書き（インプレースでなく全読み→全書き）。"""
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def rotate_history_if_needed(history_path: str = "signal_history.jsonl",
                             max_rows: int = 5000, now: datetime = None) -> str | None:
    """history が肥大化したら当月より前のレコードを月次アーカイブへ退避。

    完結済みシグナルを貯める history 専用のローテーション。pending は
    最長7日で自然に縮むため対象外。

    アーカイブ発火条件: 行数が max_rows 超過 OR 当月より前のレコードが存在。
    退避先: signal_history_YYYYMM.jsonl（最古月ごと）。

    Returns: アーカイブを作成した場合そのパス、しなければ None。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    rows = read_jsonl(history_path)
    if not rows:
        return None

    cur_ym = (now.year, now.month)

    def ym_of(rec: dict):
        try:
            ts = datetime.fromisoformat(rec["timestamp"])
            return (ts.year, ts.month)
        except (KeyError, ValueError, TypeError):
            return None

    has_old = any(( y := ym_of(r)) is not None and y < cur_ym for r in rows)
    if not has_old and len(rows) <= max_rows:
        return None

    # 当月より前を最古月だけ切り出してアーカイブ（複数月あれば次回呼び出しで順次）
    old_months = sorted({y for r in rows if (y := ym_of(r)) is not None and y < cur_ym})
    if not old_months:
        # 当月のみだが max_rows 超過 → 何もしない（当月は分割しない方針）
        return None
    target = old_months[0]
    archive_path = f"signal_history_{target[0]:04d}{target[1]:02d}.jsonl"

    to_archive = [r for r in rows if ym_of(r) == target]
    remain = [r for r in rows if ym_of(r) != target]

    # 既存アーカイブがあれば追記マージ
    existing = read_jsonl(archive_path)
    write_jsonl(archive_path, existing + to_archive)
    write_jsonl(history_path, remain)
    return archive_path

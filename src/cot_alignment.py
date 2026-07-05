"""
CoT状態の読み手モジュール（Phase2 shadow・記録専用）

conviction / routing には一切適用しない（CLAUDE.md 原則③）。
cot-weekly.yml が更新する cot_state.json（リポジトリ同梱・チェックアウト済み）を
**ローカルファイルとして読むだけ**。analyzer 実行時にネットワークを使わない。

鮮度: 祝日週は公表が遅延しうるため、state の as_of がどれだけ古いかを
age_days として記録に添える（鮮度でのフィルタはしない。集計側が判断）。
ファイル欠損・パース失敗・銘柄なしは None（呼び出し側は cot: null で記録継続）。
"""
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STATE_PATH = "cot_state.json"

# プロセス内キャッシュ（1 run = 1読み込み）
_CACHE = {"loaded": False, "symbols": None}


def _load() -> dict | None:
    if _CACHE["loaded"]:
        return _CACHE["symbols"]
    symbols = None
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, encoding="utf-8") as f:
                data = json.load(f) or {}
            symbols = data.get("symbols") or None
    except Exception:
        logger.warning("cot_state.json の読み込みに失敗（cot: null で記録継続）",
                       exc_info=True)
        symbols = None
    _CACHE["loaded"] = True
    _CACHE["symbols"] = symbols
    return symbols


def get_cot_state(symbol: str, now: datetime = None) -> dict | None:
    """銘柄の CoT shadow 記録値を返す。無ければ None（例外を漏らさない）。

    Returns:
        {"as_of", "mm_net", "pctl", "wow", "wow_tail", "age_days"} | None
    """
    try:
        symbols = _load()
        if not symbols or symbol not in symbols:
            return None
        entry = dict(symbols[symbol])
        if now is None:
            now = datetime.now(timezone.utc)
        try:
            as_of = datetime.strptime(entry.get("as_of", ""), "%Y-%m-%d")
            as_of = as_of.replace(tzinfo=timezone.utc)
            entry["age_days"] = (now - as_of).days
        except (ValueError, TypeError):
            entry["age_days"] = None
        return entry
    except Exception:
        logger.warning("cot状態の取得に失敗（cot: null で記録継続）", exc_info=True)
        return None

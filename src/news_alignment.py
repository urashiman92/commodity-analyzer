"""
ニュース(Gemini方向判定)とTA期待度の整合性係数を算出するモジュール

係数 = 0.3..1.7
  同方向: 1.6 (基準)
  中立:   1.0
  逆方向: 0.4 (基準)

時間減衰: 1.0 (0-6h) / 0.7 (6-24h) / 0.4 (24-72h) / 0.1 (72h+)
  → 係数を 1.0 に向けて引き戻す

──────────────────────────────────────────────────────────
このモジュールは2系統のAPIを持つ:
  1. calc_alignment_coefficient(...)  : 単一方向スカラ版(旧)。後方互換のため温存。
  2. calculate_alignment(items, ...)  : 実ニュース(NewsItem)のリストを受ける版(新)。
     news-bot が GitHub raw に書き出す news_state.json / reports_state.json を
     analyzer が取得し、銘柄別に集約して整合性係数を算出する。
──────────────────────────────────────────────────────────
"""
from dataclasses import dataclass
from datetime import datetime, timezone

# 時間帯ごとの減衰係数 (上限時間, 減衰率)
_TIME_DECAY: list[tuple[float, float]] = [
    (6.0,        1.0),
    (24.0,       0.7),
    (72.0,       0.4),
    (float('inf'), 0.1),
]

# 権威ソース: 方向一致時は係数フロア 1.3 を保証
AUTHORITY_SOURCES: frozenset[str] = frozenset({
    'USDA', 'WASDE', 'FOMC', 'EIA', 'COT', 'OPEC', 'IMF', 'Fed', 'BOJ',
})

_OPPOSITE = {'bullish': 'bearish', 'bearish': 'bullish'}


def _time_decay_factor(age_hours: float) -> float:
    for threshold, factor in _TIME_DECAY:
        if age_hours < threshold:
            return factor
    return 0.1


def _base_coefficient(news_dir: str, ta_dir: str) -> float:
    if news_dir == 'neutral' or ta_dir == 'neutral':
        return 1.0
    return 1.6 if news_dir == ta_dir else 0.4


def calc_alignment_coefficient(
    news_direction: str,
    ta_direction: str,
    ta_score: float = 0.0,
    is_authority_source: bool = False,
    news_age_hours: float = 0.0,
) -> dict:
    """
    Args:
        news_direction:      Geminiが返した方向 ('bullish'|'bearish'|'neutral')
        ta_direction:        expectation_scorerが返したTA方向
        ta_score:            TA期待度スコア (ダイバージェンス判定に使用)
        is_authority_source: USDA/FOMC等の権威ソース由来か
        news_age_hours:      ニュースの経過時間(h) — リアルタイム実行時は 0

    Returns:
        {
            'coefficient':    float,  # 0.3..1.7
            'is_divergence':  bool,
            'news_direction': str,
            'base':           float,
            'decay':          float,
        }
    """
    news_dir = news_direction.lower()
    ta_dir = ta_direction.lower()

    base = _base_coefficient(news_dir, ta_dir)
    decay = _time_decay_factor(news_age_hours)

    # 時間減衰: 係数を 1.0 に引き戻す (古いほど中立寄り)
    coeff = 1.0 + (base - 1.0) * decay

    # 権威ソースかつ方向一致: 係数フロア 1.3
    if is_authority_source and base >= 1.0:
        coeff = max(coeff, 1.3)

    coeff = max(0.3, min(1.7, coeff))

    # ダイバージェンス: TA/ニュースが逆方向 かつ TA強度が十分
    is_divergence = (
        _OPPOSITE.get(ta_dir) == news_dir
        and abs(ta_score) >= 30.0
    )

    return {
        'coefficient':    round(coeff, 3),
        'is_divergence':  is_divergence,
        'news_direction': news_dir,
        'base':           base,
        'decay':          decay,
    }


# ══════════════════════════════════════════════════════════
# 新API: 実ニュース(NewsItem)リストを受けて整合性係数を算出
# ══════════════════════════════════════════════════════════

# impact/direction 文字列 → 符号 (日英両対応・小文字化して照合)
DIRECTION_MAP: dict[str, int] = {
    '上昇': 1, '強気': 1, 'bullish': 1,
    '下落': -1, '弱気': -1, 'bearish': -1,
    '中立': 0, '混合': 0, 'neutral': 0,
}

# 権威ソース重要度フロア: source文字列(小文字)に含まれれば importance を引き上げ
_AUTHORITY_FLOORS: list[tuple[int, tuple[str, ...]]] = [
    (5, ('usda', 'wasde', 'nass')),
    (4, ('eia', 'fomc', 'opec', 'lme', 'powell', 'federal reserve')),
]


@dataclass
class NewsItem:
    """news-bot が書き出した1ニュースレコード"""
    title: str
    timestamp: datetime  # UTC tz-aware
    importance: int
    direction: str
    source: str
    commodity: str
    summary: str = ""


def time_decay_weight(age_hours: float) -> float:
    """ニュースの経過時間による重み (0..1)。古いほど軽い。"""
    if age_hours < 6:
        return 1.0
    if age_hours < 24:
        return 0.7
    if age_hours < 72:
        return 0.4
    if age_hours < 168:
        return 0.15
    return 0.0


def _effective_importance(importance, source: str) -> int:
    """権威ソースなら重要度をフロアまで引き上げた実効重要度を返す"""
    s = (source or '').lower()
    floor = 0
    for level, keys in _AUTHORITY_FLOORS:
        if any(k in s for k in keys):
            floor = max(floor, level)
    try:
        imp = int(importance)
    except (TypeError, ValueError):
        imp = 1
    return max(imp, floor)


def calculate_alignment(news_items: list, ta_direction: int, now=None) -> dict:
    """
    実ニュースのリストを集約して整合性係数を算出する。

    Args:
        news_items:   NewsItem のリスト (該当銘柄ぶんのみ)
        ta_direction: TA期待度の符号 (+1=強気 / -1=弱気 / 0=中立)
        now:          現在時刻(UTC aware)。省略時は datetime.now(timezone.utc)

    Returns:
        {
            'coefficient':           float,  # 0.3..1.7
            'normalized':            float,  # -1..+1 (= ta_direction * net_direction)
            'net_direction':         float,  # -1..+1 (ニュース自体の加重方向)
            'news_direction':        str,    # 'bullish'|'bearish'|'neutral'
            'news_count':            int,
            'high_importance_count': int,    # importance>=4 & age<24h & 方向あり
        }

    ニュース0件・全て減衰0の場合は coefficient=1.0 (中立素通り)。
    """
    if now is None:
        now = datetime.now(timezone.utc)

    total_w = 0.0
    signed_w = 0.0  # Σ(weight · direction)
    high_importance_count = 0

    for item in news_items:
        dir_value = DIRECTION_MAP.get(str(item.direction).strip().lower(), 0)
        eff_imp = _effective_importance(item.importance, item.source)

        ts = item.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_hours = (now - ts).total_seconds() / 3600.0

        weight = eff_imp * time_decay_weight(age_hours)
        if weight <= 0:
            continue

        total_w += weight
        signed_w += weight * dir_value

        if eff_imp >= 4 and age_hours < 24 and dir_value != 0:
            high_importance_count += 1

    net_direction = (signed_w / total_w) if total_w > 0 else 0.0
    normalized = ta_direction * net_direction
    coefficient = max(0.3, min(1.7, 1.0 + 0.7 * normalized))

    if net_direction > 0:
        news_direction = 'bullish'
    elif net_direction < 0:
        news_direction = 'bearish'
    else:
        news_direction = 'neutral'

    return {
        'coefficient':           round(coefficient, 3),
        'normalized':            round(normalized, 3),
        'net_direction':         round(net_direction, 3),
        'news_direction':        news_direction,
        'news_count':            len(news_items),
        'high_importance_count': high_importance_count,
    }

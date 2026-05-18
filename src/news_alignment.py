"""
ニュース(Gemini方向判定)とTA期待度の整合性係数を算出するモジュール

係数 = 0.3..1.7
  同方向: 1.6 (基準)
  中立:   1.0
  逆方向: 0.4 (基準)

時間減衰: 1.0 (0-6h) / 0.7 (6-24h) / 0.4 (24-72h) / 0.1 (72h+)
  → 係数を 1.0 に向けて引き戻す
"""

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

"""
TA期待度 × ニュース整合性係数 = 確信度スコア

確信度スコア = ta_score × alignment_coefficient
  符号: 正=強気、負=弱気
  絶対値: 規模・緊急度 (notification_router がルーティングに使用)
"""


def calc_conviction(ta_expectation: dict, news_alignment: dict) -> dict:
    """
    Args:
        ta_expectation: expectation_scorer.calc_expectation() の出力
        news_alignment: news_alignment.calc_alignment_coefficient() の出力

    Returns:
        {
            'score':          float,   # 符号付き確信度
            'abs_score':      float,   # ルーティング用絶対値
            'direction':      str,     # 'bullish'|'bearish'|'neutral'
            'is_divergence':  bool,
            'ta_score':       float,
            'coefficient':    float,
            'ta_direction':   str,
            'news_direction': str,
            'components':     dict,    # TA内訳
        }
    """
    ta_score = ta_expectation['score']
    coeff = news_alignment['coefficient']
    score = round(ta_score * coeff, 1)
    abs_score = abs(score)

    if score >= 10.0:
        direction = 'bullish'
    elif score <= -10.0:
        direction = 'bearish'
    else:
        direction = 'neutral'

    return {
        'score':          score,
        'abs_score':      abs_score,
        'direction':      direction,
        'is_divergence':  news_alignment['is_divergence'],
        'ta_score':       ta_score,
        'coefficient':    coeff,
        'ta_direction':   ta_expectation['direction'],
        'news_direction': news_alignment['news_direction'],
        'components':     ta_expectation.get('components', {}),
    }


def format_conviction_summary(conviction: dict) -> str:
    """Discord embed用の確信度サマリー文字列を生成"""
    dir_emoji = {'bullish': '📈', 'bearish': '📉', 'neutral': '➡️'}
    emoji = dir_emoji.get(conviction['direction'], '❓')

    lines = [
        f"確信度: {conviction['score']:+.1f}　{emoji} {conviction['direction']}",
        f"TA期待度: {conviction['ta_score']:+.1f} × 整合係数: {conviction['coefficient']:.2f}",
    ]

    if conviction['is_divergence']:
        lines.append("⚡ TA/ニュース乖離シグナル")

    comp = conviction.get('components', {})
    if comp:
        parts = [f"{k}:{v:+.0f}" for k, v in comp.items()]
        lines.append(f"内訳: {', '.join(parts)}")

    return "\n".join(lines)

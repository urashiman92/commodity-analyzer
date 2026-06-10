"""
確信度スコアに基づいてDiscord通知先チャンネルを振り分けるモジュール

振り分けルール:
  ダイバージェンス          → WEBHOOK_DIVERGENCE  (TA/ニュース逆向き)
  |確信度| >= 60            → WEBHOOK_CRITICAL    (重大シグナル)
  |確信度| >= 30            → 銘柄別Webhook        (既存チャンネル)
  15 <= |確信度| < 30       → reference (通知スキップ・シグナル記録のみ)
  |確信度| < 15             → silent    (通知も記録もしない)
"""
import os

THRESHOLDS = {
    'critical':   60,
    'commodity':  30,
    'reference':  15,
}

WEBHOOK_CRITICAL_ENV   = 'WEBHOOK_CRITICAL'
WEBHOOK_DIVERGENCE_ENV = 'WEBHOOK_DIVERGENCE'


def get_destination(conviction: dict, symbol: dict) -> dict:
    """
    Args:
        conviction: conviction_scorer.calc_conviction() の出力
        symbol:     config.yaml の symbols エントリ

    Returns:
        {
            'webhook_env':  str | None,  # 環境変数名
            'channel_type': str,         # 'critical'|'commodity'|'reference'|'divergence'|'silent'
        }
    """
    abs_score = conviction['abs_score']

    if conviction['is_divergence']:
        return {'webhook_env': WEBHOOK_DIVERGENCE_ENV, 'channel_type': 'divergence'}

    if abs_score >= THRESHOLDS['critical']:
        return {'webhook_env': WEBHOOK_CRITICAL_ENV, 'channel_type': 'critical'}

    if abs_score >= THRESHOLDS['commodity']:
        return {'webhook_env': symbol['webhook_env'], 'channel_type': 'commodity'}

    if abs_score >= THRESHOLDS['reference']:
        # reference帯は通知せず記録のみ（webhookなし）
        return {'webhook_env': None, 'channel_type': 'reference'}

    return {'webhook_env': None, 'channel_type': 'silent'}


def should_notify(channel_type: str) -> bool:
    """Discord通知（およびGemini解説文生成）を行う routing か。

    reference は記録のみに格下げ（通知スキップ）。silent は記録も通知もなし
    （--no-filter 時の記録は呼び出し側の判断）。
    """
    return channel_type in ('commodity', 'critical', 'divergence')


def resolve_webhook_url(destination: dict) -> str | None:
    """環境変数名から実際のWebhook URLを解決する"""
    env_key = destination['webhook_env']
    if not env_key:
        return None
    return os.getenv(env_key)


# チャンネル種別ごとの表示設定
CHANNEL_DISPLAY = {
    'critical':   {'emoji': '🚨', 'label': 'CRITICAL'},
    'divergence': {'emoji': '⚡', 'label': 'DIVERGENCE'},
    'commodity':  {'emoji': '📊', 'label': ''},
    'reference':  {'emoji': '📎', 'label': 'REF'},
    'silent':     {'emoji': '🔇', 'label': 'SILENT'},
}


def channel_label(channel_type: str) -> str:
    d = CHANNEL_DISPLAY.get(channel_type, {'emoji': '❓', 'label': channel_type})
    label = d['label']
    return f"{d['emoji']} {label}".strip() if label else d['emoji']

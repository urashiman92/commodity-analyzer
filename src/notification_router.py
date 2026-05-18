"""
確信度スコアに基づいてDiscord通知先チャンネルを振り分けるモジュール

振り分けルール:
  ダイバージェンス          → WEBHOOK_DIVERGENCE  (TA/ニュース逆向き)
  |確信度| >= 60            → WEBHOOK_CRITICAL    (重大シグナル)
  |確信度| >= 30            → 銘柄別Webhook        (既存チャンネル)
  15 <= |確信度| < 30       → WEBHOOK_REFERENCE   (参考情報、ミュート推奨)
  |確信度| < 15             → None                 (通知しない)
"""
import os

THRESHOLDS = {
    'critical':   60,
    'commodity':  30,
    'reference':  15,
}

WEBHOOK_CRITICAL_ENV   = 'WEBHOOK_CRITICAL'
WEBHOOK_DIVERGENCE_ENV = 'WEBHOOK_DIVERGENCE'
WEBHOOK_REFERENCE_ENV  = 'WEBHOOK_REFERENCE'


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
        return {'webhook_env': WEBHOOK_REFERENCE_ENV, 'channel_type': 'reference'}

    return {'webhook_env': None, 'channel_type': 'silent'}


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

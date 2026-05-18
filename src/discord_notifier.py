"""
Discord webhookに分析結果を投稿するモジュール
"""
import logging
import time
import requests

logger = logging.getLogger(__name__)


# 重要度ごとのDiscord embed色
COLORS = {
    'normal': 0x3498db,  # 青
    'high': 0xe74c3c,    # 赤（複数シグナル同時）
}


def send_to_discord(webhook_url: str, symbol_name: str, timeframe: str,
                    price: float, signals: list, importance: str,
                    analysis_text: str, max_retries: int = 3,
                    conviction_info: str | None = None) -> bool:
    """
    Discord webhookに投稿

    Returns:
        成功時True
    """
    if not webhook_url or 'discord.com/api/webhooks' not in webhook_url:
        logger.error(f"無効なwebhook URL: {symbol_name}")
        return False

    importance_emoji = "⚠️" if importance == 'high' else "📊"
    color = COLORS.get(importance, COLORS['normal'])

    embed = {
        "title": f"{importance_emoji} {symbol_name} - {timeframe}足シグナル",
        "description": analysis_text[:4000],  # Discord制限
        "color": color,
        "fields": [
            {
                "name": "💴 現在価格",
                "value": f"{price:,.2f}" if price else "N/A",
                "inline": True,
            },
            {
                "name": "🎯 検出シグナル",
                "value": "\n".join(f"• {s}" for s in signals[:10]) or "なし",
                "inline": False,
            },
            *([{
                "name": "📐 確信度スコア",
                "value": conviction_info,
                "inline": False,
            }] if conviction_info else []),
        ],
        "footer": {
            "text": "テクニカル分析Bot | yfinance + Gemini",
        },
    }

    payload = {
        "embeds": [embed],
    }

    for attempt in range(max_retries):
        try:
            r = requests.post(webhook_url, json=payload, timeout=10)
            if r.status_code in (200, 204):
                return True
            elif r.status_code == 429:
                # レート制限
                retry_after = r.json().get('retry_after', 1)
                logger.warning(f"Discord rate limit: {retry_after}秒待機")
                time.sleep(retry_after + 0.5)
            else:
                logger.error(f"Discord投稿失敗 [{r.status_code}]: {r.text[:200]}")
                time.sleep(2 ** attempt)
        except Exception as e:
            logger.error(f"Discord投稿エラー: {e}")
            time.sleep(2 ** attempt)

    return False

"""
Gemini APIで分析テキストと方向判定を生成するモジュール
google-genai SDK (google.genai) を使用

返り値の形式:
    {
        'text':       str,   # 400文字以内のテクニカル所見
        'direction':  str,   # 'bullish' | 'bearish' | 'neutral'
        'importance': str,   # 'high' | 'normal'
        'summary':    str,   # 1行サマリー
    }
"""
import json
import logging
import re
import time

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
あなたは金融のプロフェッショナルで、特にコモディティ関連のテクニカル分析に特化しています。
以下のルールに従い、テクニカル所見を**JSONのみ**で返してください。
コードブロック記号（```）や前後の余計な文字は不要です。

【出力形式】
{
  "direction": "bullish" または "bearish" または "neutral",
  "importance": "high" または "normal",
  "summary": "方向感と主因を50文字以内で",
  "analysis": "400文字以内のテクニカル所見"
}

【direction 判定基準】
- bullish: 上昇優位のシグナル・指標配置
- bearish: 下落優位のシグナル・指標配置
- neutral: 方向感が乏しい、または相反するシグナルが混在

【importance 判定基準】
- high: 複数の強いシグナルが同方向に重なっている
- normal: シグナルが1つ、または弱い

【analysis の記載ルール】
- 結論ファースト：最初の1行で方向感と主因
- 検出シグナルの意味を文脈に沿って解説
- 注意すべき水準（サポート・レジスタンス）を1つ明示
- シナリオ分岐があれば触れる
- マークダウン強調・箇条書きは最小限、自然な文章で

【絶対NG】
- 投資推奨の表現（買え・売れ・必ず等）
- 「と思います」「かもしれません」の多用
- ファンダメンタル要因への深入り
"""


def analyze_with_gemini(api_key: str, model_name: str,
                        symbol_name: str, timeframe: str,
                        indicators: dict, signals: list,
                        max_output_tokens: int = 1500,
                        temperature: float = 0.3,
                        max_retries: int = 3) -> dict | None:
    """
    指標とシグナルをGeminiに投げて所見と方向判定を生成。

    Returns:
        {'text': str, 'direction': str, 'importance': str, 'summary': str}
        失敗時 None
    """
    client = genai.Client(api_key=api_key)

    user_prompt = f"""\
【銘柄】{symbol_name}
【時間軸】{timeframe}
【検出シグナル】
{chr(10).join('- ' + s for s in signals) or '- なし'}

【現在の指標値】
{_format_indicators(indicators)}

上記を踏まえて、JSON形式でテクニカル所見を返してください。
"""

    # thinking_budget=0: 思考を無効化し全トークンを出力に割り当て
    # (gemini-2.5-flash はデフォルトで思考にトークンを消費するため
    #  max_output_tokens が思考+出力の合計バジェットとして扱われてしまう)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=user_prompt,
                config=config,
            )
            raw = response.text.strip()
            if raw:
                return _parse_response(raw)
        except Exception as e:
            wait = 2 ** attempt
            logger.error(f"Gemini API エラー ({attempt + 1}/{max_retries}): {e}, {wait}秒待機")
            time.sleep(wait)

    return None


def _parse_response(raw: str) -> dict:
    """
    JSONをパース。
    - コードブロック除去
    - 最初の '{' 位置から raw_decode（思考モデルが前後にテキストを出す対策）
    - 全て失敗したらテキストとしてフォールバック
    """
    cleaned = re.sub(r'```(?:json)?\s*', '', raw).strip('`').strip()
    decoder = json.JSONDecoder()

    for i, ch in enumerate(cleaned):
        if ch != '{':
            continue
        try:
            data, _ = decoder.raw_decode(cleaned, i)
            return {
                'text':       str(data.get('analysis', raw)),
                'direction':  str(data.get('direction', 'neutral')).lower(),
                'importance': str(data.get('importance', 'normal')).lower(),
                'summary':    str(data.get('summary', '')),
            }
        except json.JSONDecodeError:
            continue

    logger.warning("GeminiレスポンスがJSON非準拠。テキストとして処理。")
    return {
        'text':       raw,
        'direction':  'neutral',
        'importance': 'normal',
        'summary':    '',
    }


def _format_indicators(indicators: dict) -> str:
    lines = []
    for k, v in indicators.items():
        if v is not None:
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)

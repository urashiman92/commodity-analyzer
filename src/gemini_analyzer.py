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

# プロセス内サーキットブレーカー: RPD(日次クォータ)超過を一度検知したら、
# この実行中の以後の Gemini 呼び出しを全スキップする（リトライしても当日中は
# 回復しないため、待つだけ無駄かつ後続銘柄の処理を遅らせる）。
_RPD_EXHAUSTED = False


def reset_rpd_breaker() -> None:
    """テスト用: RPDブレーカーをリセット。"""
    global _RPD_EXHAUSTED
    _RPD_EXHAUSTED = False


def _is_rpd_error(e: Exception) -> bool:
    """429本文から RPD(日次)超過と判別できるか。
    実測の quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier"""
    return 'PerDay' in str(e)


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
    指標とシグナルをGeminiに投げて所見テキストを生成（embed用解説文専任）。

    max_retries=3 は「初回 + リトライ2回」。バックオフは 15s/30s で
    無料枠の分間レート(5req/分)を尊重する。

    Returns:
        {'text': str, 'direction': str, 'importance': str, 'summary': str}
        失敗時 None（呼び出し側がフォールバック文で続行する。
        この返り値が conviction / routing / 記録に影響してはならない）
    """
    global _RPD_EXHAUSTED
    if _RPD_EXHAUSTED:
        logger.info(f"Gemini RPD超過検知済みのためスキップ: {symbol_name}/{timeframe}")
        return None

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
            logger.error(f"Geminiレスポンスが空 ({attempt + 1}/{max_retries})")
        except Exception as e:
            # RPD(日次)超過はリトライ無意味: ブレーカーを立てて即時 None。
            # RPM(分間)超過・判別不能な429・その他は従来どおりバックオフ。
            if _is_rpd_error(e):
                _RPD_EXHAUSTED = True
                logger.error(f"Gemini RPD(日次クォータ)超過を検知。"
                             f"この実行中の以後の呼び出しを全スキップ: {e}")
                return None
            logger.error(f"Gemini API エラー ({attempt + 1}/{max_retries}): {e}")
        # 無料枠の分間レート(5req/分=12s間隔)を尊重した指数バックオフ。
        # 最終試行の後は待たない。
        if attempt < max_retries - 1:
            wait = 15 * (2 ** attempt)  # 15s, 30s
            logger.info(f"  {wait}秒待機してリトライ")
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

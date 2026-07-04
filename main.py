"""
コモディティテクニカル分析Bot
メインスクリプト

使い方:
    python main.py                  # 全銘柄・全時間軸を分析
    python main.py --tf 15分        # 特定時間軸のみ
    python main.py --dry-run        # Discord投稿せずテスト
    python main.py --no-filter      # シグナルなしでも通知
"""
import argparse
import io
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

# srcフォルダをパス追加
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from data_fetcher import fetch_ohlcv
from indicators import add_all_indicators
from signal_detector import detect_signals, build_indicator_snapshot
from gemini_analyzer import analyze_with_gemini
from discord_notifier import send_to_discord
from expectation_scorer import calc_expectation
from news_alignment import calculate_alignment, NewsItem
from conviction_scorer import calc_conviction, format_conviction_summary
from notification_router import (get_destination, resolve_webhook_url,
                                 channel_label, should_notify)
from signal_logger import record_signal, pending_path_for
from macro_alignment import get_macro_state


# news-bot が GitHub raw に書き出すニュース状態ファイル
_NEWS_BASE = "https://raw.githubusercontent.com/urashiman92/commodity-news-bot/main"
NEWS_STATE_URLS = (
    f"{_NEWS_BASE}/news_state.json",
    f"{_NEWS_BASE}/reports_state.json",
)

# analyzer銘柄名 → news-botカテゴリキー のエイリアス
# (exact matchのみ。IG証券拡張時にここを増やす。未登録は素通り)
COMMODITY_ALIAS = {
    "WTI原油": "原油",
}


def load_news_items(symbol: str) -> list:
    """news-bot の raw ファイルから該当銘柄のニュースを取得し NewsItem 化して返す。

    - news_state.json / reports_state.json の両方を取得 (timeout=10)。
    - 取得・パース失敗したファイルはスキップ (analyzer は落とさない)。
    - commodity が symbol (エイリアス変換後) と exact match のものだけ採用。
    - timestamp は ISO8601 を datetime に復元 (naive は UTC とみなす)。
    """
    target = COMMODITY_ALIAS.get(symbol, symbol)
    items = []
    for url in NEWS_STATE_URLS:
        try:
            records = requests.get(url, timeout=10).json()
        except Exception:
            continue
        for d in records:
            if d.get("commodity") != target:
                continue
            try:
                ts = datetime.fromisoformat(d["timestamp"])
            except (KeyError, ValueError, TypeError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            items.append(NewsItem(
                title=d.get("title", ""),
                timestamp=ts,
                importance=d.get("importance", 1),
                direction=d.get("direction", "中立"),
                source=d.get("source", ""),
                commodity=d.get("commodity", ""),
                summary=d.get("summary", ""),
                # 旧レコード(フィールド欠落)は最も互換な既定値で補完
                event_type=d.get("event_type", "commentary"),
                surprise=d.get("surprise", "unknown"),
            ))
    return items


def setup_logger(config: dict):
    """ログ設定"""
    log_cfg = config.get('logging', {})
    log_file = log_cfg.get('file', 'logs/analyzer.log')
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    # Windows CP932端末でも絵文字・日本語が文字化けしないようUTF-8で出力
    utf8_stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True
    )
    handlers = [
        RotatingFileHandler(
            log_file,
            maxBytes=log_cfg.get('max_bytes', 10485760),
            backupCount=log_cfg.get('backup_count', 5),
            encoding='utf-8',
        ),
        logging.StreamHandler(utf8_stdout),
    ]

    logging.basicConfig(
        level=getattr(logging, log_cfg.get('level', 'INFO')),
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=handlers,
    )


def load_config(path: str = 'config/config.yaml') -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def analyze_one(symbol: dict, timeframe: dict, config: dict,
                api_key: str, dry_run: bool, no_filter: bool) -> dict:
    """
    1銘柄×1時間軸の分析を実行

    Returns:
        {'sent': bool, 'has_signal': bool, 'channel_type': str, 'error': str | None}
    """
    logger = logging.getLogger(__name__)
    name = symbol['name']
    ticker = symbol['ticker']
    tf_label = timeframe['label']

    logger.info(f"--- 分析開始: {name} / {tf_label}足 ---")

    # 1. データ取得
    df = fetch_ohlcv(
        ticker=ticker,
        interval=timeframe['interval'],
        period=timeframe['period'],
        resample=timeframe.get('resample'),
    )
    if df is None or df.empty:
        return {'sent': False, 'has_signal': False, 'channel_type': 'silent',
                'error': 'データ取得失敗'}

    df = df.tail(timeframe['lookback'])

    # 2. 指標計算
    df = add_all_indicators(df, config['indicators'])

    # 3. シグナル検出
    signal_result = detect_signals(df, config['signal_thresholds'])
    logger.info(f"  シグナル: {signal_result['summary']}")

    if not signal_result['has_signal'] and not no_filter:
        return {'sent': False, 'has_signal': False, 'channel_type': 'silent',
                'error': None}

    # 4. 指標スナップショット + TA期待度
    indicators_snapshot = build_indicator_snapshot(df)
    ta_expectation = calc_expectation(df, indicators_snapshot)
    logger.info(f"  TA期待度: {ta_expectation['score']:+.1f} ({ta_expectation['direction']})"
                f"  内訳: {ta_expectation['components']}")

    # 5. ニュース整合性係数 + 確信度スコア（決定論: TA指標＋ニュース整合性のみ）
    #    LLMはこの経路に一切関与しない。Gemini失敗がシグナル記録の欠損を生まないように、
    #    解説文生成（Gemini）は routing 確定後の step 7 まで遅延させる。
    news_items = load_news_items(name)
    ta_score = ta_expectation['score']
    ta_direction = 1 if ta_score > 0 else (-1 if ta_score < 0 else 0)
    alignment = calculate_alignment(news_items, ta_direction)
    conviction = calc_conviction(ta_expectation, alignment)
    logger.info(f"  確信度: {conviction['score']:+.1f}  係数: {alignment['coefficient']:.2f}"
                f"  ニュース: {alignment['news_count']}件(重要{alignment['high_importance_count']})"
                f"  divergence: {conviction['is_divergence']}")

    # 6. ルーティング（決定論）
    destination = get_destination(conviction, symbol)
    channel_type = destination['channel_type']
    logger.info(f"  チャンネル: {channel_label(channel_type)} ({channel_type})")

    # 6.5 シグナル永続記録（routing 決定直後・Gemini 呼び出しより前）。
    #     Gemini リトライ中にジョブが死んでも記録が残るよう、LLM より先に書く。
    #     reference以上、または --no-filter 時に記録。失敗しても本体は止めない。
    if channel_type != 'silent' or no_filter:
        try:
            record_signal({
                'symbol': name,
                'timeframe': tf_label,
                'ta_score': ta_expectation['score'],
                'conviction_score': conviction['score'],
                'coefficient': alignment['coefficient'],
                'direction': conviction['direction'],
                'is_divergence': conviction['is_divergence'],
                'net_direction': alignment['net_direction'],
                'news_count': alignment['news_count'],
                'high_importance_count': alignment['high_importance_count'],
                # shadow: マクロレジーム（記録のみ・conviction/routing不適用）。
                # プロセス内1回算出のキャッシュ。全ソース失敗時は None (= null で記録)。
                'macro': get_macro_state(),
            }, price_at_signal=float(df['Close'].iloc[-1]))
            logger.info(f"  記録: {pending_path_for(tf_label)}")
        except Exception:
            logger.warning("  ⚠ シグナル記録失敗（本体は継続）", exc_info=True)

    # silent は通知なし。reference 帯は通知スキップ・記録のみに格下げ
    # （Gemini 解説の生成対象は routing=commodity/critical/divergence のみ）。
    if not should_notify(channel_type):
        return {'sent': False, 'has_signal': True, 'channel_type': channel_type,
                'error': None}

    # 7. Gemini解説文生成（通知確定後のみ呼ぶ。embed 用テキスト専任）
    #    失敗してもフォールバック文で通知し、記録・routing には一切影響しない。
    is_short_tf = timeframe['label'] in ('15分', '1時間')
    model_name = (config['gemini']['model_short']
                  if is_short_tf else config['gemini']['model_long'])

    gemini_result = analyze_with_gemini(
        api_key=api_key,
        model_name=model_name,
        symbol_name=name,
        timeframe=tf_label,
        indicators=indicators_snapshot,
        signals=signal_result['signals'],
        max_output_tokens=config['gemini']['max_output_tokens'],
        temperature=config['gemini']['temperature'],
    )

    if gemini_result:
        analysis_text = gemini_result['text']
        logger.info(f"  解説文: {len(analysis_text)}文字")
    else:
        analysis_text = "(TA所見の生成に失敗)"
        logger.warning("  ⚠ 解説文生成失敗 → フォールバック文で続行")

    if dry_run:
        conviction_summary = format_conviction_summary(conviction)
        logger.info(f"  [DRY-RUN] 投稿スキップ\n"
                    f"  {conviction_summary}\n"
                    f"  {analysis_text[:200]}...")
        return {'sent': False, 'has_signal': True, 'channel_type': channel_type,
                'error': None}

    # 8. Discord投稿
    webhook_url = resolve_webhook_url(destination)
    if not webhook_url:
        env_key = destination['webhook_env']
        return {'sent': False, 'has_signal': True, 'channel_type': channel_type,
                'error': f"環境変数 {env_key} 未設定"}

    # 表示上のimportance: GeminiとTA複合シグナル数の強い方（Gemini失敗時はTAのみ）
    effective_importance = (
        'high' if ((gemini_result and gemini_result['importance'] == 'high')
                   or signal_result['importance'] == 'high')
        else 'normal'
    )

    success = send_to_discord(
        webhook_url=webhook_url,
        symbol_name=name,
        timeframe=tf_label,
        price=float(df['Close'].iloc[-1]),
        signals=signal_result['signals'],
        importance=effective_importance,
        analysis_text=analysis_text,
        conviction_info=format_conviction_summary(conviction),
    )

    if success:
        logger.info(f"  Discord投稿OK → {channel_type}")
    return {'sent': success, 'has_signal': True, 'channel_type': channel_type,
            'error': None if success else 'Discord投稿失敗'}


def main():
    parser = argparse.ArgumentParser(description='コモディティテクニカル分析Bot')
    parser.add_argument('--tf', help='特定時間軸のみ（例: 15分, 1時間, 4時間, 日足）')
    parser.add_argument('--symbol', help='特定銘柄のみ（例: 金）')
    parser.add_argument('--dry-run', action='store_true',
                        help='Discord投稿しない（テスト用）')
    parser.add_argument('--no-filter', action='store_true',
                        help='シグナルなしでも通知')
    parser.add_argument('--config', default='config/config.yaml',
                        help='設定ファイルパス')
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logger(config)
    logger = logging.getLogger(__name__)

    load_dotenv()
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        logger.error("GEMINI_API_KEY が .env に設定されていません")
        sys.exit(1)

    symbols = config['symbols']
    timeframes = config['timeframes']
    if args.symbol:
        symbols = [s for s in symbols if s['name'] == args.symbol]
    if args.tf:
        timeframes = [tf for tf in timeframes if tf['label'] == args.tf]

    if not symbols or not timeframes:
        logger.error("対象なし。指定を確認してください")
        sys.exit(1)

    logger.info(f"=== 開始: {len(symbols)}銘柄 × {len(timeframes)}時間軸 ===")
    stats = {'total': 0, 'sent': 0, 'signal': 0, 'error': 0,
             'critical': 0, 'divergence': 0, 'reference': 0, 'silent': 0}

    for symbol in symbols:
        for tf in timeframes:
            stats['total'] += 1
            try:
                result = analyze_one(symbol, tf, config, api_key,
                                     args.dry_run, args.no_filter)
                if result['sent']:
                    stats['sent'] += 1
                if result['has_signal']:
                    stats['signal'] += 1
                channel = result.get('channel_type', 'silent')
                if channel in stats:
                    stats[channel] += 1
                if result['error']:
                    stats['error'] += 1
                    logger.warning(f"  ⚠ {result['error']}")
            except Exception:
                stats['error'] += 1
                logger.exception(f"想定外エラー: {symbol['name']}/{tf['label']}")

    logger.info(
        f"=== 完了: 総数{stats['total']} / シグナル{stats['signal']} / 投稿{stats['sent']} / "
        f"エラー{stats['error']} | "
        f"critical:{stats['critical']} divergence:{stats['divergence']} "
        f"ref:{stats['reference']} silent:{stats['silent']} ==="
    )


if __name__ == '__main__':
    main()

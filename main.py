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
from logging.handlers import RotatingFileHandler
from pathlib import Path

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
from news_alignment import calc_alignment_coefficient
from conviction_scorer import calc_conviction, format_conviction_summary
from notification_router import get_destination, resolve_webhook_url, channel_label


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

    # 5. Gemini分析 (direction付きJSON)
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

    if not gemini_result:
        return {'sent': False, 'has_signal': True, 'channel_type': 'silent',
                'error': 'Gemini分析失敗'}

    analysis_text = gemini_result['text']
    logger.info(f"  Gemini方向: {gemini_result['direction']}  分析テキスト: {len(analysis_text)}文字")

    # 6. ニュース整合性係数 + 確信度スコア
    alignment = calc_alignment_coefficient(
        news_direction=gemini_result['direction'],
        ta_direction=ta_expectation['direction'],
        ta_score=ta_expectation['score'],
        # is_authority_source / news_age_hours はニュースフィード統合時に使用
    )
    conviction = calc_conviction(ta_expectation, alignment)
    logger.info(f"  確信度: {conviction['score']:+.1f}  係数: {alignment['coefficient']:.2f}"
                f"  divergence: {conviction['is_divergence']}")

    # 7. ルーティング
    destination = get_destination(conviction, symbol)
    channel_type = destination['channel_type']
    logger.info(f"  チャンネル: {channel_label(channel_type)} ({channel_type})")

    if channel_type == 'silent':
        return {'sent': False, 'has_signal': True, 'channel_type': 'silent',
                'error': None}

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

    # importanceはGeminiとTA複合シグナル数の強い方を採用
    effective_importance = (
        'high' if (gemini_result['importance'] == 'high'
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

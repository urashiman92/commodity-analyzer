# コモディティテクニカル分析Bot

yfinance + Gemini API + Discord webhookを使って、コモディティ6銘柄のテクニカル分析を自動化するBot。

**月額0円**で稼働します。

## 構成

```
データ取得：yfinance（無料、無制限）
指標計算：自前ロジック（pandas/numpy）
シグナル判定：自前ロジック
AI分析：Gemini 2.5 Flash / Flash-Lite（無料枠）
通知：Discord webhook（無料）
```

## 監視銘柄

| 銘柄 | yfinanceシンボル |
|---|---|
| 小麦 | ZW=F |
| 金 | GC=F |
| WTI原油 | CL=F |
| トウモロコシ | ZC=F |
| 大豆 | ZS=F |
| 銅 | HG=F |

## 監視時間軸

- 15分足
- 1時間足
- 4時間足（1時間足からリサンプリング）
- 日足

## セットアップ手順

### 1. Pythonバージョン確認

Python 3.10以降が必要。

```powershell
python --version
```

### 2. プロジェクト配置

このフォルダ一式を任意の場所に配置（例: `C:\Tools\commodity_analyzer`）。

### 3. 仮想環境作成 & ライブラリインストール

```powershell
cd C:\Tools\commodity_analyzer
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 4. 環境変数の設定

`.env.example` を `.env` にコピーし、実際の値を記入：

```powershell
copy .env.example .env
notepad .env
```

#### Gemini APIキー取得

1. https://aistudio.google.com にアクセス
2. Googleでログイン
3. 左メニュー「Get API key」→「Create API key」
4. キーをコピーして`.env`の`GEMINI_API_KEY=`に貼り付け

#### Discord webhook URL取得（6銘柄分）

各銘柄のDiscordチャンネルで：
1. チャンネル名右の「⚙️」→「連携サービス」→「ウェブフック」
2. 「新しいウェブフック」→ 名前を設定
3. 「ウェブフックURLをコピー」
4. `.env`の該当`WEBHOOK_XXX=`に貼り付け

### 5. 動作テスト

#### ドライラン（Discord投稿なし）

```powershell
python main.py --dry-run --no-filter --symbol 金 --tf 日足
```

ログに「分析テキスト生成OK」が出れば成功。

#### 本番テスト（1銘柄1時間軸だけ）

```powershell
python main.py --no-filter --symbol 金 --tf 日足
```

Discord「金」チャンネルにメッセージが届けば成功。

#### 全銘柄分析（手動実行）

```powershell
python main.py
```

シグナル検出された銘柄・時間軸のみ通知されます。

## オプション

| オプション | 説明 |
|---|---|
| `--tf 15分` | 特定時間軸のみ実行 |
| `--symbol 金` | 特定銘柄のみ実行 |
| `--dry-run` | Discord投稿せずログのみ |
| `--no-filter` | シグナルなしでも通知 |

## 自動実行設定（Windowsタスクスケジューラ）

### 推奨スケジュール

| 時間軸 | 実行頻度 | コマンド |
|---|---|---|
| 15分足 | 15分ごと | `python main.py --tf 15分` |
| 1時間足 | 1時間ごと | `python main.py --tf 1時間` |
| 4時間足 | 4時間ごと | `python main.py --tf 4時間` |
| 日足 | 1日1回（朝7時） | `python main.py --tf 日足` |

### 実行用バッチファイル作成

`run_15min.bat` を作成：

```batch
@echo off
cd /d C:\Tools\commodity_analyzer
call venv\Scripts\activate
python main.py --tf 15分
```

同様に各時間軸用のバッチを作成。

### タスクスケジューラ登録

1. Windowsキー → 「タスク スケジューラ」を起動
2. 右ペイン「基本タスクの作成」
3. 名前：例「テクニカル分析-15分」
4. トリガー：日単位、開始時刻設定、繰り返し間隔=15分、継続期間=1日
5. 操作：プログラム開始 → `run_15min.bat` を指定
6. 完了

各時間軸ごとに繰り返し。

## ログ確認

```powershell
type logs\analyzer.log | more
```

エラーや動作状況はすべて`logs/analyzer.log`に記録。

## トラブルシューティング

### yfinanceで「データ取得失敗」が頻発

- ネットワーク確認
- yfinanceを最新版に：`pip install -U yfinance`

### Gemini APIエラー（429: Quota exceeded）

- 無料枠オーバー。15分足の頻度を下げるか、Flash-Liteに切り替え
- `config.yaml`の`model_short`を確認

### Discord投稿が失敗（401, 404）

- webhook URLが無効。チャンネル設定から再発行
- `.env`を保存後、`venv`を一度activateし直す

### 「シグナルなし」ばかり出る

- これは正常動作。シグナル検出されない時間帯はDiscord投稿されません
- テストするときは `--no-filter` で強制通知

## カスタマイズ

### シグナル条件を変更

`config/config.yaml`の`signal_thresholds`を編集：

```yaml
signal_thresholds:
  rsi_overbought: 75    # 70 → 75に変更（より厳しく）
  rsi_oversold: 25
```

### 銘柄を追加

`config/config.yaml`の`symbols`に追加：

```yaml
- name: "天然ガス"
  ticker: "NG=F"
  webhook_env: "WEBHOOK_GAS"
```

`.env`にも`WEBHOOK_GAS=...`を追加。

## ファイル構成

```
commodity_analyzer/
├── main.py                  # メインスクリプト
├── requirements.txt         # 依存ライブラリ
├── .env.example             # 環境変数テンプレート
├── .env                     # 実際の環境変数（git除外）
├── README.md                # この文書
├── config/
│   └── config.yaml          # 銘柄・時間軸・閾値の設定
├── src/
│   ├── data_fetcher.py      # yfinanceからデータ取得
│   ├── indicators.py        # テクニカル指標計算
│   ├── signal_detector.py   # シグナル検出ロジック
│   ├── gemini_analyzer.py   # Gemini API呼び出し
│   └── discord_notifier.py  # Discord投稿
└── logs/
    └── analyzer.log         # 実行ログ
```

## ライセンス

個人利用前提。再配布の際は注意。

# commodity-analyzer 恒久原則

このリポジトリ（および連携先 commodity-news-bot）で変更を加えるときに必ず守る設計原則。
過去に実際に踏んだ事故・バグから確立されたものなので、例外を作る場合は明示的な合意を取ること。

## 1. 一ファイル一ライター

git コミットされる状態ファイル（*.json / *.jsonl）は、**書き手となるプロセスを1つに限定**する。
複数のワークフローが同一ファイルに書くと GitHub Actions の push が競合する（seen.json で実害）。

- 例: main.py → news_state.json、reports.py → reports_state.json（news-bot）
- 競合は「時刻をずらして確率を下げる」のではなく「書き手を分けて構造的にゼロにする」。

### シグナル検証基盤の書き手マップ（正準）

| ファイル | 書き手 | モード |
|---|---|---|
| `signal_pending_4h.jsonl` | ta-4h 実行の analyzer 本体のみ | append 専用 |
| `signal_pending_1d.jsonl` | ta-1d 実行の analyzer 本体のみ | append 専用 |
| `signal_history.jsonl`（+月次アーカイブ） | verify-signals のみ | append 専用 |
| `cot_state.json` | cot-weekly のみ | 全書き（週次上書き） |
| `term_raw.jsonl` | term-archive のみ | append 専用（日次・(date,contract_id)で冪等） |

- verifier は pending を**読み取り専用**で走査。pending からの削除・移動は誰もしない（追記専用ログ）。
- 重複防止は `signal_id`（sha1(timestamp\|symbol\|timeframe)）の idempotency（history 既載スキップ）で行う。
- Actions ランナー上で書いたファイルは **commit ステップで push しなければ消える**。
  書き手ワークフローには必ず自分のファイルだけを `git add` する commit ステップ（rebase+3リトライ+[skip ci]）を付け、
  `permissions: contents: write` を忘れない（既定 GITHUB_TOKEN は read）。
- glob を `git add` に直接書かない（`signal_history_*.jsonl` が未解決だと pathspec エラーで job ごと落ちる。実害済み）。
  存在チェック付きループで add する。

## 2. LLMは言語境界のみ（数値経路に入れない）

LLM（Gemini/Haiku）の出力が **確信度・係数・divergence・routing・シグナル記録に影響してはならない**。
数値経路は TA指標＋ニュース整合性の決定論で完結させる。LLMの役割は:

- 通知時の解説文・要約テキストの生成（embed用）
- ニュース記事の属性分類（news-bot 側の impact/importance/event_type/surprise）

LLM呼び出しは routing 確定後にのみ行い、失敗時はフォールバック文で通知を続行する。
LLM失敗がシグナル記録の欠損（ランダムでないデータ欠損）を生む構造を作らない。

## 3. 新ファクターは必ず shadow から開始

新しいスコア要素・係数を追加するときは、まず**記録のみ（conviction に不適用）**で signal_pending/history に書き、
signal_history の成績（方向一致率・%リターン）で有効性を確認してから昇格させる。
いきなり本番の確信度計算に組み込まない。

## 4. JSONLスキーマは追加のみ・読み手は欠落フィールド耐性

- state/履歴ファイルのスキーマ変更は**フィールド追加のみ**（リネーム・削除・意味変更は不可）。
- 例外: フィールド除去は「**全読み手の非参照を grep で確認済み**」の場合のみ可。
  リネーム・意味変更は引き続き不可（必要なら新フィールド追加で対応する）。
- 読み手は欠落フィールドに必ずデフォルト値を与える（`d.get(key, default)`）。
  旧レコードが混在しても落ちないこと（例: event_type→"commentary", surprise→"unknown"）。

## 5. タイムスタンプは UTC tz-aware ISO8601

素の `datetime.now()`（naive）は禁止。`datetime.now(timezone.utc).isoformat()` を使う。
読み手は naive を受け取ったら UTC とみなして補完する。時間減衰・ホライズン照合が naive で壊れる。

## 6. cost-zero 優先

新規のLLM呼び出し・有料APIを足さない。既存呼び出しのプロンプト拡張・結果の使い回しで実現する。
（Gemini無料枠: flash 20req/日・5req/分、flash-lite 20req/日。超過は 429 RESOURCE_EXHAUSTED）

---

## 実行・検証のメモ

- ローカル実行は `venv\Scripts\python.exe`（system python に deps なし）。`.env` に GEMINI_API_KEY。
- 検証は直接呼び出しの使い捨てテスト推奨（フルランは Gemini 課金/枠消費 + Discord 実投稿）。
- dry-run: `python main.py --tf 日足 --dry-run`（--no-filter で全銘柄を確信度算出まで通す）。
- 銘柄キー: analyzer は `WTI原油`、news-bot は `原油`。`COMMODITY_ALIAS` で吸収。
- **Gemini 無料枠の実測値: RPD 20req/日・RPM 5req/分**（超過は 429 RESOURCE_EXHAUSTED、
  quotaId の `PerDay`/`PerMinute` で判別可）。RPD超過は gemini_analyzer のプロセス内
  サーキットブレーカーが以後の呼び出しを全スキップする。
- reference帯（|確信度|15〜30）は**通知スキップ・シグナル記録のみ**。Gemini解説の生成対象は
  routing=commodity/critical/divergence のみ（WEBHOOK_REFERENCE は廃止済み・Secret不要）。

# -*- coding: utf-8 -*-
"""
原油 限月構造の生値アーカイブ（Phase4 後始末・書き手）

term-archive.yml（日次・単独ジョブ）からのみ実行される。term_raw.jsonl の
唯一の書き手（CLAUDE.md 書き手マップ参照・append専用）。

  python scripts/fetch_term_raw.py

■ 記録するもの＝事実のみ:
  上場中の期近3限月の {date, contract_id, close} を1行ずつ追記。
  **F1選定・ロール規則・導出値（roll_yield等）は一切適用しない**。
  導出は将来の分析側（再監査・Stage2再開後）で行う。
  ※再監査合格前に、このアーカイブから derived を「便利だから」と
    使い始めないこと（ドリフト警戒。CLAUDE.md 原則③）。

■ 限月の解決（実測済みの形式 CL{月}{YY}.NYM）:
  当月+1〜+8 の受渡月を順に叩き、直近7日以内の足を返す最初の3本を採用。
  期限切れ限月は Yahoo が 404 で消すため（実測）、自然に次の限月へ進む。

■ 冪等性: (date, contract_id) の組が既存なら追記しない。
■ fail-loud: 3限月そろわない日は exit 1 でスキップ（runが赤くなる・翌日自然再開）。
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

RAW_PATH = "term_raw.jsonl"
MONTH_CODES = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
               7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}
NEED = 3          # 期近3限月
FRESH_DAYS = 7    # 直近足がこれより古いティッカーは死んでいるとみなす


def candidate_tickers(today) -> list:
    """当月+1〜+8 受渡月のティッカー候補（期近から順）。"""
    out = []
    y, m = today.year, today.month
    for k in range(1, 9):
        mm = m + k
        yy = y + (mm - 1) // 12
        mm = (mm - 1) % 12 + 1
        out.append(f"CL{MONTH_CODES[mm]}{str(yy)[2:]}.NYM")
    return out


def fetch_latest(ticker: str):
    """最新足の (date_str, close) を返す。取得不能・古すぎは None。"""
    try:
        df = yf.download(ticker, interval="1d", period="5d",
                         progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        s = df["Close"].dropna()
        if s.empty:
            return None
        last_dt = s.index[-1]
        if (datetime.now(timezone.utc).date() - last_dt.date()) > timedelta(days=FRESH_DAYS):
            return None
        return (str(last_dt.date()), float(s.iloc[-1]))
    except Exception:
        return None


def existing_pairs() -> set:
    out = set()
    if os.path.exists(RAW_PATH):
        with open(RAW_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    out.add((r.get("date"), r.get("contract_id")))
                except json.JSONDecodeError:
                    continue
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    today = datetime.now(timezone.utc).date()
    got = []
    for t in candidate_tickers(today):
        r = fetch_latest(t)
        if r is not None:
            got.append((t, r[0], r[1]))
            print(f"  {t}: {r[0]} close={r[1]}")
        if len(got) == NEED:
            break

    if len(got) < NEED:
        print(f"❌ 期近{NEED}限月がそろわない（取得{len(got)}本）。"
              f"この日はスキップ（翌日自然再開）。")
        sys.exit(1)

    seen = existing_pairs()
    new_rows = [{"date": d, "contract_id": t, "close": c}
                for t, d, c in got if (d, t) not in seen]
    if not new_rows:
        print("変更なし（同date+contractは記録済み）。")
        return
    with open(RAW_PATH, "a", encoding="utf-8") as f:
        for r in new_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"追記: {len(new_rows)}行 → {RAW_PATH}")


if __name__ == "__main__":
    main()

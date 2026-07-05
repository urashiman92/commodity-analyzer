# -*- coding: utf-8 -*-
"""
CoT状態ファイル更新（Phase2 shadow・書き手）

cot-weekly.yml（週次・単独ジョブ）からのみ実行される。cot_state.json の
唯一の書き手（CLAUDE.md 書き手マップ参照）。analyzer 本体は読むだけ。

  python scripts/update_cot_state.py

データ: CFTC Socrata API（72hh-3qpy = Disaggregated Futures Only）。
契約コードは Stage1（research/cot_offline_report.md）で実データ確認済みの確定値。
祝日週の公表遅延: 取得できた最新 as_of をそのまま記録する（鮮度の判定は
analyzer 側が age_days で行う。このジョブは「最新を写す」だけ）。
銘柄単位の取得失敗時は、既存 cot_state.json のその銘柄エントリを温存する
（一時的なAPI障害で状態を失わない）。
"""
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd
import requests

SOCRATA = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
STATE_PATH = "cot_state.json"
ROLL_W = 156  # trailing 3年（週次）。research/cot_offline_check.py と同一定義。

# Stage1 で実データ確認済みの契約コード（推測禁止ルールに基づく確定値）
# 注意: NYMEX WTI の名義は「WTI-PHYSICAL」（CRUDE 検索には出ない）
CONTRACTS = {
    "小麦": "001602",        # WHEAT-SRW - CHICAGO BOARD OF TRADE
    "金": "088691",          # GOLD - COMMODITY EXCHANGE INC.
    "WTI原油": "067651",     # WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE
    "トウモロコシ": "002602",  # CORN - CHICAGO BOARD OF TRADE
    "大豆": "005602",        # SOYBEANS - CHICAGO BOARD OF TRADE
    "銅": "085692",          # COPPER- #1 - COMMODITY EXCHANGE INC.
}


def fetch_series(code: str) -> pd.DataFrame | None:
    """trailing pctl 算出に足る約3.4年の週次系列を取得。失敗は None。"""
    since = (datetime.now(timezone.utc) - pd.DateOffset(years=3, months=5)).strftime("%Y-%m-%d")
    try:
        r = requests.get(SOCRATA, params={
            "$select": ("report_date_as_yyyy_mm_dd,"
                        "m_money_positions_long_all,m_money_positions_short_all,"
                        "open_interest_all"),
            "$where": (f"cftc_contract_market_code='{code}' "
                       f"AND report_date_as_yyyy_mm_dd>='{since}'"),
            "$order": "report_date_as_yyyy_mm_dd",
            "$limit": 300,
        }, timeout=60)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["as_of"] = pd.to_datetime(df["report_date_as_yyyy_mm_dd"])
        for c in ("m_money_positions_long_all", "m_money_positions_short_all",
                  "open_interest_all"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna().sort_values("as_of").reset_index(drop=True)
        # trailing 窓が組めない銘柄は品質不足として不採用
        return df if len(df) >= ROLL_W + 1 else None
    except Exception:
        return None


def compute_entry(df: pd.DataFrame) -> dict:
    """最新レポートの shadow 記録値を算出（research と同一定義）。"""
    mm_net = ((df["m_money_positions_long_all"] - df["m_money_positions_short_all"])
              / df["open_interest_all"])
    wow = mm_net.diff()

    window = mm_net.iloc[-ROLL_W:]
    latest = float(mm_net.iloc[-1])
    pctl = float((window <= latest).mean()) * 100

    wow_window = wow.iloc[-ROLL_W:].dropna()
    latest_wow = float(wow.iloc[-1])
    q05 = float(wow_window.quantile(0.05))
    q95 = float(wow_window.quantile(0.95))
    wow_tail = "high" if latest_wow >= q95 else ("low" if latest_wow <= q05 else None)

    return {
        "as_of": df["as_of"].iloc[-1].strftime("%Y-%m-%d"),
        "mm_net": round(latest, 5),
        "pctl": round(pctl, 1),
        "wow": round(latest_wow, 5),
        "wow_tail": wow_tail,
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # 既存stateを読み、失敗銘柄はエントリ温存
    old_symbols = {}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                old_symbols = (json.load(f) or {}).get("symbols", {})
        except Exception:
            old_symbols = {}

    symbols = {}
    ok = failed = 0
    for name, code in CONTRACTS.items():
        df = fetch_series(code)
        if df is not None:
            symbols[name] = compute_entry(df)
            ok += 1
            print(f"  {name}: as_of={symbols[name]['as_of']} pctl={symbols[name]['pctl']} "
                  f"wow_tail={symbols[name]['wow_tail']}")
        elif name in old_symbols:
            symbols[name] = old_symbols[name]
            failed += 1
            print(f"  {name}: 取得失敗 → 既存エントリ温存 (as_of={old_symbols[name].get('as_of')})")
        else:
            failed += 1
            print(f"  {name}: 取得失敗（既存エントリなし・スキップ）")

    # 全滅は run を赤くする（fail-loud の流儀。部分成功では落とさない）。
    # 「変更なしスキップ」より先に判定する（全滅+温存で同一内容になっても隠れないように）。
    if ok == 0:
        print("❌ 全銘柄の取得に失敗。Socrata疎通/契約コードを確認してください。")
        sys.exit(1)

    # symbols の内容が既存と同一なら書き換えない（fetched_at だけの差分で
    # 週次コミットが無駄に発生するのを防ぐ。CFTC未更新週は「変更なし」で終わる）
    if symbols == old_symbols:
        print(f"変更なし（as_of更新なし・成功{ok}/失敗{failed}）。書き換えスキップ。")
        return

    state = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols,
    }
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f"更新完了: 成功{ok} / 失敗{failed} → {STATE_PATH}")


if __name__ == "__main__":
    main()

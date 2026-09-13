# -*- coding: utf-8 -*-
"""EIA週次石油在庫の発表日時ルールのユニットテスト（pytest 非依存・stdlib のみ）

  python tests/test_event_gate_eia.py     # 全PASSで exit 0

守っている契約（出典: EIA 公式 Holiday Release Schedule
https://www.eia.gov/petroleum/supply/weekly/schedule.php・2026-09-13 確認）:
  - 通常週: 水曜 10:30 a.m. ET
  - シフト週（週前半 月〜水に連邦祝日）: 木曜 12:00 p.m. ET
    ※ 2026-09-13 に 11:00 → 12:00 へ訂正（protocol v1.12）。
      本テストは同じ誤りの再発（時刻のドリフト）を検出するために置く。
"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import event_gate as eg  # noqa: E402

ET = ZoneInfo("America/New_York")
_failed = []


def check(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n      got={got!r}\n      want={want!r}"))
    if not ok:
        _failed.append(name)


def et_of(dt):
    """UTC aware datetime → ET の (date, hh, mm)。"""
    e = dt.astimezone(ET)
    return (e.date(), e.hour, e.minute)


# 実際の祝日リスト（config/events.yaml）を使う。設定と実装のズレも同時に検出する。
HOLIDAYS = eg._holidays(eg._load_config())


def main():
    print("=== 定数（protocol v1.12 の訂正値）===")
    check("EIA_NORMAL_ET が 水曜10:30", eg.EIA_NORMAL_ET, (10, 30))
    check("EIA_SHIFTED_ET が 木曜12:00", eg.EIA_SHIFTED_ET, (12, 0))

    print("\n=== シフト週: 日 と 時刻（12:00 ET）===")
    # Veterans Day 2026-11-11(水) を含む週。公式: データ週11/6 → 振替 11/12木 12:00 p.m.
    got = eg._gen_eia_weekly(date(2026, 11, 9), HOLIDAYS)
    check("Veterans週(11/9起点) → 11/12(木) 12:00 ET",
          et_of(got), (date(2026, 11, 12), 12, 0))
    # EST 期間なので UTC は 17:00
    check("Veterans週 → 17:00Z（EST=UTC-5）",
          got, datetime(2026, 11, 12, 17, 0, tzinfo=timezone.utc))

    # Labor Day 2026-09-07(月)
    check("Labor Day週(9/7起点) → 9/10(木) 12:00 ET",
          et_of(eg._gen_eia_weekly(date(2026, 9, 7), HOLIDAYS)),
          (date(2026, 9, 10), 12, 0))
    # EDT 期間なので UTC は 16:00
    check("Labor Day週 → 16:00Z（EDT=UTC-4）",
          eg._gen_eia_weekly(date(2026, 9, 7), HOLIDAYS),
          datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc))

    # Columbus Day 2026-10-12(月)
    check("Columbus週(10/12起点) → 10/15(木) 12:00 ET",
          et_of(eg._gen_eia_weekly(date(2026, 10, 12), HOLIDAYS)),
          (date(2026, 10, 15), 12, 0))

    print("\n=== 通常週: 無変更（水曜10:30 ET）===")
    check("通常週(11/16起点) → 11/18(水) 10:30 ET",
          et_of(eg._gen_eia_weekly(date(2026, 11, 16), HOLIDAYS)),
          (date(2026, 11, 18), 10, 30))
    check("通常週(9/14起点) → 9/16(水) 10:30 ET",
          et_of(eg._gen_eia_weekly(date(2026, 9, 14), HOLIDAYS)),
          (date(2026, 9, 16), 10, 30))

    print("\n=== Thanksgiving: 木曜祝日はシフトしない（公式表に振替行なし）===")
    check("Thanksgiving週(11/23起点) → 11/25(水) 10:30 ET",
          et_of(eg._gen_eia_weekly(date(2026, 11, 23), HOLIDAYS)),
          (date(2026, 11, 25), 10, 30))

    print("\n=== 祝日リストの前提（config/events.yaml）===")
    check("2026-11-11 が祝日として登録", date(2026, 11, 11) in HOLIDAYS, True)
    check("2026-11-26 が祝日として登録", date(2026, 11, 26) in HOLIDAYS, True)

    print()
    if _failed:
        print(f"SOME FAILED ({len(_failed)}): {_failed}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

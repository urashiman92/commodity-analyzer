# -*- coding: utf-8 -*-
"""
マイルストーン通知（「やるべきことが来た」時だけ Discord に投稿。平時は沈黙）

読み取り専用（唯一の書き込みは milestone_state.json = 本ジョブが唯一の書き手）。
判定ロジック・パイプラインには一切触れない。集計は report_validation を再利用する。

  python scripts/check_milestones.py                 # 本番（WEBHOOK_CRITICAL に投稿）
  python scripts/check_milestones.py --dry-run       # 投稿せず標準出力のみ
  python scripts/check_milestones.py --n-threshold 5 --state /tmp/s.json  # 受け入れ確認用

チェック内容:
  A. 状態ベース  … アクティブなテストの主集計 n が全群 >= 閾値(既定100)に到達
                    ※ 一致率は通知しない（判定前に数字の印象を入れないため）
  B. 日付ベース  … 毎月第1営業日の events.yaml 更新リマインド /
                    登録イベントの枯渇（最遠イベントが30日以内）/ term再監査の到達
  C. 死活        … cot_state / term_raw / signal_pending の鮮度

再通知の抑制: milestone_state.json に通知済みキーを保持し同一項目は1回のみ。
死活アラートは条件が解消したらフラグを消す（再発時は再通知される）。
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
os.chdir(ROOT)  # report_validation の glob は cwd 相対のため repo ルートに固定

import report_validation as rv  # noqa: E402  (chdir 後に読む)

STATE_PATH_DEFAULT = "milestone_state.json"
EVENTS_YAML = "config/events.yaml"

# --- 鮮度の許容日数（C. 死活） ---
COT_STALE_DAYS = 14
TERM_STALE_DAYS = 5
PENDING_STALE_DAYS = 7
# --- B. 日付ベース ---
EVENTS_EXHAUST_DAYS = 30           # 最遠イベントがこれ以内なら「登録が尽きる」警告
TERM_REAUDIT_FROM = date(2027, 1, 1)
TERM_REAUDIT_MIN_CONTRACTS = 6     # front切替6回以上（= contract_id ユニーク6以上）

# A. 対象テスト（v1 TAバケットは 2026-08-29 に判定済みのため対象外）
ACTIVE_TESTS = [
    ("v1.11 ニュース存在の劣化テスト", "v111", lambda: rv._v111_group, ["無風", "ニュース"]),
    ("v1.8 両通知エントリー", "v18", lambda: rv._entry_group, ["両通知", "対照"]),
    ("v1.4 イベントゲート", "v14", lambda: rv._event_group, ["pre", "ウィンドウ外"]),
    ("v1.2 ニュース層帰属", "v12", lambda: rv._news_group, ["減衰", "中立", "増幅"]),
    ("v1.7 流動性トリガー", "v17", lambda: rv._liq_group, ["一致", "不一致"]),
]


# ------------------------------------------------------------------ state

def load_state(path):
    if not os.path.exists(path):
        return {"notified": {}}
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"notified": {}}
    st.setdefault("notified", {})
    return st


def save_state(path, state):
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


# ------------------------------------------------------------------ helpers

def group_n(history, group_fn, label):
    """主集計 n（168h の方向一致が判定可能な件数）= report_validation の hit_cell と同一定義。"""
    return sum(1 for r in history
               if group_fn(r) == label and rv.ta_hit(r, "168h") is not None)


def _parse_dt(s):
    try:
        dt = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def age_days(dt, now):
    return None if dt is None else (now - dt).total_seconds() / 86400.0


def first_business_day(year, month):
    """その月の第1営業日（月〜金。連邦祝日は考慮しない = リマインド用途には十分）。"""
    for day in range(1, 8):
        d = date(year, month, day)
        if d.weekday() < 5:
            return d
    return date(year, month, 1)


def read_jsonl_last(path):
    """最終行の dict（無ければ None）。"""
    if not os.path.exists(path):
        return None
    last = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                last = line
    if last is None:
        return None
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        return None


# ------------------------------------------------------------------ checks

def check_sample_size(history, n_threshold):
    """A. 各アクティブテストの全群が n>=閾値 に到達したら通知（一致率は載せない）。"""
    out = []
    for title, key, fn_getter, labels in ACTIVE_TESTS:
        fn = fn_getter()
        counts = {g: group_n(history, fn, g) for g in labels}
        if counts and all(v >= n_threshold for v in counts.values()):
            detail = " / ".join(f"{g} n={counts[g]}" for g in labels)
            out.append((
                f"milestone:{key}",
                f"📊 **{title}**: 全群が n>={n_threshold} に到達しました（{detail}）。"
                f"\n→ **本判定の実行時期です**（判定はセッションで正式に実施）。",
            ))
    return out


def check_events_yaml(now):
    """B. 毎月第1営業日のリマインド / 登録イベントの枯渇警告。"""
    out = []
    today = now.date()

    if today == first_business_day(today.year, today.month):
        out.append((
            f"monthly_events:{today.year:04d}-{today.month:02d}",
            "🗓 **月次リマインド: config/events.yaml の更新**\n"
            "→ OPEC 会合日 / BLS 雇用統計 / WASDE の確定日程を確認して追記してください。\n"
            "（bls.gov・opec.org は Code 環境から403のため、チャット側 Claude に確認依頼）",
        ))

    try:
        import yaml
        with open(EVENTS_YAML, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        dts = [d for d in (_parse_dt(e.get("datetime_utc"))
                           for e in (cfg.get("events") or [])) if d]
    except Exception as e:  # yaml 不読・破損でジョブを落とさない
        return out + [("health:events_unreadable",
                       f"⚠️ **config/events.yaml が読めません**: {e}")]

    if not dts:
        out.append(("events_exhausted:none",
                    "⚠️ **events.yaml に手動登録イベントが1件もありません**（登録が尽きています）。"))
        return out

    farthest = max(dts)
    remain = (farthest - now).total_seconds() / 86400.0
    if remain <= EVENTS_EXHAUST_DAYS:
        out.append((
            f"events_exhausted:{farthest.date().isoformat()}",
            f"⚠️ **events.yaml の登録が尽きます**: 最も遠いイベントは "
            f"{farthest.date().isoformat()}（残り {remain:.0f} 日）。\n"
            "→ 次期分の FOMC/WASDE/Grain Stocks/雇用統計/OPEC を追記してください。",
        ))
    return out


def check_term_reaudit(now):
    """B. 2027-01-01 以降、term_raw の contract_id ユニーク数が6以上なら再監査可能。"""
    if now.date() < TERM_REAUDIT_FROM:
        return []
    ids = set()
    if os.path.exists("term_raw.jsonl"):
        with open("term_raw.jsonl", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    cid = json.loads(line).get("contract_id")
                except json.JSONDecodeError:
                    continue
                if cid:
                    ids.add(cid)
    if len(ids) >= TERM_REAUDIT_MIN_CONTRACTS:
        return [("term_reaudit",
                 f"🧪 **term structure 再監査が可能になりました**: "
                 f"contract_id ユニーク数 {len(ids)}（>= {TERM_REAUDIT_MIN_CONTRACTS}）。\n"
                 "→ 基準D（ロール段差3σ）を**同一定義**で再監査してください。"
                 "合格なら Stage2 再開・protocol v1.6 充填。")]
    return []


def check_health(now):
    """C. 死活。(key, msg, is_alert) を返す。解消時はフラグを消せるよう is_alert=False も返す。"""
    out = []

    # cot_state.json: symbols[*].as_of の最新
    as_of = None
    if os.path.exists("cot_state.json"):
        try:
            with open("cot_state.json", encoding="utf-8") as f:
                cot = json.load(f)
            cands = [_parse_dt(v.get("as_of"))
                     for v in (cot.get("symbols") or {}).values()]
            cands = [c for c in cands if c]
            as_of = max(cands) if cands else None
        except (json.JSONDecodeError, OSError, AttributeError):
            as_of = None
    a = age_days(as_of, now)
    out.append(("health:cot_stale",
                f"🚨 **CoT が更新されていません**: 最新 as_of "
                f"{as_of.date().isoformat() if as_of else '不明'}"
                f"（{a:.0f} 日前）。cot-weekly.yml を確認してください。"
                if a is not None else
                "🚨 **cot_state.json を読めない/as_of がありません**。",
                a is None or a >= COT_STALE_DAYS))

    # term_raw.jsonl: 最終行の date
    last = read_jsonl_last("term_raw.jsonl")
    tdt = _parse_dt(last.get("date")) if last else None
    a = age_days(tdt, now)
    out.append(("health:term_stale",
                f"🚨 **term_raw が更新されていません**: 最終 "
                f"{tdt.date().isoformat() if tdt else '不明'}（{a:.0f} 日前）。"
                "term-archive.yml を確認してください。"
                if a is not None else
                "🚨 **term_raw.jsonl が空/読めません**。",
                a is None or a >= TERM_STALE_DAYS))

    # signal_pending_*.jsonl: 各ファイルの最終 timestamp
    for tf, path in (("4h", "signal_pending_4h.jsonl"), ("1d", "signal_pending_1d.jsonl")):
        last = read_jsonl_last(path)
        pdt = _parse_dt(last.get("timestamp")) if last else None
        a = age_days(pdt, now)
        out.append((f"health:pending_{tf}_stale",
                    f"🚨 **{path} が更新されていません**: 最終 "
                    f"{pdt.isoformat() if pdt else '不明'}（{a:.0f} 日前）。"
                    f"ta-{tf} ワークフローを確認してください。"
                    if a is not None else
                    f"🚨 **{path} が空/読めません**。",
                    a is None or a >= PENDING_STALE_DAYS))
    return out


# ------------------------------------------------------------------ notify

def post_discord(text, dry_run):
    url = os.environ.get("WEBHOOK_CRITICAL", "")
    if dry_run or not url:
        if not dry_run and not url:
            print("[WARN] WEBHOOK_CRITICAL 未設定のため投稿をスキップします")
        print("---- 通知内容 ----")
        print(text)
        print("------------------")
        return True
    try:
        import requests
        body = text if len(text) <= 1900 else text[:1900] + "..."
        resp = requests.post(url, json={"content": body}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[ERROR] Discord 送信失敗: {e}")
        return False


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="マイルストーン通知（平時は沈黙）")
    ap.add_argument("--dry-run", action="store_true", help="投稿せず標準出力のみ")
    ap.add_argument("--state", default=STATE_PATH_DEFAULT, help="状態ファイルのパス")
    ap.add_argument("--n-threshold", type=int, default=rv.N_FULL,
                    help=f"本判定の n 閾値（既定 {rv.N_FULL}・受け入れ確認用に下げられる）")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    state = load_state(args.state)
    notified = state["notified"]

    history = rv.load_history()

    pending = []          # (key, msg) 未通知なら送る
    pending += check_sample_size(history, args.n_threshold)
    pending += check_events_yaml(now)
    pending += check_term_reaudit(now)

    # 死活は解消時にフラグを落とす（再発時に再通知されるように）
    changed = False
    for key, msg, is_alert in check_health(now):
        if is_alert:
            pending.append((key, msg))
        elif key in notified:
            del notified[key]
            changed = True

    fresh = [(k, m) for k, m in pending if k not in notified]

    if not fresh:
        print(f"[{now.isoformat()}] 通知対象なし（平時）。history={len(history)}件")
        # 平時は state を書かない（updated_at だけ動いて毎日 commit が出るのを防ぐ）
        if changed:
            save_state(args.state, state)
        return 0

    body = "\n\n".join(m for _, m in fresh)
    text = f"⏰ **commodity-analyzer マイルストーン** ({now.date().isoformat()})\n\n{body}"
    if post_discord(text, args.dry_run):
        for k, _ in fresh:
            notified[k] = now.isoformat()
        save_state(args.state, state)
        print(f"通知 {len(fresh)} 件: {[k for k, _ in fresh]}")
        return 0

    print("送信失敗のため通知済みフラグは更新しません（次回再試行）")
    return 1


if __name__ == "__main__":
    sys.exit(main())

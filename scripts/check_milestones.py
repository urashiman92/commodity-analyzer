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
  C. 死活        … 各ワークフローの「最後に正常完走した時刻」（GitHub Actions API）
                    + cot_state のデータ鮮度（稼働とは別軸）
                    ※ API で取得できないものは判定をスキップ（監視失敗で偽アラートを出さない）

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

# --- C. 死活: データ鮮度（ワークフロー稼働とは別軸） ---
COT_STALE_DAYS = 14

# --- C. 死活: ワークフローの最終成功時刻 ---
# 「最後にシグナルが記録された時刻」ではなく「最後にジョブが正常完走した時刻」で判定する。
# 前者は "静かな相場（全銘柄 silent で pending に1行も追記されない）" と "WF停止" を
# 区別できず誤報を出した（2026-09-10 に health:pending_1d_stale が発火。実際は
# ta-1d が全平日 success で、9/3〜9/11 に条件を満たすシグナルが無かっただけ）。
#
# 閾値は cron 間隔 + 実測の起動遅延（GitHub のスケジュール遅延は実測 2〜5h）を踏まえた値:
#   ta-4h        1日6回(0,4,8,12,16,20時)・毎日 → 2日（連続12回の失敗で発火）
#   ta-1d        平日のみ 23:10（実測 01:00 前後に起動）。金→月が最長の空白で、
#                月曜朝のチェック時点の最大 age は約2.2日 → 3日（週末の空白では発火しない）
#   verify-signals 毎日 0:30 → 3日（2日連続失敗までは許容）
#   cot-weekly   毎週土曜 1:00 → 14日（2週連続失敗で発火。CoT は週次公表）
#   term-archive 平日 22:30 → 5日（ta-1d 同様に週末の空白を吸収した上で2営業日分の猶予）
WORKFLOW_HEALTH = [
    ("ta-4h.yml", "ta-4h（4時間足の分析・記録）", 2.0),
    ("ta-1d.yml", "ta-1d（日足の分析・記録）", 3.0),
    ("verify-signals.yml", "verify-signals（ホライズン照合）", 3.0),
    ("cot-weekly.yml", "cot-weekly（CoT取得）", 14.0),
    ("term-archive.yml", "term-archive（限月生値アーカイブ）", 5.0),
]
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


def _repo_slug():
    """owner/repo。Actions では GITHUB_REPOSITORY、ローカルでは origin の URL から。"""
    slug = os.environ.get("GITHUB_REPOSITORY")
    if slug:
        return slug
    try:
        import subprocess
        url = subprocess.run(["git", "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return None
    if not url:
        return None
    url = url.removesuffix(".git")
    if url.startswith("git@"):
        url = url.split(":", 1)[-1]
    parts = [p for p in url.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def latest_success_at(workflow_file, now):
    """ワークフローの最新 success の完了時刻。取得できなければ None（= 判定をスキップ）。

    監視自体の失敗で偽アラートを出さないため、例外・認証なし・0件はすべて None を返す。
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = _repo_slug()
    if not token or not repo:
        return None
    try:
        import requests
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs",
            params={"status": "success", "per_page": 1},
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=15,
        )
        resp.raise_for_status()
        runs = resp.json().get("workflow_runs") or []
    except Exception as e:
        print(f"[WARN] {workflow_file} の run 取得に失敗（判定スキップ）: {e}")
        return None
    if not runs:
        print(f"[WARN] {workflow_file} の success run が0件（判定スキップ）")
        return None
    return _parse_dt(runs[0].get("updated_at") or runs[0].get("created_at"))


def check_health(now, fetch=None):
    """C. 死活。(key, msg, is_alert) を返す。解消時はフラグを消せるよう is_alert=False も返す。

    ワークフローの最終成功時刻が取得できなかったものは**タプル自体を返さない**
    （アラートも解除もしない = 監視不能時に沈黙する）。
    """
    fetch = fetch or latest_success_at
    out = []

    # --- ワークフロー稼働（最後に正常完走した時刻） ---
    for wf_file, label, limit_days in WORKFLOW_HEALTH:
        last = fetch(wf_file, now)
        if last is None:
            continue  # 監視不能 → 沈黙（誤報を出さない）
        a = age_days(last, now)
        out.append((
            f"health:wf_{wf_file.removesuffix('.yml')}",
            f"🚨 **{label} が {a:.1f} 日間 成功していません**"
            f"（最終成功 {last.isoformat(timespec='minutes')} / 閾値 {limit_days:.0f}日）。\n"
            f"→ .github/workflows/{wf_file} の失敗を確認してください。",
            a >= limit_days,
        ))

    # --- データ鮮度（WF稼働とは別軸。ジョブが成功しても中身が古いことはある） ---
    as_of = None
    if os.path.exists("cot_state.json"):
        try:
            with open("cot_state.json", encoding="utf-8") as f:
                cot = json.load(f)
            cands = [c for c in (_parse_dt(v.get("as_of"))
                                 for v in (cot.get("symbols") or {}).values()) if c]
            as_of = max(cands) if cands else None
        except (json.JSONDecodeError, OSError, AttributeError):
            as_of = None
    a = age_days(as_of, now)
    out.append(("health:cot_stale",
                f"🚨 **CoT データが更新されていません**: 最新 as_of "
                f"{as_of.date().isoformat() if as_of else '不明'}"
                f"（{a:.0f} 日前）。cot-weekly.yml と CFTC 公表を確認してください。"
                if a is not None else
                "🚨 **cot_state.json を読めない/as_of がありません**。",
                a is None or a >= COT_STALE_DAYS))
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

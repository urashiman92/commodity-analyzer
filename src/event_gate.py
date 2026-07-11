"""
イベントゲート判定モジュール（Phase3 shadow・記録専用）

conviction / routing には一切適用しない（CLAUDE.md 原則③）。
config/events.yaml（人手編集のみ・本モジュールは読み取り専用）を読み、
実行時刻(UTC)が主要発表の前後ウィンドウ内かを銘柄別に判定して
シグナルの `event_gate` フィールドに shadow 記録する。

- ステートレス（新stateファイルなし・書き手マップ無変更）
- ET→UTC は zoneinfo（America/New_York）で DST 込み正確に変換
- 重複ウィンドウは全件を配列で保持
- events.yaml 欠損・破損時は None（呼び出し側は event_gate: null で記録継続）

返り値（常にこの形。ウィンドウ外は pre/post 空配列・時間 null）:
  {"pre": [names], "hours_to_event": float|None,
   "post": [names], "hours_since_event": float|None}
"""
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger(__name__)

EVENTS_PATH = "config/events.yaml"
ET = ZoneInfo("America/New_York")

# recurring 出現の生成範囲（ウィンドウ最大は before12h+after2h なので±14日で十分）
GEN_DAYS = 14

# プロセス内キャッシュ
_CACHE = {"loaded": False, "config": None}


def _load_config() -> dict | None:
    if _CACHE["loaded"]:
        return _CACHE["config"]
    cfg = None
    try:
        with open(EVENTS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            cfg = data
    except Exception:
        logger.warning("events.yaml の読み込みに失敗（event_gate: null で記録継続）",
                       exc_info=True)
        cfg = None
    _CACHE["loaded"] = True
    _CACHE["config"] = cfg
    return cfg


def _holidays(cfg) -> set:
    out = set()
    for h in cfg.get("us_federal_holidays") or []:
        if isinstance(h, date) and not isinstance(h, datetime):
            out.add(h)
        else:
            try:
                out.add(datetime.strptime(str(h), "%Y-%m-%d").date())
            except ValueError:
                continue
    return out


def _et_to_utc(d: date, hh: int, mm: int) -> datetime:
    """ET の日付+時刻を UTC の aware datetime に（DST は zoneinfo が解決）。"""
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).astimezone(timezone.utc)


def _gen_eia_weekly(monday: date, holidays: set):
    """EIA週次石油在庫: 水曜10:30 ET。週前半(月〜水)に連邦祝日 → 木曜11:00 ET。"""
    week_first_half = {monday + timedelta(days=i) for i in range(3)}  # 月,火,水
    if week_first_half & holidays:
        return _et_to_utc(monday + timedelta(days=3), 11, 0)   # 木曜11:00 ET
    return _et_to_utc(monday + timedelta(days=2), 10, 30)      # 水曜10:30 ET


def _gen_crop_progress(monday: date, holidays: set):
    """Crop Progress: 月曜16:00 ET・4〜11月のみ。月曜祝日 → 火曜16:00 ET。"""
    d = monday + timedelta(days=1) if monday in holidays else monday
    if not (4 <= d.month <= 11):
        return None
    return _et_to_utc(d, 16, 0)

_RULES = {
    "eia_weekly": _gen_eia_weekly,
    "crop_progress": _gen_crop_progress,
}


def _build_events(cfg, now: datetime) -> list:
    """now±GEN_DAYS の全イベント（手動+ルール生成）を
    [(name, dt_utc, symbols, before_h, after_h)] で返す。"""
    defaults = cfg.get("defaults") or {}
    def_before = float(defaults.get("window_before_h", 12))
    def_after = float(defaults.get("window_after_h", 2))
    holidays = _holidays(cfg)
    out = []

    # 手動登録
    for ev in cfg.get("events") or []:
        try:
            dt = datetime.fromisoformat(str(ev["datetime_utc"]))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError, TypeError):
            continue
        out.append((str(ev.get("name", "?")), dt.astimezone(timezone.utc),
                    list(ev.get("symbols") or []),
                    float(ev.get("window_before_h", def_before)),
                    float(ev.get("window_after_h", def_after))))

    # ルール自動生成（now を含む週の前後 GEN_DAYS 分の各週）
    start_monday = (now - timedelta(days=GEN_DAYS)).date()
    start_monday -= timedelta(days=start_monday.weekday())  # その週の月曜へ
    for rec in cfg.get("recurring") or []:
        gen = _RULES.get(str(rec.get("rule", "")))
        if gen is None:
            continue
        monday = start_monday
        while monday <= (now + timedelta(days=GEN_DAYS)).date():
            dt = gen(monday, holidays)
            if dt is not None:
                out.append((str(rec.get("name", "?")), dt,
                            list(rec.get("symbols") or []),
                            float(rec.get("window_before_h", def_before)),
                            float(rec.get("window_after_h", def_after))))
            monday += timedelta(days=7)
    return out


def get_event_gate(symbol: str, now: datetime = None) -> dict | None:
    """銘柄の発表前後ウィンドウ判定。yaml 不読は None（例外を漏らさない）。"""
    try:
        cfg = _load_config()
        if cfg is None:
            return None
        if now is None:
            now = datetime.now(timezone.utc)

        pre, pre_hours = [], []
        post, post_hours = [], []
        for name, dt, symbols, before_h, after_h in _build_events(cfg, now):
            if symbol not in symbols:
                continue
            if dt - timedelta(hours=before_h) <= now < dt:
                pre.append(name)
                pre_hours.append((dt - now).total_seconds() / 3600.0)
            elif dt <= now <= dt + timedelta(hours=after_h):
                post.append(name)
                post_hours.append((now - dt).total_seconds() / 3600.0)

        return {
            "pre": pre,
            "hours_to_event": round(min(pre_hours), 2) if pre_hours else None,
            "post": post,
            "hours_since_event": round(min(post_hours), 2) if post_hours else None,
        }
    except Exception:
        logger.warning("event_gate 判定に失敗（null で記録継続）", exc_info=True)
        return None

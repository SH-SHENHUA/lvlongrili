#!/usr/bin/env python3
"""生成 Team Spirit CS2 赛程的 iCalendar(.ics) 文件，用于导入 iPhone 日历。

数据源：bzzoiro Sports Data API 的 CS2 接口（免费，注册只要邮箱）。
输出的命名方式沿用曼联日历：未进行显示「主队 VS 客队」，已结束显示「主队 比分 客队」，
队名不翻译；备注里显示比赛名称，已结束的比赛备注里逐张列出地图比分。
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ==========配置区域==========
# bzzoiro 的 API key（必填）。到 https://sports.bzzoiro.com/register/ 免费注册后获取。
API_TOKEN = (os.environ.get("BZZOIRO_API_KEY") or "").strip()
API_BASE = (os.environ.get("BZZOIRO_API_BASE") or "https://sports.bzzoiro.com").rstrip("/")

TEAM = (os.environ.get("TEAM_NAME") or "Team Spirit").strip()
YEAR = int(os.environ.get("YEAR") or 2026)
PAGE_SIZE = 100
MAX_PAGES = 20
# 已结束的比赛要再查一次详情才能拿到逐地图比分；留个上限防止意外打爆额度。
DETAIL_FETCH_MAX = int(os.environ.get("DETAIL_FETCH_MAX") or 300)
# 请求之间的间隔秒数，礼貌一点。
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY") or 0.5)

OUTPUT_FILE = (os.environ.get("OUTPUT_FILE") or "matches.ics").strip()
CALENDAR_NAME = f"{TEAM} CS2 {YEAR}"
# ============================

CRLF = "\r\n"
MAX_LINE_OCTETS = 75

# 赛制 -> 日程时长。BO1 约 1 小时，BO3 约 2.5 小时，BO5 约 4 小时；未知按 2 小时。
BO_DURATION_HOURS = {1: 1.0, 2: 1.5, 3: 2.5, 4: 3.5, 5: 4.0}
DEFAULT_DURATION_HOURS = 2.0


# ---------- 工具 ----------

def _pick(mapping, *keys):
    """从 dict 里按候选键依次取值（不同接口的字段名偶有差异）。"""
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", []):
            return value
    return None


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _team_name(side) -> str:
    if isinstance(side, dict):
        return str(_pick(side, "name", "title", "team_name") or "").strip()
    return str(side or "").strip()


def _parse_dt(value):
    if not value:
        return None
    text = str(value).strip()
    if not text or text.startswith("0000-"):
        return None
    text = text.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                moment = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


# ---------- 数据源：bzzoiro CS2 ----------

def _api_get(path: str, params: dict | None = None):
    if not API_TOKEN:
        raise SystemExit(
            "错误：缺少 BZZOIRO_API_KEY。\n"
            "到 https://sports.bzzoiro.com/register/ 免费注册（只要邮箱）即可拿到 key，\n"
            "然后在本机设为环境变量，或在仓库 Settings → Secrets 里加同名 Secret。"
        )
    headers = {"Authorization": f"Token {API_TOKEN}", "Accept": "application/json"}
    url = f"{API_BASE}{path}"
    resp = requests.get(url, headers=headers, params=params or {}, timeout=40)
    if resp.status_code in (401, 403):
        raise SystemExit(
            f"错误：API 拒绝了请求（HTTP {resp.status_code}）。请检查 BZZOIRO_API_KEY 是否有效"
            f"（头部格式应为 Authorization: Token <key>）。"
        )
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    return resp.json()


def fetch_matches() -> list:
    """列出该年与本队相关的 CS2 比赛（含分页）。"""
    results, offset = [], 0
    for page in range(MAX_PAGES):
        payload = _api_get("/csgo/api/v2/matches/", {
            "team": TEAM,
            "date_from": f"{YEAR}-01-01",
            "date_to": f"{YEAR}-12-31",
            "limit": PAGE_SIZE,
            "offset": offset,
        })
        batch = payload.get("results") if isinstance(payload, dict) else payload
        batch = batch or []
        print(f"[bzzoiro] 第 {page + 1} 页取到 {len(batch)} 条（累计 {len(results) + len(batch)}）")
        results.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return results


def _matches_team(record: dict) -> bool:
    names = f"{_team_name(record.get('home_team'))} {_team_name(record.get('away_team'))}".lower()
    return TEAM.lower() in names


def _map_rows(detail: dict) -> list:
    """逐地图比分。详情接口的 maps 元素结构未在规范里写死，这里做容错。"""
    rows = []
    for item in detail.get("maps") or []:
        if isinstance(item, str):
            rows.append({"map": item, "scores": [None, None], "length": None})
            continue
        if not isinstance(item, dict):
            continue
        map_name = str(_pick(item, "map", "map_name", "mapname", "name") or "").strip()
        scores = _pick(item, "scores")
        if isinstance(scores, (list, tuple)) and len(scores) >= 2:
            left, right = _as_int(scores[0]), _as_int(scores[1])
        else:
            left = _as_int(_pick(item, "home_score", "score_home", "score1", "score_left"))
            right = _as_int(_pick(item, "away_score", "score_away", "score2", "score_right"))
        if not map_name and left is None and right is None:
            continue
        rows.append({"map": map_name or "?", "scores": [left, right],
                     "length": _pick(item, "duration", "length", "time")})
    return rows


def _tournament_title(record: dict, detail: dict | None = None) -> str:
    source = detail or record
    tournament = _pick(source, "tournament")
    name = ""
    if isinstance(tournament, dict):
        name = str(_pick(tournament, "name", "title") or "")
    elif tournament:
        name = str(tournament)
    if not name:
        name = str(_pick(source, "tournament_name") or "")
    stage = str(_pick(source, "stage") or "").strip()
    if stage and stage.lower() not in name.lower():
        return f"{name} - {stage}" if name else stage
    return name or "未知赛事"


def normalize(record: dict, detail: dict | None = None) -> dict | None:
    start = _parse_dt(_pick(record, "start_time", "date", "start_date") or
                      (detail or {}).get("start_time"))
    if start is None or start.year != YEAR:
        return None

    left = _team_name(record.get("home_team")) or _team_name((detail or {}).get("home_team"))
    right = _team_name(record.get("away_team")) or _team_name((detail or {}).get("away_team"))
    if not left or not right:
        return None

    status = str(_pick(record, "status") or (detail or {}).get("status") or "").lower()
    score_left = _as_int(_pick(record, "home_score") if record.get("home_score") is not None
                         else (detail or {}).get("home_score"))
    score_right = _as_int(_pick(record, "away_score") if record.get("away_score") is not None
                          else (detail or {}).get("away_score"))
    finished = status == "finished" and score_left is not None and score_right is not None

    bestof = _as_int(_pick(record, "best_of") or (detail or {}).get("best_of"))
    duration = BO_DURATION_HOURS.get(bestof, DEFAULT_DURATION_HOURS)
    maps = _map_rows(detail) if (detail and finished) else []

    key = _pick(record, "id", "api_id") or (detail or {}).get("id")
    return {
        "key": str(key or "").strip(),
        "start": start,
        "duration": timedelta(hours=duration),
        "left": left,
        "right": right,
        "score": (score_left, score_right) if finished else None,
        "finished": finished,
        "bestof": bestof,
        "tournament": _tournament_title(record, detail),
        "maps": maps,
        "status": status,
    }


def collect_events() -> tuple[list, str]:
    records = fetch_matches()
    print(f"[bzzoiro] 列表共取到 {len(records)} 条")

    events, skipped_team, skipped_date, duplicates = [], 0, 0, 0
    seen = set()
    for record in records:
        if not _matches_team(record):
            skipped_team += 1
            continue
        event = normalize(record)
        if event is None:
            skipped_date += 1
            continue
        if not event["key"] or event["key"] in seen:
            duplicates += 1
            continue
        seen.add(event["key"])
        events.append(event)

    # 已结束的比赛再查详情，取逐地图比分
    finished = [e for e in events if e["finished"]]
    if finished:
        print(f"[bzzoiro] 需要补充逐地图比分的已结束比赛 {len(finished)} 场"
              f"（上限 {DETAIL_FETCH_MAX}）")
        for event in finished[:DETAIL_FETCH_MAX]:
            try:
                detail = _api_get(f"/csgo/api/v2/matches/{event['key']}/")
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                print(f"[bzzoiro] 详情接口失败（HTTP {status}），该场地图比分将缺失：{event['key']}")
                continue
            event["maps"] = _map_rows(detail)
            if not event["maps"]:
                print(f"[bzzoiro] 该场没有返回地图数据：{event['key']}")

    if not events:
        raise SystemExit(
            f"错误：没有取到 {TEAM} 在 {YEAR} 年的任何比赛，拒绝写出空日历。\n"
            f"原始记录 {len(records)} 条（非本队 {skipped_team}、日期不符或缺失 {skipped_date}、"
            f"重复 {duplicates}）。请检查 key / 年份 / 队名配置。"
        )

    events.sort(key=lambda e: e["start"])
    finished_count = sum(1 for e in events if e["finished"])
    print(f"[bzzoiro] 有效比赛 {len(events)} 场（已结束 {finished_count} 场，"
          f"未进行 {len(events) - finished_count} 场；"
          f"过滤掉 非本队 {skipped_team} / 日期不符 {skipped_date} / 重复 {duplicates}）")

    note = f"数据来源：bzzoiro CS2 API（sports.bzzoiro.com）"
    print(f"[覆盖] {note}")
    return events, note


# ---------- 写 iCalendar ----------

def escape_text(value: str) -> str:
    """转义 RFC 5545 TEXT 值里的特殊字符，并把真实换行变成 \\n。"""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold_line(line: str) -> str:
    """按 RFC 5545 折行：每行不超过 75 字节，续行以一个空格开头。"""
    data = line.encode("utf-8")
    if len(data) <= MAX_LINE_OCTETS:
        return line
    chunks, start, limit = [], 0, MAX_LINE_OCTETS
    while start < len(data):
        end = min(start + limit, len(data))
        while end > start and end < len(data) and (data[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(data[start:end].decode("utf-8"))
        start = end
        limit = MAX_LINE_OCTETS - 1
    return (CRLF + " ").join(chunks)


def utc_stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def build_description(event: dict) -> str:
    """备注：比赛名称（必显）；已结束的比赛逐张列出地图比分。"""
    lines = [f"比赛：{event['tournament'] or '未知赛事'}"]
    if event["bestof"]:
        lines.append(f"赛制：BO{event['bestof']}")

    if event["finished"]:
        if event["maps"]:
            lines.append("地图比分：")
            for index, row in enumerate(event["maps"], 1):
                scores = row["scores"]
                if len(scores) >= 2 and scores[0] is not None and scores[1] is not None:
                    detail = f"{scores[0]}-{scores[1]}"
                else:
                    detail = "比分未知"
                extra = f"（{row['length']}）" if row.get("length") else ""
                lines.append(f"  {index}. {row['map']} {detail}{extra}")
        else:
            lines.append("地图比分：数据缺失")

    lines.append(f"比赛ID：{event['key']}")
    return "\n".join(lines)


def build_event(event: dict, stamp: str) -> list:
    start = event["start"].astimezone(timezone.utc)
    end = start + event["duration"]

    if event["finished"] and event["score"]:
        summary = f"{event['left']} {event['score'][0]}-{event['score'][1]} {event['right']}"
    else:
        summary = f"{event['left']} VS {event['right']}"
    summary = re.sub(r"\s+", " ", summary).strip()

    return [
        "BEGIN:VEVENT",
        f"UID:{event['key']}@lvlongrili",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{utc_stamp(start)}",
        f"DTEND:{utc_stamp(end)}",
        f"SUMMARY:{escape_text(summary)}",
        f"DESCRIPTION:{escape_text(build_description(event))}",
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
        "END:VEVENT",
    ]


def build_calendar(events: list, stamp: str, calendar_note: str = "") -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//lvlongrili//Team Spirit CS2 fixtures//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_text(CALENDAR_NAME)}",
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    if calendar_note:
        lines.append(f"X-WR-CALDESC:{escape_text(calendar_note)}")
    for event in events:
        lines.extend(build_event(event, stamp))
    lines.append("END:VCALENDAR")
    return CRLF.join(fold_line(line) for line in lines) + CRLF


def _same_ignoring_dtstamp(existing: str, fresh: str) -> bool:
    """内容一致就不改写（DTSTAMP 每次都变，否则会每天产生一次无意义提交）。"""
    def strip(text: str) -> str:
        return CRLF.join(l for l in text.split(CRLF) if not l.startswith("DTSTAMP:"))

    return strip(existing) == strip(fresh)


def main():
    events, calendar_note = collect_events()

    stamp = utc_stamp(_now_utc())
    ics = build_calendar(events, stamp, calendar_note)

    if ics.count("BEGIN:VEVENT") == 0:
        raise SystemExit("错误：生成的日历里没有任何事件，拒绝写出空日历")

    unchanged = False
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, encoding="utf-8", newline="") as fp:
            existing = fp.read()
        unchanged = _same_ignoring_dtstamp(existing, ics)

    if unchanged:
        print(f"{OUTPUT_FILE} 的比赛内容与上次一致（仅 DTSTAMP 不同），跳过写入")
    else:
        with open(OUTPUT_FILE, "w", encoding="utf-8", newline="") as fp:
            fp.write(ics)

    upcoming = sum(1 for e in events if not e["finished"])
    print(f"共 {len(events)} 场比赛（其中未进行 {upcoming} 场）-> {OUTPUT_FILE}"
          + ("（内容未变化，未改写）" if unchanged else "（已更新）"))


if __name__ == "__main__":
    main()

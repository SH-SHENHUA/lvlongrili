#!/usr/bin/env python3
"""生成 Team Spirit CS2 赛程的 iCalendar(.ics) 文件，用于导入 iPhone 日历。

数据源：Liquipedia LPDB v3（需要免费 API key，申请方式见 README）。
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
# Liquipedia 免费 API key（必填）。到 https://discord.gg/liquipedia 申请，见 README。
LIQUIPEDIA_API_KEY = (os.environ.get("LIQUIPEDIA_API_KEY") or "").strip()
# 他们条款要求 User-Agent 写明用途与联系方式。
USER_AGENT = (os.environ.get("LIQUIPEDIA_USER_AGENT")
              or "lvlongrili-calendar/1.0 (https://github.com/SH-SHENHUA/lvlongrili; contact: sfc100520@163.com)")

API_BASE = "https://api.liquipedia.net/api/v3"
WIKI = (os.environ.get("LIQUIPEDIA_WIKI") or "counterstrike").strip()
TEAM = (os.environ.get("TEAM_NAME") or "Team Spirit").strip()
YEAR = int(os.environ.get("YEAR") or 2026)
PAGE_SIZE = 200
MAX_PAGES = 10
# 免费档限 60 次/小时，请求之间留间隔（他们条款也建议节流）。
REQUEST_DELAY_SECONDS = float(os.environ.get("LIQUIPEDIA_DELAY") or 2)

OUTPUT_FILE = (os.environ.get("OUTPUT_FILE") or "matches.ics").strip()
CALENDAR_NAME = f"{TEAM} CS2 {YEAR}"
# ============================

CRLF = "\r\n"
MAX_LINE_OCTETS = 75

# 赛制 -> 日程时长。BO1 约 1 小时，BO3 约 2.5 小时，BO5 约 4 小时；未知按 2 小时。
BO_DURATION_HOURS = {1: 1.0, 2: 1.5, 3: 2.5, 4: 3.5, 5: 4.0}
DEFAULT_DURATION_HOURS = 2.0


# ---------- 工具 ----------

def _pick(mapping: dict, *keys):
    """从 dict 里按候选键依次取值（LPDB 字段名偶有差异）。"""
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


# ---------- 数据源：Liquipedia LPDB v3 ----------

def _api_get(endpoint: str, params: dict):
    if not LIQUIPEDIA_API_KEY:
        raise SystemExit(
            "错误：缺少 LIQUIPEDIA_API_KEY。\n"
            "Liquipedia 的 API key 免费，申请方式见 README（需要加入他们的 Discord）。\n"
            "拿到后在本机设为环境变量，或在仓库 Settings → Secrets 里加同名 Secret。"
        )
    headers = {"Authorization": f"Apikey {LIQUIPEDIA_API_KEY}", "User-Agent": USER_AGENT,
               "Accept": "application/json"}
    url = f"{API_BASE}/{endpoint}"
    resp = requests.get(url, headers=headers, params=params, timeout=40)
    if resp.status_code == 403:
        raise SystemExit(
            "错误：Liquipedia 拒绝了请求（403）。常见原因：key 无效/未生效，"
            "或 User-Agent 未按要求写明用途与联系方式。"
        )
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    return resp.json()


def _match_conditions() -> str:
    """比赛筛选条件：2026 年 + 参与方是 Team Spirit。"""
    return (f"[[date::>{YEAR - 1}-12-31]] AND [[date::<{YEAR + 1}-01-01]] "
            f'AND [[match2opponents::"{TEAM}"]]')


def fetch_matches() -> list:
    """拉取 LPDB 的比赛记录。先按队伍条件查；该条件不被支持时退回按时间窗查再本地过滤。"""
    records = []
    offset = 0
    conditions = _match_conditions()
    for page in range(MAX_PAGES):
        params = {
            "wiki": WIKI,
            "conditions": conditions,
            "limit": PAGE_SIZE,
            "offset": offset,
            "order": "date ASC",
        }
        try:
            payload = _api_get("match", params)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            if page == 0 and status in (400, 422):
                print(f"[liquipedia] 队伍条件不被接受（HTTP {status}），"
                      f"退回按时间窗查询并在本地过滤。")
                conditions = f"[[date::>{YEAR - 1}-12-31]] AND [[date::<{YEAR + 1}-01-01]]"
                offset = 0
                continue
            raise SystemExit(f"错误：请求 Liquipedia 失败（HTTP {status}）：{exc}")
        batch = payload.get("result") or []
        print(f"[liquipedia] 第 {page + 1} 页取到 {len(batch)} 条"
              f"（累计 {len(records) + len(batch)}）")
        records.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return records


def _opponent_name(opponent: dict) -> str:
    return (_pick(opponent, "name", "template", "displayname") or "").strip()


def _participants(record: dict) -> list:
    """返回 [{"name":..., "score":...}, ...]，取不到就返回空列表。"""
    raw = record.get("match2opponents") or []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append({
            "name": _opponent_name(item),
            "score": _as_int(_pick(item, "score", "gamescore")),
        })
    return out


def _is_target_team(record: dict) -> bool:
    return any(TEAM.lower() in (p["name"] or "").lower() for p in _participants(record))


def _map_rows(record: dict) -> list:
    """逐地图数据：返回 [{"map": "Dust2", "scores": [13, 9]}, ...]。

    LPDB 的 match2games 结构在不同赛事模板下字段名略有差异，这里做容错。
    """
    rows = []
    for game in record.get("match2games") or []:
        if not isinstance(game, dict):
            continue
        map_name = (_pick(game, "map", "mapname", "map_name", "displayname") or "").strip()
        scores = []
        for side in game.get("opponents") or []:
            if isinstance(side, dict):
                scores.append(_as_int(_pick(side, "score", "gamescore")))
            else:
                scores.append(_as_int(side))
        if not map_name and not any(s is not None for s in scores):
            continue
        rows.append({"map": map_name or "?", "scores": scores,
                     "winner": _as_int(game.get("winner")), "length": game.get("length")})
    return rows


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text or text.startswith("0000-"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def normalize(record: dict) -> dict | None:
    start = _parse_dt(record.get("date"))
    if start is None or start.year != YEAR:
        return None
    participants = _participants(record)
    if len(participants) < 2:
        return None
    left, right = participants[0], participants[1]

    finished = bool(_as_int(record.get("finished"))) or str(record.get("status", "")).lower() in (
        "finished", "completed")
    score_left, score_right = left["score"], right["score"]
    if finished and (score_left is None or score_right is None):
        # 没有比分就不按「已结束」渲染，避免出现「A - B」这种空比分
        finished = False

    bestof = _as_int(record.get("bestof"))
    duration = BO_DURATION_HOURS.get(bestof, DEFAULT_DURATION_HOURS)

    tournament = (_pick(record, "tournament", "shortname", "tickername") or "").strip()
    section = (_pick(record, "section", "series", "type") or "")
    if isinstance(section, str):
        section = section.strip()

    return {
        "key": (_pick(record, "match2id", "objectname", "pageid") or "").strip(),
        "start": start,
        "duration": timedelta(hours=duration),
        "left": left["name"],
        "right": right["name"],
        "score": (score_left, score_right) if finished else None,
        "finished": finished,
        "bestof": bestof,
        "tournament": tournament,
        "section": section,
        "maps": _map_rows(record),
        "dateexact": _as_int(record.get("dateexact")) != 0,
    }


def collect_events() -> tuple[list, str]:
    records = fetch_matches()
    print(f"[liquipedia] 共取到 {len(records)} 条记录")

    events, skipped_team, skipped_date, duplicates = [], 0, 0, 0
    seen = set()
    for record in records:
        if not _is_target_team(record):
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

    if not events:
        raise SystemExit(
            f"错误：没有取到 {TEAM} 在 {YEAR} 年的任何比赛，拒绝写出空日历。\n"
            f"原始记录 {len(records)} 条（非本队 {skipped_team}、日期不符或缺失 {skipped_date}、"
            f"重复 {duplicates}）。请检查 key / 年份 / 队名配置。"
        )

    events.sort(key=lambda e: e["start"])
    finished_count = sum(1 for e in events if e["finished"])
    print(f"[liquipedia] 有效比赛 {len(events)} 场（已结束 {finished_count} 场，"
          f"未进行 {len(events) - finished_count} 场；过滤掉 非本队 {skipped_team} / 日期不符 {skipped_date} / 重复 {duplicates}）")

    note = f"数据来源：Liquipedia LPDB v3（{WIKI}）"
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
    lines = []
    title = event["tournament"] or "未知赛事"
    if event["section"]:
        title = f"{title} - {event['section']}"
    lines.append(f"比赛：{title}")
    if event["bestof"]:
        lines.append(f"赛制：BO{event['bestof']}")
    if not event["dateexact"]:
        lines.append("时间：待定（该场开赛时间尚未确定）")

    if event["finished"] and event["maps"]:
        lines.append("地图比分：")
        for index, row in enumerate(event["maps"], 1):
            scores = row["scores"]
            if len(scores) >= 2 and scores[0] is not None and scores[1] is not None:
                detail = f"{scores[0]}-{scores[1]}"
            else:
                detail = "比分未知"
            extra = f"（{row['length']}）" if row.get("length") else ""
            lines.append(f"  {index}. {row['map']} {detail}{extra}")
    elif event["finished"]:
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

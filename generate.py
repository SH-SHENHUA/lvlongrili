"""生成 Team Spirit CS2 赛程的 iCalendar(.ics) 文件，用于导入 iPhone 日历。

数据源：Cito API 的 CS2 接口（免费档 500 次/月，注册无需信用卡）。
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
# Cito 的 API key（必填）。到 https://citoapi.com/signup?game=cs2 免费注册（500 次/月）。
API_KEY = (os.environ.get("CITO_API_KEY") or "").strip()
API_BASE = (os.environ.get("CITO_API_BASE") or "https://api.citoapi.com/api/v1").rstrip("/")

# Team Spirit 在 Cito 里的 slug（接口也接受 id，如 cs2-team-7020）。
TEAM_SLUG = (os.environ.get("TEAM_SLUG") or "spirit").strip()
TEAM_NAME = (os.environ.get("TEAM_NAME") or "Team Spirit").strip()
YEAR = int(os.environ.get("YEAR") or 2026)
PAGE_SIZE = 250
MAX_PAGES = 4
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY") or 0.5)

OUTPUT_FILE = (os.environ.get("OUTPUT_FILE") or "matches.ics").strip()
CALENDAR_NAME = f"{TEAM_NAME} CS2 {YEAR}"
# ============================

CRLF = "\r\n"
MAX_LINE_OCTETS = 75

# 赛制 -> 日程时长。BO1 约 1 小时，BO3 约 2.5 小时，BO5 约 4 小时；未知按 2 小时。
BO_DURATION_HOURS = {1: 1.0, 2: 1.5, 3: 2.5, 4: 3.5, 5: 4.0}
DEFAULT_DURATION_HOURS = 2.0


# ---------- 工具 ----------

def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _sum_rounds(halves) -> int | None:
    """把上下半场回合数相加得到该地图总分（示例 [10,3] -> 13）。"""
    if not isinstance(halves, (list, tuple)) or not halves:
        return None
    total = 0
    found = False
    for value in halves:
        number = _as_int(value)
        if number is not None:
            total += number
            found = True
    return total if found else None


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
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


# ---------- 数据源：Cito CS2 ----------

def _api_get(path: str, params: dict | None = None):
    if not API_KEY:
        raise SystemExit(
            "错误：缺少 CITO_API_KEY。\n"
            "到 https://citoapi.com/signup?game=cs2 免费注册（500 次/月，无需信用卡）即可拿到 key，\n"
            "然后在本机设为环境变量，或在仓库 Settings → Secrets 里加同名 Secret。"
        )
    headers = {"x-api-key": API_KEY, "Accept": "application/json"}
    resp = requests.get(f"{API_BASE}{path}", headers=headers, params=params or {}, timeout=40)
    if resp.status_code in (401, 403):
        raise SystemExit(
            f"错误：Cito 拒绝了请求（HTTP {resp.status_code}）。请检查 CITO_API_KEY 是否有效"
            f"（头部应为 x-api-key）。"
        )
    if resp.status_code == 429:
        raise SystemExit("错误：Cito 返回 429（超出免费额度 500 次/月 或 10 次/分钟），请稍后再试。")
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    payload = resp.json()
    if isinstance(payload, dict) and payload.get("success") is False:
        raise SystemExit(f"错误：Cito 返回失败：{payload.get('error') or payload}")
    return payload


def fetch_matches() -> list:
    """取该队的全部比赛（含进行中与已结束，maps 内嵌在列表响应里）。"""
    results, page = [], 1
    for _ in range(MAX_PAGES):
        payload = _api_get(f"/cs2/teams/{TEAM_SLUG}/matches",
                           {"limit": PAGE_SIZE, "page": page})
        batch = payload.get("data") if isinstance(payload, dict) else payload
        batch = batch or []
        print(f"[cito] 第 {page} 页取到 {len(batch)} 条（累计 {len(results) + len(batch)}）")
        results.extend(batch)
        meta = payload.get("meta") or {}
        if len(batch) < PAGE_SIZE or not meta.get("hasNext"):
            break
        page += 1
    return results


def _side_names(record: dict) -> tuple[str, str]:
    def name(side_key, name_key):
        side = record.get(side_key)
        if isinstance(side, dict) and side.get("name"):
            return str(side["name"]).strip()
        return str(record.get(name_key) or "").strip()

    return name("team1", "team1Name"), name("team2", "team2Name")


def _maps_team(record: dict) -> bool:
    left, right = _side_names(record)
    names = f"{left} {right}".lower()
    if TEAM_NAME.lower() in names:
        return True
    # 兜底：slug 形如 team-spirit / spirit
    return TEAM_SLUG.replace("-", " ").lower() in names


def _map_rows(record: dict) -> list:
    """逐地图比分。

    优先用 map.team1Score/team2Score；它们为空时用上下半场回合数相加
    （官方示例：team1Halves [10,3] vs team2Halves [2,0] -> 13-2，且 durationRounds=15=13+2）。
    """
    rows = []
    for item in record.get("maps") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("mapName") or item.get("map") or "").strip()
        left = _as_int(item.get("team1Score"))
        right = _as_int(item.get("team2Score"))
        if left is None or right is None:
            h_left = _sum_rounds(item.get("team1Halves"))
            h_right = _sum_rounds(item.get("team2Halves"))
            if h_left is not None and h_right is not None:
                left, right = h_left, h_right
        note = ""
        if item.get("isForfeit"):
            note = "（弃权）"
        elif item.get("resultType") not in (None, "played"):
            note = f"（{item.get('resultType')}）"
        if not name and left is None and right is None:
            continue
        rows.append({"map": name or "?", "scores": [left, right],
                     "number": _as_int(item.get("mapNumber")) or 0, "note": note})
    rows.sort(key=lambda row: row["number"])
    return rows


def normalize(record: dict) -> dict | None:
    start = _parse_dt(record.get("startsAt") or record.get("startTime"))
    if start is None or start.year != YEAR:
        return None

    left, right = _side_names(record)
    if not left or not right:
        return None

    status = str(record.get("status") or "").lower()
    score_left = _as_int(record.get("team1Score"))
    score_right = _as_int(record.get("team2Score"))
    finished = status == "completed" and score_left is not None and score_right is not None

    bestof = _as_int(record.get("bestOf"))
    duration = BO_DURATION_HOURS.get(bestof, DEFAULT_DURATION_HOURS)

    event_name = str(record.get("eventName") or "").strip()
    stage = str(record.get("stageName") or "").strip()
    title = event_name or "未知赛事"
    if stage and stage.lower() not in title.lower():
        title = f"{title} - {stage}" if event_name else stage

    return {
        "key": str(record.get("id") or "").strip(),
        "start": start,
        "duration": timedelta(hours=duration),
        "left": left,
        "right": right,
        "score": (score_left, score_right) if finished else None,
        "finished": finished,
        "bestof": bestof,
        "tournament": title,
        "maps": _map_rows(record) if finished else [],
        "status": status,
    }


def collect_events() -> tuple[list, str]:
    records = fetch_matches()
    print(f"[cito] 列表共取到 {len(records)} 条")

    events, skipped_team, skipped_year, skipped_time, duplicates = [], 0, 0, 0, 0
    missing_time = []
    seen = set()
    for record in records:
        if not _maps_team(record):
            skipped_team += 1
            continue
        start = _parse_dt(record.get("startsAt") or record.get("startTime"))
        if start is None:
            # 没有开赛时间就无法生成日程，必须明确报出来
            skipped_time += 1
            if len(missing_time) < 5:
                left, right = _side_names(record)
                missing_time.append(f"{left} vs {right}（id={record.get('id')}）")
            continue
        if start.year != YEAR:
            skipped_year += 1
            continue
        event = normalize(record)
        if event is None:
            skipped_year += 1
            continue
        if not event["key"] or event["key"] in seen:
            duplicates += 1
            continue
        seen.add(event["key"])
        events.append(event)

    if not events:
        raise SystemExit(
            f"错误：没有取到 {TEAM_NAME} 在 {YEAR} 年的任何比赛，拒绝写出空日历。\n"
            f"原始记录 {len(records)} 条（非本队 {skipped_team}、年份不符 {skipped_year}、"
            f"缺少开赛时间 {skipped_time}、重复 {duplicates}）。"
            f"请检查 key / 队名 slug / 年份配置。"
        )

    events.sort(key=lambda e: e["start"])
    finished_count = sum(1 for e in events if e["finished"])
    with_maps = sum(1 for e in events if e["finished"] and e["maps"])
    print(f"[cito] 有效比赛 {len(events)} 场（已结束 {finished_count} 场，其中带逐地图比分 "
          f"{with_maps} 场；未进行 {len(events) - finished_count} 场；"
          f"过滤掉 非本队 {skipped_team} / 年份不符 {skipped_year} / "
          f"缺少开赛时间 {skipped_time} / 重复 {duplicates}）")
    if finished_count and with_maps < finished_count:
        print(f"[cito] 注意：有 {finished_count - with_maps} 场已结束比赛没有逐地图数据")
    if skipped_time:
        print(f"[cito] 警告：有 {skipped_time} 场比赛因为接口没有给开赛时间而被跳过，"
              f"这些场次不会出现在日历里：")
        for item in missing_time:
            print(f"         {item}")

    note = "数据来源：Cito API（cs2-api.org）"
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
                lines.append(f"  {index}. {row['map']} {detail}{row.get('note', '')}")
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

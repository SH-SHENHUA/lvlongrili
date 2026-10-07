"""生成 Team Spirit CS2 赛程的 iCalendar(.ics) 文件，用于导入 iPhone 日历。

数据源：Cito API 的 CS2 接口（免费档 500 次/月，注册无需信用卡）。
输出的命名方式沿用曼联日历：未进行显示「主队 VS 客队」，已结束显示「主队 比分 客队」，
队名不翻译；备注里显示比赛名称，已结束的比赛备注里逐张列出地图比分。
"""

import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

# ==========配置区域==========
# 数据源：cito（默认，免费档 500 次/月但只有最近 30 天历史）或 liquipedia（全量历史，需 key）
SOURCE = (os.environ.get("SOURCE") or "cito").strip().lower()

# --- Cito（默认源）---
# Cito 的 API key（必填）。到 https://citoapi.com/signup?game=cs2 免费注册（500 次/月）。
API_KEY = (os.environ.get("CITO_API_KEY") or "").strip()
API_BASE = (os.environ.get("CITO_API_BASE") or "https://api.citoapi.com/api/v1").rstrip("/")

# --- Liquipedia（备选源）---
LIQUIPEDIA_API_KEY = (os.environ.get("LIQUIPEDIA_API_KEY") or "").strip()
LIQUIPEDIA_BASE = (os.environ.get("LIQUIPEDIA_BASE")
                   or "https://api.liquipedia.net/api/v3").rstrip("/")
LIQUIPEDIA_WIKI = (os.environ.get("LIQUIPEDIA_WIKI") or "counterstrike").strip()
# 他们的条款要求 User-Agent 写明用途与联系方式
LIQUIPEDIA_USER_AGENT = (os.environ.get("LIQUIPEDIA_USER_AGENT")
                         or "lvlongrili-calendar/1.0 "
                            "(https://github.com/SH-SHENHUA/lvlongrili; contact: sfc100520@163.com)")

# Team Spirit 在 Cito 里的 slug（接口也接受 id，如 cs2-team-7020）。
TEAM_SLUG = (os.environ.get("TEAM_SLUG") or "spirit").strip()
TEAM_NAME = (os.environ.get("TEAM_NAME") or "Team Spirit").strip()
# 可选：额外的队名写法（逗号分隔），用于 Liquipedia 条件与过滤兜底
TEAM_ALIASES = [item.strip() for item in (os.environ.get("TEAM_ALIASES") or "").split(",")
                if item.strip()]
YEAR = int(os.environ.get("YEAR") or 2026)
PAGE_SIZE = 250
MAX_PAGES = 4
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY") or 0.5)

OUTPUT_FILE = (os.environ.get("OUTPUT_FILE") or "matches.ics").strip()
# 累积缓存：把见过的比赛存下来。数据源的历史窗口（例如免费档只有 30 天）会让旧比赛
# 从接口里消失，有了缓存，日历只会越来越完整，不会把已经收录的比赛丢掉。
CACHE_FILE = (os.environ.get("CACHE_FILE") or "matches_cache.json").strip()
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


def resolve_team() -> dict | None:
    """校验 TEAM_SLUG 并取回队伍资料（尽力而为，失败不影响主流程）。

    slug 跟着队名走（官方文档：Team IDs are stable; slugs follow the current name），
    所以先确认一次，既能把接口用的队名/ID 回显到日志里，也能在 slug 写错时给出可读的提示。
    """
    try:
        payload = _api_get(f"/cs2/teams/{TEAM_SLUG}")
    except SystemExit:
        raise
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        if status == 404:
            raise SystemExit(
                f"错误：Cito 里找不到队伍 slug={TEAM_SLUG!r}（HTTP 404）。\n"
                f"请确认 TEAM_SLUG：例如 Team Spirit 通常为 spirit（也可直接用队伍 id，"
                f"形如 cs2-team-7020）。\n"
                f"可用 GET {API_BASE}/cs2/teams/{{slug}} 逐个试，或参考官方文档的 Teams 部分。"
            )
        print(f"[cito] 队伍资料查询失败（HTTP {status}），继续按 slug 取比赛列表")
        return None
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict) and data.get("name"):
        print(f"[cito] 队伍确认：{data.get('name')}"
              f"（id={data.get('id')}，slug={data.get('slug')}，"
              f"世界排名={data.get('worldRanking')}）")
    return data if isinstance(data, dict) else None


def fetch_matches() -> list:
    """取该队的全部比赛（含进行中与已结束，maps 内嵌在列表响应里）。"""
    results, page = [], 1
    for _ in range(MAX_PAGES):
        try:
            payload = _api_get(f"/cs2/teams/{TEAM_SLUG}/matches",
                               {"limit": PAGE_SIZE, "page": page})
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            if status == 404:
                raise SystemExit(
                    f"错误：Cito 的 /cs2/teams/{TEAM_SLUG}/matches 返回 404。\n"
                    f"通常是 TEAM_SLUG 不对（Team Spirit 一般用 spirit，或直接用队伍 id）。"
                )
            raise SystemExit(f"错误：请求 Cito 失败（HTTP {status}）：{exc}")
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


def _detect_team_names(records: list) -> set:
    """从返回数据里自识别本队名称。

    接口是按队伍（/cs2/teams/{slug}/matches）查的，所以出现次数最多的队名就是本队。
    这样就不必依赖配置里的名字和接口的显示名完全一致 —— 例如接口把 Team Spirit
    叫 "Spirit"，若按配置的 "Team Spirit" 去匹配会把所有比赛都当成非本队丢光。
    """
    counts = Counter()
    for record in records:
        left, right = _side_names(record)
        for name in (left, right):
            if name:
                counts[name] += 1
    if not counts:
        return set()
    top = counts.most_common(1)[0][1]
    if top < 2:
        return set()
    # 该端点是按队伍查的，本队会出现在**每一场**里，所以取出现次数最多的那个名字
    # （并列时都取，避免同分歧义）。
    return {name for name, count in counts.items() if count == top}


def _maps_team(record: dict, accepted_names: set | None = None) -> bool:
    left, right = _side_names(record)
    names = f"{left} {right}".lower()
    accepted = {TEAM_NAME.lower()} | {n.lower() for n in (accepted_names or set())}
    if any(name and name in names for name in accepted):
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


def _collect_cito() -> tuple[list, str]:
    resolve_team()
    records = fetch_matches()
    print(f"[cito] 列表共取到 {len(records)} 条")

    accepted_names = _detect_team_names(records)
    if accepted_names:
        print(f"[cito] 自识别本队名称：{'、'.join(sorted(accepted_names))}"
              f"（配置里的 TEAM_NAME={TEAM_NAME}）")
        if TEAM_NAME.lower() not in {n.lower() for n in accepted_names}:
            print(f"[cito] 提示：接口用的队名与 TEAM_NAME 不一致，已按接口的队名过滤；"
                  f"这只影响日历显示名（当前为 {CALENDAR_NAME}）。")
    else:
        print("[cito] 未能自识别队名，将按配置的 TEAM_NAME / slug 过滤")

    events, skipped_team, skipped_year, skipped_time, duplicates = [], 0, 0, 0, 0
    missing_time = []
    seen = set()
    for record in records:
        if not _maps_team(record, accepted_names):
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
        # 不在这里直接失败：可能是休赛期，或者数据源只给最近 30 天而这段时间没有比赛。
        # 交给 main() 与缓存合并后再判断，避免把已有的日历弄丢。
        print(f"[cito] 警告：本次没有取到 {TEAM_NAME} 在 {YEAR} 年的比赛"
              f"（原始记录 {len(records)} 条：非本队 {skipped_team}、年份不符 {skipped_year}、"
              f"缺少开赛时间 {skipped_time}、重复 {duplicates}）。将尝试使用缓存。")

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


# ---------- 数据源：Liquipedia LPDB v3（备选）----------
# 用途：Cito 免费档只有最近 30 天历史，无法回填 2026 年较早的比赛；
# Liquipedia 的 LPDB 有全量历史与逐地图数据（match2games），可以补上这一段。
# 代价：key 需要通过加入他们的 Discord 申请，且条款要求 User-Agent 写明用途与联系方式。

def _lp_api_get(params: dict):
    if not LIQUIPEDIA_API_KEY:
        raise SystemExit(
            "错误：缺少 LIQUIPEDIA_API_KEY（当前 SOURCE=liquipedia）。\n"
            "Liquipedia 的 API key 免费，但需要加入他们的 Discord 申请（见 README），\n"
            "拿到后在仓库 Settings → Secrets 里加同名 Secret。"
        )
    headers = {"Authorization": f"Apikey {LIQUIPEDIA_API_KEY}",
               "User-Agent": LIQUIPEDIA_USER_AGENT,
               "Accept": "application/json"}
    resp = requests.get(f"{LIQUIPEDIA_BASE}/match", headers=headers, params=params, timeout=40)
    if resp.status_code in (401, 403):
        raise SystemExit(
            f"错误：Liquipedia 拒绝了请求（HTTP {resp.status_code}）。\n"
            f"常见原因：key 无效/未生效，或 User-Agent 未按要求写明用途与联系方式。"
        )
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    payload = resp.json()
    return payload.get("result") or payload.get("data") or []


def _lp_candidate_conditions() -> list:
    """按队伍筛选的条件写法有多种，逐个试，取第一个有结果的。"""
    window = f"[[date::>{YEAR - 1}-12-31]] AND [[date::<{YEAR + 1}-01-01]]"
    names = [TEAM_NAME] + list(TEAM_ALIASES)
    candidates = []
    for name in names:
        candidates.append(f'{window} AND [[match2opponents::"{name}"]]')
        candidates.append(f'{window} AND [[opponent::"{name}"]]')
    candidates.append(window)  # 兜底：只按时间窗查，再在本地按队名过滤
    return candidates


def _lp_participants(record: dict) -> list:
    out = []
    for item in record.get("match2opponents") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("template") or "").strip()
        out.append({"name": name, "score": _as_int(item.get("score"))})
    return out


def _lp_games(record: dict) -> list:
    rows = []
    for game in record.get("match2games") or []:
        if not isinstance(game, dict):
            continue
        name = str(game.get("map") or game.get("mapname") or game.get("map_name") or "").strip()
        scores = []
        for side in game.get("opponents") or []:
            scores.append(_as_int(side.get("score")) if isinstance(side, dict) else _as_int(side))
        if len(scores) < 2:
            raw = game.get("scores")
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                scores = [_as_int(raw[0]), _as_int(raw[1])]
        if not name and not any(s is not None for s in scores):
            continue
        rows.append({
            "map": name or "?",
            "scores": [scores[0] if scores else None, scores[1] if len(scores) > 1 else None],
            "number": _as_int(game.get("order")) or len(rows) + 1,
            "note": "（弃权）" if str(game.get("resulttype") or "").lower() == "forfeit" else "",
            "length": game.get("length"),
        })
    rows.sort(key=lambda row: row["number"])
    return rows


def _lp_normalize(record: dict) -> dict | None:
    start = _parse_dt(record.get("date"))
    if start is None or start.year != YEAR:
        return None
    participants = _lp_participants(record)
    if len(participants) < 2:
        return None
    left, right = participants[0], participants[1]

    finished = bool(_as_int(record.get("finished"))) or \
        str(record.get("status") or "").lower() in ("finished", "completed")
    score_left, score_right = left["score"], right["score"]
    if finished and (score_left is None or score_right is None):
        finished = False  # 没有比分就不按「已结束」渲染

    bestof = _as_int(record.get("bestof"))
    duration = BO_DURATION_HOURS.get(bestof, DEFAULT_DURATION_HOURS)

    title = str(record.get("tournament") or "").strip() or "未知赛事"
    section = str(record.get("section") or record.get("series") or "").strip()
    if section and section.lower() not in title.lower():
        title = f"{title} - {section}"

    return {
        "key": str(record.get("match2id") or record.get("objectname") or "").strip(),
        "start": start,
        "duration": timedelta(hours=duration),
        "left": left["name"],
        "right": right["name"],
        "score": (score_left, score_right) if finished else None,
        "finished": finished,
        "bestof": bestof,
        "tournament": title,
        "maps": _lp_games(record) if finished else [],
        "status": str(record.get("status") or ("finished" if finished else "upcoming")),
    }


def _collect_liquipedia() -> tuple[list, str]:
    records = []
    used_conditions = ""
    for conditions in _lp_candidate_conditions():
        collected, offset = [], 0
        for page in range(MAX_PAGES):
            batch = _lp_api_get({"wiki": LIQUIPEDIA_WIKI, "conditions": conditions,
                                 "limit": PAGE_SIZE, "offset": offset, "order": "date ASC"})
            print(f"[liquipedia] 条件={conditions} 第 {page + 1} 页取到 {len(batch)} 条")
            collected.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        if any(_lp_team_in(r) for r in collected):
            records, used_conditions = collected, conditions
            break
        if collected:
            print(f"[liquipedia] 该条件取到 {len(collected)} 条但没有本队，换下一种写法")
    if not records:
        print("[liquipedia] 警告：所有查询条件都没有取到本队比赛")
    else:
        print(f"[liquipedia] 采用条件：{used_conditions}")

    events, skipped_team, skipped_year, skipped_time, duplicates = [], 0, 0, 0, 0
    missing_time = []
    seen = set()
    for record in records:
        if not _lp_team_in(record):
            skipped_team += 1
            continue
        start = _parse_dt(record.get("date"))
        if start is None:
            skipped_time += 1
            if len(missing_time) < 5:
                names = [p["name"] for p in _lp_participants(record)]
                missing_time.append(f"{' vs '.join(names)}（id={record.get('match2id')}）")
            continue
        if start.year != YEAR:
            skipped_year += 1
            continue
        event = _lp_normalize(record)
        if event is None:
            skipped_year += 1
            continue
        if not event["key"] or event["key"] in seen:
            duplicates += 1
            continue
        seen.add(event["key"])
        events.append(event)

    events.sort(key=lambda e: e["start"])
    finished_count = sum(1 for e in events if e["finished"])
    with_maps = sum(1 for e in events if e["finished"] and e["maps"])
    print(f"[liquipedia] 有效比赛 {len(events)} 场（已结束 {finished_count} 场，其中带逐地图比分 "
          f"{with_maps} 场；未进行 {len(events) - finished_count} 场；"
          f"过滤掉 非本队 {skipped_team} / 年份不符 {skipped_year} / "
          f"缺少开赛时间 {skipped_time} / 重复 {duplicates}）")
    if skipped_time:
        print(f"[liquipedia] 警告：有 {skipped_time} 场比赛因为接口没有给开赛时间而被跳过：")
        for item in missing_time:
            print(f"         {item}")

    note = f"数据来源：Liquipedia LPDB v3（{LIQUIPEDIA_WIKI}）"
    print(f"[覆盖] {note}")
    return events, note


def _lp_team_in(record: dict) -> bool:
    accepted = {TEAM_NAME.lower()} | {n.lower() for n in TEAM_ALIASES}
    for participant in _lp_participants(record):
        name = participant["name"].lower()
        if any(token and token in name for token in accepted | {TEAM_SLUG.replace("-", " ")}):
            return True
    return False


# ---------- 数据源调度 ----------

SOURCES = {"cito": _collect_cito, "liquipedia": _collect_liquipedia}


def collect_events() -> tuple[list, str]:
    collector = SOURCES.get(SOURCE.lower())
    if collector is None:
        raise SystemExit(
            f"错误：未知的数据源 SOURCE={SOURCE!r}，可选：{'、'.join(sorted(SOURCES))}"
        )
    print(f"[源] 使用数据源：{SOURCE}")
    return collector()


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


# ---------- 累积缓存 ----------

def _event_to_json(event: dict) -> dict:
    return {
        "key": event["key"],
        "start": event["start"].astimezone(timezone.utc).isoformat(),
        "duration_hours": event["duration"].total_seconds() / 3600.0,
        "left": event["left"],
        "right": event["right"],
        "score": list(event["score"]) if event["score"] else None,
        "finished": event["finished"],
        "bestof": event["bestof"],
        "tournament": event["tournament"],
        "maps": event["maps"],
        "status": event["status"],
    }


def _event_from_json(data: dict) -> dict | None:
    start = _parse_dt(data.get("start"))
    if start is None or not data.get("key") or not data.get("left") or not data.get("right"):
        return None
    hours = data.get("duration_hours")
    try:
        duration = timedelta(hours=float(hours)) if hours else timedelta(hours=DEFAULT_DURATION_HOURS)
    except (TypeError, ValueError):
        duration = timedelta(hours=DEFAULT_DURATION_HOURS)
    score = data.get("score")
    return {
        "key": str(data["key"]),
        "start": start,
        "duration": duration,
        "left": str(data["left"]),
        "right": str(data["right"]),
        "score": tuple(score) if isinstance(score, (list, tuple)) and len(score) == 2 else None,
        "finished": bool(data.get("finished")),
        "bestof": _as_int(data.get("bestof")),
        "tournament": str(data.get("tournament") or ""),
        "maps": data.get("maps") or [],
        "status": str(data.get("status") or ""),
    }


def load_cache() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"[cache] 读取 {CACHE_FILE} 失败，将忽略：{exc}")
        return {}
    raw = payload.get("matches") if isinstance(payload, dict) else payload
    cache = {}
    for item in raw or []:
        event = _event_from_json(item)
        if event:
            cache[event["key"]] = event
    print(f"[cache] 已有 {len(cache)} 场历史记录")
    return cache


def save_cache(cache: dict) -> None:
    items = [_event_to_json(event) for event in sorted(cache.values(), key=lambda e: e["start"])]
    payload = {"team": TEAM_NAME, "year": YEAR, "count": len(items), "matches": items}
    with open(CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")


def merge_with_cache(events: list, cache: dict) -> tuple[list, int]:
    """接口的数据优先；接口这次没返回、但缓存里有且属于本年的比赛保留下来。"""
    merged = dict(cache)
    for event in events:
        merged[event["key"]] = event
    source_keys = {event["key"] for event in events}
    kept_from_cache = sum(
        1 for key, event in merged.items()
        if key not in source_keys and event["start"].year == YEAR
    )
    result = [event for event in merged.values() if event["start"].year == YEAR]
    result.sort(key=lambda event: event["start"])
    return result, kept_from_cache


def main():
    events, calendar_note = collect_events()

    cache = load_cache()
    events, kept_from_cache = merge_with_cache(events, cache)
    if kept_from_cache:
        print(f"[cache] 本次接口未返回、但缓存保留的比赛：{kept_from_cache} 场")
    print(f"[cache] 合并后共 {len(events)} 场（已结束 {sum(1 for e in events if e['finished'])} 场）")

    if not events:
        raise SystemExit(
            f"错误：接口与缓存都没有 {TEAM_NAME} 在 {YEAR} 年的比赛，拒绝写出空日历。\n"
            f"请检查 CITO_API_KEY / TEAM_SLUG / YEAR 配置是否正确。"
        )

    save_cache({event["key"]: event for event in events})

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

"""生成 Team Spirit CS2 赛程的 iCalendar(.ics) 文件，用于导入 iPhone 日历。

数据源：Cito API 的 CS2 接口（免费档 500 次/月，注册无需信用卡）。
输出的命名方式沿用曼联日历：未进行显示「主队 VS 客队」，已结束显示「主队 比分 客队」，
队名不翻译；备注里显示比赛名称，已结束的比赛备注里逐张列出地图比分。
"""

import hashlib
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
TEAM_ID = ""  # 由 resolve_team() 填入，用于在赛事对阵结构里认本队的比赛
TEAM_NAME = (os.environ.get("TEAM_NAME") or "Team Spirit").strip()
# 可选：额外的队名写法（逗号分隔），用于 Liquipedia 条件与过滤兜底
TEAM_ALIASES = [item.strip() for item in (os.environ.get("TEAM_ALIASES") or "").split(",")
                if item.strip()]
YEAR = int(os.environ.get("YEAR") or 2026)
PAGE_SIZE = 250
MAX_PAGES = 4
# Cito 免费档限流为 10 次/分钟，因此默认每次请求间隔 6.5 秒（每日任务总共十几次请求，
# 完全可以接受）；可用 REQUEST_DELAY 覆盖。
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY") or 6.5)
# 被限流（429）时默认等待的秒数（若响应带 Retry-After 则以其为准）
RATE_LIMIT_WAIT = float(os.environ.get("RATE_LIMIT_WAIT") or 20)

OUTPUT_FILE = (os.environ.get("OUTPUT_FILE") or "matches.ics").strip()
# 累积缓存：把见过的比赛存下来。数据源的历史窗口（例如免费档只有 30 天）会让旧比赛
# 从接口里消失，有了缓存，日历只会越来越完整，不会把已经收录的比赛丢掉。
CACHE_FILE = (os.environ.get("CACHE_FILE") or "matches_cache.json").strip()
# 人工补录：数据源缺失、且比赛详情接口取不到（实测 404）的已完赛场次
MANUAL_FILE = (os.environ.get("MANUAL_FILE") or "manual_matches.json").strip()
CALENDAR_NAME = f"{TEAM_NAME} CS2 {YEAR}"
# ============================

CRLF = "\r\n"
MAX_LINE_OCTETS = 75

# 赛制 -> 日程时长。BO1 约 1 小时，BO3 约 2.5 小时，BO5 约 4 小时；未知按 2 小时。
BO_DURATION_HOURS = {1: 1.0, 2: 1.5, 3: 2.5, 4: 3.5, 5: 4.0}
DEFAULT_DURATION_HOURS = 2.0

# 已过开赛时间这么久仍未结束的比赛，标注「结果未更新（可能延期或取消）」
STALE_HOURS = 12
# 赛事详情最多多少天重新拉一次（赛事可能仍在进行、还会新增比赛）
EVENT_REFRESH_DAYS = int(os.environ.get("EVENT_REFRESH_DAYS") or 7)


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


def canonical_key(start: datetime, left: str, right: str) -> str:
    """跨数据源稳定的比赛键：日期（UTC）+ 双方队名。

    UID 必须与数据源无关：否则一旦更换数据源，iPhone 里同一场比赛会出现两条
    （数据源各自的 id 不同）。用这个规范键，换源是原地更新而不是新增。
    """
    stamp = start.astimezone(timezone.utc).strftime("%Y-%m-%d")
    raw = f"{stamp}|{left.strip().lower()}|{right.strip().lower()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# 合法的大比分终局（BO1/BO3/BO5/BO7 的正常结果），用于识别数据源给出的异常比分
def score_is_sane(left, right) -> bool:
    """判断系列赛比分是否可能是真实的终局。

    用于挡掉数据源里自相矛盾的结果（实测遇到 BO1 却给出 1-1）。
    规则：双方不等、胜方达到过半、总局数不超过 9。
    """
    if left is None or right is None:
        return False
    total = left + right
    if total < 1 or total > 9:
        return False
    high, low = max(left, right), min(left, right)
    if low >= high or high > 5:
        return False
    return high >= total // 2 + 1


# ---------- 数据源：Cito CS2 ----------

def _api_get(path: str, params: dict | None = None, retries: int = 3):
    if not API_KEY:
        raise SystemExit(
            "错误：缺少 CITO_API_KEY。\n"
            "到 https://citoapi.com/signup?game=cs2 免费注册（500 次/月，无需信用卡）即可拿到 key，\n"
            "然后在本机设为环境变量，或在仓库 Settings → Secrets 里加同名 Secret。"
        )
    headers = {"x-api-key": API_KEY, "Accept": "application/json"}
    url = f"{API_BASE}{path}"
    resp = None
    for attempt in range(1, retries + 1):
        resp = requests.get(url, headers=headers, params=params or {}, timeout=40)
        if resp.status_code == 429:
            wait = _as_int(resp.headers.get("Retry-After")) or RATE_LIMIT_WAIT
            print(f"[cito] 触到限流（免费档 10 次/分钟），等待 {wait:.0f} 秒后重试"
                  f"（第 {attempt}/{retries} 次）")
            time.sleep(wait)
            continue
        break
    if resp is None:
        raise SystemExit("错误：请求 Cito 失败（未收到响应）")
    if resp.status_code in (401, 403):
        raise SystemExit(
            f"错误：Cito 拒绝了请求（HTTP {resp.status_code}）。请检查 CITO_API_KEY 是否有效"
            f"（头部应为 x-api-key）。"
        )
    if resp.status_code == 429:
        raise SystemExit(
            "错误：Cito 持续返回 429（免费档 10 次/分钟、500 次/月）。"
            "请稍后重试，或调大 REQUEST_DELAY。"
        )
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
        global TEAM_ID
        TEAM_ID = str(data.get("id") or "")
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
        # 系列赛提前结束（例如 2-0）时，接口仍会带上未打的那些地图：
        # mapName 为 TBA、resultType 为 not_played，这些不能列进比分。
        result_type = str(item.get("resultType") or "").lower()
        if result_type in ("not_played", "unplayed", "notplayed"):
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
        if not name and left is None and right is None:
            continue
        if left is None and right is None and not note:
            # 没有任何比分也没标注，视为无效条目
            continue
        rows.append({"map": name or "?", "scores": [left, right],
                     "number": _as_int(item.get("mapNumber")) or 0, "note": note})
    rows.sort(key=lambda row: row["number"])
    return rows


def _event_id_of(record: dict) -> str:
    event = record.get("event")
    if isinstance(event, dict) and event.get("id"):
        return str(event["id"])
    return str(record.get("eventId") or "")


def _structure_matches(structure, team_ids: set) -> list:
    """从赛事详情的 eventStructure 里抽出「涉及本队且有比分」的比赛。

    实测意义：Cito 的队伍接口会漏掉某些比赛。例如 Esports World Cup 2026，
    Spirit 实际打完整届并夺冠，但 /cs2/teams/spirit/matches 只收录 2 场；
    而这些场次在赛事详情的对阵结构里是全的（含系列赛比分）。
    """
    found = {}

    def walk(node):
        if isinstance(node, dict):
            teams = node.get("teams")
            if (isinstance(teams, list) and len(teams) >= 2
                    and all(isinstance(t, dict) and "score" in t and "winner" in t for t in teams)):
                match_id = str(node.get("id") or "")
                ids = {str(t.get("id") or "") for t in teams}
                scores = [_as_int(t.get("score")) for t in teams]
                if match_id and (ids & team_ids) and all(s is not None for s in scores[:2]):
                    found[match_id] = {
                        "id": match_id,
                        "teams": [{"id": str(t.get("id") or ""), "name": str(t.get("name") or ""),
                                   "score": _as_int(t.get("score"))} for t in teams],
                    }
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(structure)
    return list(found.values())


def _event_needs_refresh(entry: dict | None) -> bool:
    """赛事详情是否需要（重新）拉取：没缓存过、赛事可能仍在进行、或缓存太旧。"""
    if not entry:
        return True
    ends = _parse_dt(entry.get("endsAt"))
    if ends is not None and ends > _now_utc() - timedelta(days=1):
        return True
    checked = _parse_dt(entry.get("checked_at"))
    if checked is None:
        return True
    return (_now_utc() - checked).days >= EVENT_REFRESH_DAYS


def event_locations(records: list, cached: dict | None = None) -> dict:
    """按赛事取「举办城市／地点」，并把赛事详情里的对阵结构一并缓存。

    列表接口不返回位置，需要 GET /cs2/events/{id}；同一份响应里还带着该赛事的
    全部对阵（eventStructure），可用来补齐队伍接口漏掉的比赛。
    位置与对阵几乎不变，缓存后之后每天通常无需重复查询（免费档限 10 次/分钟）。
    """
    locations = cached if cached is not None else {}
    event_ids = []
    for record in records:
        # 只查「会进入日历的那些比赛」所属的赛事：接口返回的是队伍全量历史（含往年），
        # 若不过滤年份，会为多年前的赛事也去查询，白白浪费免费额度与时间。
        start = _parse_dt(record.get("startsAt") or record.get("startTime"))
        if start is None or start.year != YEAR:
            continue
        event_id = _event_id_of(record)
        if event_id and event_id not in event_ids:
            event_ids.append(event_id)

    todo = [event_id for event_id in event_ids if _event_needs_refresh(locations.get(event_id))]
    print(f"[cito] 赛事详情：共 {len(event_ids)} 个，本次需查询 {len(todo)} 个"
          f"（已缓存且未过期 {len(event_ids) - len(todo)} 个）")
    for event_id in todo:
        try:
            payload = _api_get(f"/cs2/events/{event_id}")
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            print(f"[cito] 赛事 {event_id} 详情查询失败（HTTP {status}），沿用缓存")
            continue
        data = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(data, dict):
            continue
        previous = locations.get(event_id) or {}
        entry = {
            "name": str(data.get("name") or ""),
            "location": str(data.get("location") or "").strip(),
            "isLan": data.get("isLan"),
            "status": data.get("status"),
            "endsAt": data.get("endsAt"),
            "checked_at": _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
            # 已处理过的补充比赛 id 要保留，避免每天重复查详情
            "fetched_ids": list(previous.get("fetched_ids") or []),
            "matches": _structure_matches(data.get("eventStructure"), _team_ids()),
        }
        locations[event_id] = entry
    return locations


def _team_ids() -> set:
    """本队在 Cito 里的 id 集合（用于在赛事对阵结构里认出本队的比赛）。"""
    ids = {TEAM_SLUG} if TEAM_SLUG.startswith("cs2-team-") else set()
    if TEAM_ID:
        ids.add(TEAM_ID)
    return ids


def _location_for(record: dict, locations: dict) -> str:
    info = locations.get(_event_id_of(record)) or {}
    return str(info.get("location") or "").strip()


def normalize(record: dict, location: str = "") -> dict | None:
    start = _parse_dt(record.get("startsAt") or record.get("startTime"))
    if start is None or start.year != YEAR:
        return None

    left, right = _side_names(record)
    if not left or not right:
        return None

    status = str(record.get("status") or "").lower()
    score_left = _as_int(record.get("team1Score"))
    score_right = _as_int(record.get("team2Score"))
    finished = (status == "completed" and score_is_sane(score_left, score_right))

    bestof = _as_int(record.get("bestOf"))
    duration = BO_DURATION_HOURS.get(bestof, DEFAULT_DURATION_HOURS)

    event_name = str(record.get("eventName") or "").strip()
    if not event_name:
        # 少数记录只有嵌套的 event 对象
        event_obj = record.get("event")
        if isinstance(event_obj, dict):
            event_name = str(event_obj.get("name") or "").strip()
    stage = str(record.get("stageName") or "").strip()
    title = event_name or "未知赛事"
    if stage and stage.lower() not in title.lower():
        title = f"{title} - {stage}" if event_name else stage

    notes = []
    if status == "completed" and not finished and (score_left is not None or score_right is not None):
        # 数据源声称已结束，但比分不可能是终局（例如 BO1 给出 1-1）：不显示该比分
        notes.append(f"比分待核实（数据源给出 {score_left}-{score_right}）")
    if not finished and start + timedelta(hours=STALE_HOURS) < _now_utc():
        # 已远超开赛时间却仍显示未结束：多为延期或取消，明确标注，避免看着像漏了结果
        notes.append(f"结果未更新（已过开赛时间 {STALE_HOURS} 小时以上，可能延期或取消）")

    return {
        "key": canonical_key(start, left, right),
        "source_id": str(record.get("id") or "").strip(),
        "start": start,
        "duration": timedelta(hours=duration),
        "left": left,
        "right": right,
        "score": (score_left, score_right) if finished else None,
        "finished": finished,
        "bestof": bestof,
        "tournament": title,
        "location": location,
        "maps": _map_rows(record) if finished else [],
        "status": status,
        "data_note": "；".join(notes),
    }


def _structure_index(locations: dict) -> dict:
    """{比赛id: [(队名, 比分), ...]}，来自各赛事对阵结构（用于回填缺失的结果）。"""
    index = {}
    for entry in locations.values():
        if not isinstance(entry, dict):
            continue
        for item in entry.get("matches") or []:
            teams = (item or {}).get("teams") or []
            if len(teams) >= 2:
                index[str(item.get("id") or "")] = [
                    (str(team.get("name") or ""), _as_int(team.get("score"))) for team in teams
                ]
    return index


def _structure_scores(index: dict, match_id: str, left: str, right: str):
    """按队名把结构里的比分对应到本场（接口两边顺序可能不同），拿不到就返回 None。"""
    entry = index.get(match_id)
    if not entry:
        return None
    by_name = {name.lower(): score for name, score in entry if name}
    left_score, right_score = by_name.get(left.lower()), by_name.get(right.lower())
    if left_score is None or right_score is None or not score_is_sane(left_score, right_score):
        return None
    return (left_score, right_score)


def _supplement_from_structures(records: list, locations: dict, events: list) -> list:
    """用赛事对阵结构里「队伍接口漏掉或没给结果」的比赛补齐日历。

    只对满足以下条件的场次查一次详情（并记进 fetched_ids，避免每天重复查）：
      - 出现在赛事对阵结构里且涉及本队、有系列赛比分；
      - 队伍接口里没有这场，或者接口给的还不是最终结果（例如停在 unresolved）。
    """
    completed_ids = set()
    for record in records:
        if str(record.get("status") or "").lower() != "completed":
            continue
        if score_is_sane(_as_int(record.get("team1Score")), _as_int(record.get("team2Score"))):
            completed_ids.add(str(record.get("id") or ""))
    known_keys = {event["key"] for event in events}
    extras = []
    for event_id, entry in locations.items():
        if not isinstance(entry, dict):
            continue
        for item in entry.get("matches") or []:
            match_id = str((item or {}).get("id") or "")
            if not match_id or match_id in completed_ids:
                continue
            if match_id in (entry.get("fetched_ids") or []):
                continue
            try:
                payload = _api_get(f"/cs2/matches/{match_id}")
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                # 记下来，避免每天都重试同一场：结构里有、但比赛详情取不到（实测 404）
                entry.setdefault("fetched_ids", []).append(match_id)
                if status == 404:
                    print(f"[cito] 赛事对阵结构里的 {match_id} 在比赛接口取不到（404），"
                          f"无法确定开赛时间，暂不加入日历")
                else:
                    print(f"[cito] 补齐 {match_id} 失败（HTTP {status}）")
                continue
            detail = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(detail, dict):
                continue
            entry.setdefault("fetched_ids", []).append(match_id)
            event = normalize(detail, location=str(entry.get("location") or ""))
            if event is None:
                continue
            if event["key"] in known_keys:
                # 同一场（日期+队名一致）已在列表里，用更完整的结果覆盖它
                for index, existing in enumerate(extras):
                    if existing["key"] == event["key"]:
                        extras[index] = event
                        break
                continue
            known_keys.add(event["key"])
            extras.append(event)
    return extras


def _collect_cito(cached_locations: dict | None = None) -> tuple[list, str]:
    resolve_team()
    records = fetch_matches()
    print(f"[cito] 列表共取到 {len(records)} 条")
    locations = event_locations(records, cached_locations)
    structure_index = _structure_index(locations)

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
    patched = 0
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
        event = normalize(record, location=_location_for(record, locations))
        if event is not None and not event["finished"]:
            # 某些比赛在队伍接口里停在 unresolved，但赛事对阵结构里已有比分：
            # 用结构里的比分回填（按队名对应，接口两边顺序可能不同）
            patch = _structure_scores(structure_index, str(record.get("id") or ""),
                                      event["left"], event["right"])
            if patch:
                event["finished"] = True
                event["score"] = patch
                event["status"] = "completed"
                event["data_note"] = ""
                patched += 1
        if event is None:
            skipped_year += 1
            continue
        if not event["key"] or event["key"] in seen:
            duplicates += 1
            continue
        seen.add(event["key"])
        events.append(event)

    # 用赛事详情里的对阵结构补齐队伍接口漏掉／没给出结果的比赛
    supplemented = _supplement_from_structures(records, locations, events)
    if supplemented:
        events.extend(supplemented)
        print(f"[cito] 从赛事对阵结构补齐 {len(supplemented)} 场"
              f"（队伍接口未收录或未给结果）：")
        for item in supplemented:
            print(f"         {item['start'].strftime('%Y-%m-%d')} "
                  f"{item['left']} {item['score'][0]}-{item['score'][1]} {item['right']}"
                  f"（{item['tournament']}）")
    if patched:
        print(f"[cito] 另有 {patched} 场用赛事对阵结构里的比分回填了结果"
              f"（队伍接口里仍是未结束）")

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
    if finished and not score_is_sane(score_left, score_right):
        finished = False  # 没有比分、或比分不可能是终局：不按「已结束」渲染

    bestof = _as_int(record.get("bestof"))
    duration = BO_DURATION_HOURS.get(bestof, DEFAULT_DURATION_HOURS)

    title = str(record.get("tournament") or "").strip() or "未知赛事"
    section = str(record.get("section") or record.get("series") or "").strip()
    if section and section.lower() not in title.lower():
        title = f"{title} - {section}"

    data_note = ""
    if not finished and (score_left is not None or score_right is not None):
        data_note = f"比分待核实（数据源给出 {score_left}-{score_right}）"
    if not finished and start + timedelta(hours=STALE_HOURS) < _now_utc():
        stale = f"结果未更新（已过开赛时间 {STALE_HOURS} 小时以上，可能延期或取消）"
        data_note = f"{data_note}；{stale}" if data_note else stale

    return {
        "key": canonical_key(start, left["name"], right["name"]),
        "source_id": str(record.get("match2id") or record.get("objectname") or "").strip(),
        "start": start,
        "duration": timedelta(hours=duration),
        "left": left["name"],
        "right": right["name"],
        "score": (score_left, score_right) if finished else None,
        "finished": finished,
        "bestof": bestof,
        "tournament": title,
        "location": str(record.get("location") or record.get("venue") or "").strip(),
        "maps": _lp_games(record) if finished else [],
        "status": str(record.get("status") or ("finished" if finished else "upcoming")),
        "data_note": data_note,
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

def load_manual_events() -> list:
    """读取人工补录的比赛（仅已完赛；见 manual_matches.json 里的说明）。"""
    if not os.path.exists(MANUAL_FILE):
        return []
    try:
        with open(MANUAL_FILE, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"[manual] 读取 {MANUAL_FILE} 失败，将忽略：{exc}")
        return []
    events = []
    for item in (payload.get("matches") if isinstance(payload, dict) else payload) or []:
        if not isinstance(item, dict):
            continue
        start = _parse_dt(item.get("date"))
        left = str(item.get("left") or "").strip()
        right = str(item.get("right") or "").strip()
        score = item.get("score")
        if start is None or not left or not right:
            print(f"[manual] 跳过格式不完整的条目：{item}")
            continue
        if not (isinstance(score, (list, tuple)) and len(score) == 2
                and score_is_sane(_as_int(score[0]), _as_int(score[1]))):
            print(f"[manual] 跳过没有有效终局比分的条目（人工补录只放已完赛）："
                  f"{left} vs {right}")
            continue
        if start.year != YEAR:
            continue
        events.append({
            "key": canonical_key(start, left, right),
            "source_id": str(item.get("source_id") or f"manual:{start.date()}:{left}-{right}"),
            "start": start,
            "duration": timedelta(hours=BO_DURATION_HOURS.get(_as_int(item.get("bestof")),
                                                             DEFAULT_DURATION_HOURS)),
            "left": left,
            "right": right,
            "score": (_as_int(score[0]), _as_int(score[1])),
            "finished": True,
            "bestof": _as_int(item.get("bestof")),
            "tournament": str(item.get("tournament") or "").strip() or "未知赛事",
            "location": str(item.get("location") or "").strip(),
            "maps": item.get("maps") or [],
            "status": "completed",
            # note 只作来源记录（写在 manual_matches.json 里便于日后核对），不进日历备注
            "data_note": str(item.get("calendar_note") or "").strip(),
        })
    if events:
        print(f"[manual] 人工补录 {len(events)} 场已完赛比赛：")
        for event in events:
            print(f"         {event['start'].strftime('%Y-%m-%d')} {event['left']} "
                  f"{event['score'][0]}-{event['score'][1]} {event['right']}"
                  f"（{event['tournament']}）")
    return events


SOURCES = {"cito": _collect_cito, "liquipedia": _collect_liquipedia}


def _source_names() -> list:
    names = [item.strip().lower() for item in SOURCE.split(",") if item.strip()]
    return names or ["cito"]


def collect_events(cached_locations: dict | None = None) -> tuple[list, str]:
    """按 SOURCE 指定的数据源取比赛；支持逗号分隔多个源并按顺序合并。

    多源合并的意义：单个源可能漏比赛。实测 Cito 对 Esports World Cup 2026 的覆盖不完整
    （Spirit 实际打完整届并夺冠，Cito 只收录 2 场），而 bzzoiro 的对手名单也对不上。
    合并时按「UTC 日期 + 双方队名」的规范键去重，**写在后面的源优先**：
    例如 SOURCE=cito,liquipedia 表示以 Liquipedia 补齐并覆盖冲突项。
    """
    names = _source_names()
    unknown = [name for name in names if name not in SOURCES]
    if unknown:
        raise SystemExit(
            f"错误：未知的数据源 {('、'.join(unknown))}，可选：{'、'.join(sorted(SOURCES))}"
            f"（可用逗号分隔多个源合并，如 cito,liquipedia）"
        )

    merged, notes = {}, []
    for name in names:
        collector = SOURCES[name]
        print(f"[源] 使用数据源：{name}")
        events, note = (collector(cached_locations) if name == "cito" else collector())
        print(f"[源] {name} 提供 {len(events)} 场")
        for event in events:
            merged[event["key"]] = event
        notes.append(note)

    # 人工补录只用来补数据源没有的场次；若数据源已给出更好的记录（有结果），以数据源为准
    manual = load_manual_events()
    filled = 0
    for event in manual:
        current = merged.get(event["key"])
        if current is None:
            merged[event["key"]] = event
            filled += 1
        elif not current["finished"] and event["finished"]:
            merged[event["key"]] = event
            filled += 1
    if filled:
        notes.append(f"人工补录 {filled} 场")

    return list(merged.values()), " + ".join(notes)


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
    """备注：第一行是赛事名称，紧跟系列赛比分，然后赛制与逐张地图比分。"""
    lines = [event["tournament"] or "未知赛事"]
    if event["finished"] and event["score"]:
        lines.append(f"比分：{event['score'][0]}-{event['score'][1]}")
    if event["bestof"]:
        lines.append(f"赛制：BO{event['bestof']}")
    if event.get("data_note"):
        lines.append(event["data_note"])

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

    lines.append(f"比赛ID：{event.get('source_id') or event['key']}")
    return "\n".join(lines)


def build_event(event: dict, stamp: str) -> list:
    start = event["start"].astimezone(timezone.utc)
    end = start + event["duration"]

    if event["finished"] and event["score"]:
        summary = f"{event['left']} {event['score'][0]}-{event['score'][1]} {event['right']}"
    else:
        summary = f"{event['left']} VS {event['right']}"
    summary = re.sub(r"\s+", " ", summary).strip()

    lines = [
        "BEGIN:VEVENT",
        f"UID:{event['key']}@lvlongrili",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{utc_stamp(start)}",
        f"DTEND:{utc_stamp(end)}",
        f"SUMMARY:{escape_text(summary)}",
    ]
    location = str(event.get("location") or "").strip()
    if location:
        lines.append(f"LOCATION:{escape_text(location)}")
    lines.extend([
        f"DESCRIPTION:{escape_text(build_description(event))}",
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
        "END:VEVENT",
    ])
    return lines


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
        "source_id": event.get("source_id", ""),
        "start": event["start"].astimezone(timezone.utc).isoformat(),
        "duration_hours": event["duration"].total_seconds() / 3600.0,
        "left": event["left"],
        "right": event["right"],
        "score": list(event["score"]) if event["score"] else None,
        "finished": event["finished"],
        "bestof": event["bestof"],
        "tournament": event["tournament"],
        "location": event.get("location", ""),
        "maps": event["maps"],
        "status": event["status"],
        "data_note": event.get("data_note", ""),
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
        "source_id": str(data.get("source_id") or ""),
        "start": start,
        "duration": duration,
        "left": str(data["left"]),
        "right": str(data["right"]),
        "score": tuple(score) if isinstance(score, (list, tuple)) and len(score) == 2 else None,
        "finished": bool(data.get("finished")),
        "bestof": _as_int(data.get("bestof")),
        "tournament": str(data.get("tournament") or ""),
        "location": str(data.get("location") or ""),
        "maps": data.get("maps") or [],
        "status": str(data.get("status") or ""),
        "data_note": str(data.get("data_note") or ""),
    }


def load_cache() -> tuple[dict, dict]:
    """返回 (比赛缓存, 赛事位置缓存)。"""
    if not os.path.exists(CACHE_FILE):
        return {}, {}
    try:
        with open(CACHE_FILE, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"[cache] 读取 {CACHE_FILE} 失败，将忽略：{exc}")
        return {}, {}
    raw = payload.get("matches") if isinstance(payload, dict) else payload
    cache = {}
    for item in raw or []:
        event = _event_from_json(item)
        if event:
            cache[event["key"]] = event
    locations = payload.get("event_locations") if isinstance(payload, dict) else {}
    locations = locations if isinstance(locations, dict) else {}
    print(f"[cache] 已有 {len(cache)} 场历史记录、{len(locations)} 个赛事位置")
    return cache, locations


def save_cache(cache: dict, locations: dict | None = None) -> None:
    items = [_event_to_json(event) for event in sorted(cache.values(), key=lambda e: e["start"])]
    payload = {
        "team": TEAM_NAME,
        "year": YEAR,
        "count": len(items),
        "event_locations": locations or {},
        "matches": items,
    }
    with open(CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")


def _is_richer(candidate: dict, current: dict) -> bool:
    """候选记录是否比现有记录信息更全。

    用于避免「数据源某天把已有结果变回未结束」这种倒退把缓存里的结果冲掉：
    有结果的胜过没结果的，有逐地图比分的胜过没有的；同强度时以新数据为准。
    """
    if bool(candidate["finished"]) != bool(current["finished"]):
        return bool(candidate["finished"])
    if candidate["finished"] and bool(candidate["maps"]) != bool(current["maps"]):
        return bool(candidate["maps"])
    return True


def merge_with_cache(events: list, cache: dict) -> tuple[list, int]:
    """接口的数据优先（但不会用更差的记录覆盖更好的）；接口本次没返回、
    但缓存里有且属于本年的比赛保留下来。"""
    merged = dict(cache)
    for event in events:
        current = merged.get(event["key"])
        if current is None or _is_richer(event, current):
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
    cache, locations = load_cache()
    events, calendar_note = collect_events(locations)

    events, kept_from_cache = merge_with_cache(events, cache)
    if kept_from_cache:
        print(f"[cache] 本次接口未返回、但缓存保留的比赛：{kept_from_cache} 场")
    print(f"[cache] 合并后共 {len(events)} 场（已结束 {sum(1 for e in events if e['finished'])} 场）")

    if not events:
        raise SystemExit(
            f"错误：接口与缓存都没有 {TEAM_NAME} 在 {YEAR} 年的比赛，拒绝写出空日历。\n"
            f"请检查 CITO_API_KEY / TEAM_SLUG / YEAR 配置是否正确。"
        )

    save_cache({event["key"]: event for event in events}, locations)

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

#!/usr/bin/env python3
"""校验 matches.ics 的内容是否符合本项目的格式要求（CI 在提交前运行）。

validate_ics.py 负责字节层与结构层（CRLF、折行、必填字段、UID 唯一等）；
本脚本再往上检查"内容对不对"：

  - 已结束的比赛：标题必须是「队伍A 比分-比分 队伍B」；
  - 未进行的比赛：标题必须是「队伍A VS 队伍B」，且不含比分；
  - 每场比赛的备注必须含比赛名称（"比赛：…"）；
  - 已结束比赛的备注必须列出逐张地图比分（数据源缺数据时给出警告，不算失败）；
  - 比赛 ID 必须存在（UID 由它派生）。

区分「已结束 / 未进行」的依据：备注里出现「地图比分」即为已结束。
"""

import re
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "matches.ics"
SCORE_PATTERN = re.compile(r" \d+-\d+ ")

problems = []
warnings = []


def fail(msg):
    problems.append(msg)


def warn(msg):
    warnings.append(msg)


def unfold(text):
    """解折行：RFC 5545 每 75 字节折行，续行开头的那个空格不属于内容。"""
    return text.replace("\r\n ", "")


def parse_events(text):
    events, current = [], None
    for line in text.split("\r\n"):
        name, _, value = line.partition(":")
        if name == "BEGIN" and value == "VEVENT":
            current = {}
        elif name == "END" and value == "VEVENT" and current is not None:
            events.append(current)
            current = None
        elif current is not None and name:
            current.setdefault(name, value)
    return events


def main():
    with open(PATH, encoding="utf-8", newline="") as fh:
        raw = fh.read()

    events = parse_events(unfold(raw))
    if not events:
        fail("没有任何 VEVENT")
        return

    finished = upcoming = 0
    missing_maps = []
    missing_location = []
    for index, event in enumerate(events, 1):
        summary = event.get("SUMMARY", "")
        description = event.get("DESCRIPTION", "").replace("\\n", "\n")
        uid = event.get("UID", "")
        label = f"第 {index} 个事件（{uid or '无 UID'}）"
        lines = [line for line in description.split("\n") if line.strip()]

        for prop in ("UID", "DTSTART", "DTEND", "SUMMARY", "DESCRIPTION"):
            if prop not in event:
                fail(f"{label} 缺少 {prop}")

        if not uid.endswith("@lvlongrili"):
            fail(f"{label} 的 UID 不是稳定的日历 ID（应以 @lvlongrili 结尾）：{uid!r}")

        # 备注第一行必须是赛事名称（不能是「比分：/赛制：/地图比分：/比赛ID：」这类标签行）
        first = lines[0] if lines else ""
        if not first:
            fail(f"{label} 的备注为空")
        elif first.startswith(("比分：", "赛制：", "地图比分：", "比赛ID：")):
            fail(f"{label} 的备注第一行不是赛事名称：{first!r}")
        if "比赛ID：" not in description:
            fail(f"{label} 的备注没有比赛 ID：{summary!r}")

        # 地点（比赛举办城市）：有就必须非空
        location = event.get("LOCATION")
        if location is not None and not str(location).strip():
            fail(f"{label} 的 LOCATION 为空")

        score_line = next((line for line in lines if line.startswith("比分：")), None)
        title_score = SCORE_PATTERN.search(summary)
        is_finished = "地图比分" in description

        if is_finished:
            finished += 1
            if not title_score:
                fail(f"{label} 是已结束比赛，标题里却没有「比分」：{summary!r}")
            if " VS " in summary:
                fail(f"{label} 是已结束比赛，标题却仍是 VS 形式：{summary!r}")
            if not score_line:
                fail(f"{label} 是已结束比赛，备注里缺少「比分：」行：{summary!r}")
            elif title_score and score_line.strip() != f"比分：{title_score.group(0).strip()}":
                fail(f"{label} 备注里的比分与标题不一致：{score_line!r} vs {summary!r}")
            if "地图比分：数据缺失" in description:
                missing_maps.append(summary)
            if location is None:
                missing_location.append(summary)
        else:
            upcoming += 1
            if " VS " not in summary:
                fail(f"{label} 是未进行比赛，标题却不是「队伍A VS 队伍B」：{summary!r}")
            if SCORE_PATTERN.search(summary):
                fail(f"{label} 是未进行比赛，标题里却带了比分：{summary!r}")
            if score_line:
                fail(f"{label} 是未进行比赛，备注里不应有「比分：」行：{score_line!r}")

    print(f"{PATH} 内容检查：共 {len(events)} 场（已结束 {finished}、未进行 {upcoming}）")
    if missing_maps:
        warn(f"有 {len(missing_maps)} 场已结束比赛没有逐地图比分（数据源缺数据），例如：")
        for summary in missing_maps[:5]:
            warn(f"    {summary}")
    if missing_location:
        warn(f"有 {len(missing_location)} 场比赛没有地点信息（数据源未给举办城市），例如：")
        for summary in missing_location[:5]:
            warn(f"    {summary}")


if __name__ == "__main__":
    main()
    for item in warnings:
        print(f"  警告：{item}" if not item.startswith("    ") else f"  {item}")
    if problems:
        print(f"{PATH} 内容检查失败：")
        for item in problems:
            print(f"  - {item}")
        sys.exit(1)
    print(f"{PATH} 内容检查通过")

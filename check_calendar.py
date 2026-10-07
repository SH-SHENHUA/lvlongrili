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
    for index, event in enumerate(events, 1):
        summary = event.get("SUMMARY", "")
        description = event.get("DESCRIPTION", "").replace("\\n", "\n")
        uid = event.get("UID", "")
        label = f"第 {index} 个事件（{uid or '无 UID'}）"

        for prop in ("UID", "DTSTART", "DTEND", "SUMMARY", "DESCRIPTION"):
            if prop not in event:
                fail(f"{label} 缺少 {prop}")

        if not uid.endswith("@lvlongrili"):
            fail(f"{label} 的 UID 不是由比赛 ID 派生：{uid!r}")

        if "比赛：" not in description:
            fail(f"{label} 的备注没有比赛名称：{summary!r}")
        if "比赛ID：" not in description:
            fail(f"{label} 的备注没有比赛 ID：{summary!r}")

        is_finished = "地图比分" in description
        if is_finished:
            finished += 1
            if not SCORE_PATTERN.search(summary):
                fail(f"{label} 是已结束比赛，标题里却没有「比分」：{summary!r}")
            if " VS " in summary:
                fail(f"{label} 是已结束比赛，标题却仍是 VS 形式：{summary!r}")
            if "地图比分：数据缺失" in description:
                missing_maps.append(summary)
        else:
            upcoming += 1
            if " VS " not in summary:
                fail(f"{label} 是未进行比赛，标题却不是「队伍A VS 队伍B」：{summary!r}")
            if SCORE_PATTERN.search(summary):
                fail(f"{label} 是未进行比赛，标题里却带了比分：{summary!r}")

    print(f"{PATH} 内容检查：共 {len(events)} 场（已结束 {finished}、未进行 {upcoming}）")
    if missing_maps:
        warn(f"有 {len(missing_maps)} 场已结束比赛没有逐地图比分（数据源缺数据），例如：")
        for summary in missing_maps[:5]:
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

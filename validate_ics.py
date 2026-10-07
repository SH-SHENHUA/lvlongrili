#!/usr/bin/env python3
"""校验 matches.ics 是否能被 iPhone（iOS 日历）正常导入。

只做字节层与结构层检查，不联网、不依赖第三方库，供 CI 在提交前把关：
一旦生成结果不满足导入要求，就直接失败，避免把坏文件推到仓库里。
"""

import sys
from datetime import datetime

PATH = sys.argv[1] if len(sys.argv) > 1 else "matches.ics"

REQUIRED_EVENT_PROPS = ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY", "DESCRIPTION")
TIME_PROPS = ("DTSTART", "DTEND", "DTSTAMP")

problems = []


def fail(msg):
    problems.append(msg)


def main():
    raw = open(PATH, "rb").read()

    if raw.startswith(b"\xef\xbb\xbf"):
        fail("文件带 UTF-8 BOM，iOS 日历会解析失败")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        fail(f"不是合法 UTF-8：{exc}")
        return

    if not raw.endswith(b"\r\n"):
        fail("文件没有以 CRLF 结尾")

    stripped = raw.replace(b"\r\n", b"")
    if b"\n" in stripped or b"\r" in stripped:
        fail("存在裸 LF/CR，RFC 5545 要求 CRLF 行尾")

    physical = raw.split(b"\r\n")
    if physical and physical[-1] == b"":
        physical.pop()
    for idx, line in enumerate(physical, 1):
        if len(line) > 75:
            fail(f"第 {idx} 行 {len(line)} 字节，超过 75 字节且未折行")

    logical = []
    for line in physical:
        if line[:1] in (b" ", b"\t") and logical:
            logical[-1] += line[1:]
        else:
            logical.append(line)
    logical = [l.decode("utf-8") for l in logical]

    events = []
    stack = []
    for line in logical:
        name, _, value = line.partition(":")
        if name == "BEGIN":
            stack.append({"name": value, "props": {}})
        elif name == "END":
            if not stack or stack[-1]["name"] != value:
                fail(f"BEGIN/END 不匹配：{value}")
                return
            comp = stack.pop()
            if value == "VEVENT":
                events.append(comp)
        elif stack:
            stack[-1]["props"].setdefault(name, []).append(value)

    if stack:
        fail(f"组件未闭合：{[c['name'] for c in stack]}")
    for prop in ("VERSION:2.0", "CALSCALE:GREGORIAN"):
        if prop not in logical:
            fail(f"缺少 {prop}")
    if not any(l.startswith("PRODID:") for l in logical):
        fail("缺少 PRODID")

    if not events:
        fail("没有任何 VEVENT，导入到 iPhone 后会是空日历")

    seen_uids = set()
    for ev in events:
        for prop in REQUIRED_EVENT_PROPS:
            if prop not in ev["props"]:
                fail(f"VEVENT 缺少字段 {prop}")
        for prop in TIME_PROPS:
            value = (ev["props"].get(prop) or [""])[0]
            try:
                datetime.strptime(value, "%Y%m%dT%H%M%SZ")
            except ValueError:
                fail(f"{prop} 不是 UTC Z 形式：{value!r}")
        uid = (ev["props"].get("UID") or [""])[0]
        if uid in seen_uids:
            fail(f"UID 重复：{uid}")
        seen_uids.add(uid)
        for prop in ("SUMMARY", "DESCRIPTION"):
            for value in ev["props"].get(prop, []):
                if "\n" in value or "\r" in value:
                    fail(f"{prop} 含未转义的换行")


if __name__ == "__main__":
    main()
    if problems:
        print(f"{PATH} 校验失败：")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print(f"{PATH} 校验通过")

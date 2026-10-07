# lvlongrili

生成 **Team Spirit CS2 2026 年赛程**的 `.ics` 日历文件，可直接导入 iPhone 的「日历」App。

## 行为

- 比赛标题沿用曼联日历的命名方式：
  - **未进行**：`队伍A VS 队伍B`
  - **已结束**：`队伍A 比分-比分 队伍B`
- **队名不翻译**，原样保留
- 每条日程的**备注（描述）显示比赛名称**（赛事名 + 阶段）
- **已结束的比赛，备注里逐张列出地图比分**，例如：

  ```
  比赛：BLAST Premier Spring 2026 - Group A
  赛制：BO3
  地图比分：
    1. Dust2 13-9
    2. Mirage 9-13
    3. Inferno 13-11
  比赛ID：...
  ```

- 日程时长按赛制估算：BO1 = 1 小时、BO3 = 2.5 小时、BO5 = 4 小时，未知按 2 小时
- 时间以 UTC 写入，iPhone 会按你所在时区显示
- 每个事件的 `UID` 由比赛 ID 稳定派生，**导入到同一个日历时是更新事件，不会产生重复条目**
- GitHub Actions **每天**自动更新一次（03:00 UTC = 北京时间 11:00）

## 数据来源：Liquipedia（需要免费 API key）

数据来自 [Liquipedia LPDB v3](https://api.liquipedia.net/api/v3/match) 的 `counterstrike` wiki。
它同时提供赛事名（`tournament`）、双方与系列赛比分（`match2opponents`）、
以及**逐地图数据（`match2games`）**，正好满足上面的要求。

### 为什么必须用 Liquipedia

实测排除了所有「无需 key」的来源：

| 来源 | 结果 |
| --- | --- |
| TheSportsDB（免费） | 有 Team Spirit 队伍记录，但 CS2 比赛数据全为空（其电竞只覆盖 LoL / 火箭联盟） |
| bo3.gg（免费，接口可直连） | 比赛对象里的 `match_maps` **没有逐地图比分**，且无法按队伍列出赛程 |
| HLTV 直连 / 社区镜像 | Cloudflare 403，GitHub Actions 同样会被拦 |
| PandaScore（官方，有免费档） | 免费档**不含**比赛内的每一局（=每张地图），需付费 Historical 档 |

### 申请 key（免费）

1. 加入 Liquipedia 的 Discord：**https://discord.gg/liquipedia**
2. 按其指引提交 API key 申请（说明用途：个人赛程日历，**每天仅少量请求**，远低于免费档 60 次/小时）
3. 在仓库 `Settings → Secrets and variables → Actions` 新增 Secret：**`LIQUIPEDIA_API_KEY`**

他们的[使用条款](https://liquipedia.net/api-terms-of-use)要求请求头里的 User-Agent 写明用途与联系方式，
本脚本默认已带（也可用 `LIQUIPEDIA_USER_AGENT` 覆盖）。

## 可调配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LIQUIPEDIA_API_KEY` | 无（**必填**） | API key |
| `TEAM_NAME` | `Team Spirit` | 队伍名 |
| `YEAR` | `2026` | 只取该自然年的比赛 |
| `LIQUIPEDIA_WIKI` | `counterstrike` | Liquipedia wiki |
| `LIQUIPEDIA_DELAY` | `2` | 请求间隔秒数（遵守节流） |
| `OUTPUT_FILE` | `matches.ics` | 输出文件名 |

## 导入 iPhone 日历

用 iPhone 的 Safari 打开下面任一地址，系统会提示「在"日历"中打开」，选择要加入的日历并确认：

1. GitHub Pages（更短，手机上手打方便）：
   `https://sh-shenhua.github.io/lvlongrili/matches.ics`
2. raw 地址：
   `https://raw.githubusercontent.com/SH-SHENHUA/lvlongrili/main/matches.ics`

## 文件

| 文件 | 作用 |
| --- | --- |
| `generate.py` | 拉取 Liquipedia 数据并输出 `matches.ics` |
| `validate_ics.py` | 校验 `matches.ics` 是否符合 iOS 导入要求（CI 在提交前运行） |
| `matches.ics` | 生成结果 |
| `.gitattributes` | 禁止 Git 对 `*.ics` 做行尾转换（RFC 5545 要求 CRLF） |

## 本地运行

```bash
pip install -r requirements.txt
export LIQUIPEDIA_API_KEY=你的key          # Windows: set LIQUIPEDIA_API_KEY=你的key
python generate.py
python validate_ics.py matches.ics
```

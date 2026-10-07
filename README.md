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
  比赛：BLAST Bounty Spring 2026 - Quarterfinals
  赛制：BO3
  地图比分：
    1. Dust2 13-9
    2. Mirage 9-13
    3. Inferno 11-13
  比赛ID：cs2-match-2398526
  ```

- 日程时长按赛制估算：BO1 = 1 小时、BO3 = 2.5 小时、BO5 = 4 小时，未知按 2 小时
- 时间以 UTC 写入，iPhone 会按你所在时区显示
- 每个事件的 `UID` 由比赛 ID 稳定派生，**导入到同一个日历时是更新事件，不会产生重复条目**
- GitHub Actions **每天**自动更新一次（03:00 UTC = 北京时间 11:00）

## 数据来源：Cito API 的 CS2 接口（免费档 500 次/月）

数据来自 [cs2-api.org](https://cs2-api.org)（Cito API）的 CS2 接口。
文档明确写着覆盖「**results with every map score**（每一张地图的比分）」，索引约 1.5 万场比赛。

用到的端点与字段：

| 你的要求 | 接口与字段 |
| --- | --- |
| 该队全部比赛（含 maps） | `GET /cs2/teams/{slug}/matches` → `data[]`，每条内嵌 `maps[]` |
| 比赛名称 | `eventName` + `stageName` |
| 双方与系列赛比分 | `team1Name` / `team2Name` / `team1Score` / `team2Score` |
| **每张地图的比分** | `maps[].mapName`，比分优先取 `team1Score`/`team2Score`，为空时用 `team1Halves`/`team2Halves` 相加 |
| 开赛时间 / 赛制 / 状态 | `startsAt`（UTC，ISO 8601）/ `bestOf` / `status`（`upcoming`/`live`/`completed`） |

鉴权：请求头 `x-api-key`；响应统一为 `{ success, data, meta }`。

> 关于逐地图比分的还原：官方示例里 `maps[].team1Score`/`team2Score` 可能为 `null`，
> 但 `team1Halves`/`team2Halves`（上下半场回合数）是齐全的，且 `durationRounds` 等于两队回合数之和
> （示例 `[10,3]` vs `[2,0]` → 13-2，`durationRounds` 15 = 13+2）。因此脚本按这个规则还原比分。

### 为什么用 Cito（对比其它来源，均为实测）

| 来源 | 逐地图比分 | 拿 key | 结论 |
| --- | --- | --- | --- |
| **Cito API（cs2-api.org）** | ✅ `maps[]`（逐张地图） | **免费，邮箱注册，无需信用卡** | **采用** |
| bzzoiro CS2 API | ❌ **实测 55 场已结束比赛 `maps` 全为空**（live 接口与 `?include=maps` 同样为空，`/stats/` 的 `map_pool` 也是空） | 免费，邮箱 | 排除（赛程可用，但拿不到逐地图比分） |
| Liquipedia LPDB v3 | ✅ `match2games` | 免费，但需加入 **Discord** 申请 | 备选 |
| PandaScore | ❌ 免费档不含「比赛内的每一局」，需付费 Historical | 简单 | 排除 |
| TheSportsDB | ❌ 无 CS2 比赛数据（其电竞只覆盖 LoL / 火箭联盟） | 无需 key | 排除 |
| bo3.gg | ❌ `match_maps` 只有地图名/顺序，没有比分；且无法按队伍列出赛程 | 无需 key | 排除 |
| HLTV 直连 / 社区镜像 | 页面有数据 | —— | 全部 **403**（Cloudflare），GitHub Actions 同样会被拦 |

请求量：每天只需 1 次请求（`/cs2/teams/{slug}/matches`，`limit=250`），
约 30 次/月，远低于免费档 500 次/月。

### 申请 key（免费）

1. 打开 **https://citoapi.com/signup?game=cs2**，用邮箱注册（500 次/月，10 次/分钟，无需信用卡）
2. 在控制台复制 API key
3. 在仓库 `Settings → Secrets and variables → Actions` 新增 Secret：**`CITO_API_KEY`**

## 可调配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CITO_API_KEY` | 无（**必填**） | API key |
| `TEAM_SLUG` | `spirit` | 队伍 slug（如 `vitality`、`natus-vincere`），也接受 id |
| `TEAM_NAME` | `Team Spirit` | 队名，用于识别「哪些比赛是本队的」并写入日历名 |
| `YEAR` | `2026` | 只取该自然年的比赛 |
| `REQUEST_DELAY` | `0.5` | 请求间隔秒数 |
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
| `generate.py` | 拉取 Cito 数据并输出 `matches.ics` |
| `validate_ics.py` | 校验 `matches.ics` 是否符合 iOS 导入要求（CI 在提交前运行） |
| `matches.ics` | 生成结果 |
| `.gitattributes` | 禁止 Git 对 `*.ics` 做行尾转换（RFC 5545 要求 CRLF） |

## 本地运行

```bash
pip install -r requirements.txt
export CITO_API_KEY=你的key      # Windows: set CITO_API_KEY=你的key
python generate.py
python validate_ics.py matches.ics
```

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
- 提交前会过**两道闸门**：`validate_ics.py`（字节层/结构层，保证 iPhone 能导入）与
  `check_calendar.py`（内容层，保证命名与备注符合上面的规则）；任一不过都不会提交

## 数据源：可选 Cito（默认）或 Liquipedia

脚本内置两个数据源，用环境变量 `SOURCE` 切换（`cito` / `liquipedia`），字段映射见下。

### 源 A：Cito API 的 CS2 接口（默认，免费档 500 次/月）

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

请求量：每天 2 次请求（`/cs2/teams/{slug}` 确认队伍 + `/cs2/teams/{slug}/matches?limit=250` 取比赛），
约 60 次/月，仍远低于免费档 500 次/月。

`TEAM_SLUG` 写错时会有明确提示（而不是一串 traceback）：

```
错误：Cito 里找不到队伍 slug='spirit'（HTTP 404）。
请确认 TEAM_SLUG：例如 Team Spirit 通常为 spirit（也可直接用队伍 id，形如 cs2-team-7020）。
```

### ⚠️ 免费档只开放最近 30 天的历史

Cito 各档的**历史深度**不同（见[定价页](https://cs2-api.org/pricing/)）：

| 档位 | 价格 | 历史深度 |
| --- | --- | --- |
| Free | $0（500 次/月） | **最近 30 天** |
| Starter | $15/月 | 90 天 |
| Pro | $59/月 | 全量历史 |

也就是说：**用免费档无法回填 2026 年较早的比赛**，接口只返回最近 30 天的列表。
为此脚本带了**累积缓存**（见下），保证日历只会越来越完整。

### 累积缓存：日历只增不减

每轮运行会把已见过的比赛写入 `matches_cache.json`（随仓库提交）：

- 接口这次返回的比赛 → 覆盖缓存里的同名比赛（取最新比分）
- 接口**不再返回**、但缓存里有且属于 `YEAR` 的比赛 → **保留在日历里**

因此即使数据源只给最近 30 天，随着每天运行，日历会自动积累；
若哪天升级到有全量历史的档位或换成有全量历史的来源，早期比赛也能补齐。

**休赛期也安全**：如果某次运行接口一条比赛都没返回（休赛期、或窗口内确实没有比赛），
脚本不会失败退出，而是用缓存里的比赛继续生成日历；
只有「接口和缓存都没有比赛」时才会明确报错，拒绝写出空日历。

### 申请 key（免费）

1. 打开 **https://citoapi.com/signup?game=cs2**，用邮箱注册（500 次/月，10 次/分钟，无需信用卡）
2. 在控制台复制 API key
3. 在仓库 `Settings → Secrets and variables → Actions` 新增 Secret：**`CITO_API_KEY`**

### 源 B：Liquipedia LPDB v3（全量历史，可完整回填）

Cito 免费档只有 30 天历史，因此 2026 年较早的比赛无法回填。Liquipedia 的 LPDB
有全量历史与逐地图数据（`match2games`），可以补上：

```
GET https://api.liquipedia.net/api/v3/match
  ?wiki=counterstrike
  &conditions=[[date::>2025-12-31]] AND [[date::<2027-01-01]] AND [[match2opponents::"Team Spirit"]]
  &limit=250&offset=0&order=date ASC
Header: Authorization: Apikey <你的key>
```

字段映射：`tournament`（+`section`）→ 比赛名称；`match2opponents` → 双方与系列赛比分；
`match2games` → 逐地图比分；`date`（UTC）→ 开赛时间；`bestof` → 时长；`finished` → 是否已结束。

启用方式：设 `SOURCE=liquipedia`，并把 key 加到 Secret：**`LIQUIPEDIA_API_KEY`**。
他们的条款要求 User-Agent 写明用途与联系方式，脚本默认已带（可用 `LIQUIPEDIA_USER_AGENT` 覆盖）。

> key 申请：Liquipedia 的 key 免费，但需要通过加入他们的 **Discord** 申请
> （其条款页从本环境访问被 Cloudflare 拦截，具体步骤以其站内说明为准）。
> 条件写法可能有多种，脚本会按 `match2opponents` → `opponent` → 仅时间窗 的顺序自动尝试，
> 并在日志里写明最终采用哪一种。

### 源选择对比

| | 源 A：Cito（免费档） | 源 B：Liquipedia |
| --- | --- | --- |
| 历史深度 | 最近 30 天 | 全量 |
| 逐地图比分 | ✅ `maps[]`（含上下半场） | ✅ `match2games` |
| 拿 key | 邮箱注册，1 分钟 | 需加入 Discord 申请 |
| 每天请求 | 2 次 | 1–2 次 |

用 `SOURCE` 切换即可，两种源产出的日历格式完全一致，也都受下面的累积缓存保护。

## 可调配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SOURCE` | `cito` | 数据源：`cito` 或 `liquipedia` |
| `CITO_API_KEY` | 无（`SOURCE=cito` 时必填） | Cito API key |
| `LIQUIPEDIA_API_KEY` | 无（`SOURCE=liquipedia` 时必填） | Liquipedia API key |
| `LIQUIPEDIA_USER_AGENT` | 带用途与联系方式的默认值 | Liquipedia 条款要求 |
| `LIQUIPEDIA_WIKI` | `counterstrike` | Liquipedia wiki |
| `TEAM_SLUG` | `spirit` | 队伍 slug（如 `vitality`、`natus-vincere`），也接受 id。**这是取数据用的关键配置** |
| `TEAM_NAME` | `Team Spirit` | 仅用于**日历显示名**与过滤兜底。脚本会从接口返回数据里自识别本队名（例如接口把 Team Spirit 叫 `Spirit`），因此两者不一致也不会丢比赛 |
| `TEAM_ALIASES` | 空 | 可选，额外的队名写法（逗号分隔），用于 Liquipedia 查询条件与过滤 |
| `YEAR` | `2026` | 只取该自然年的比赛 |
| `REQUEST_DELAY` | `0.5` | 请求间隔秒数 |
| `OUTPUT_FILE` | `matches.ics` | 输出文件名 |
| `CACHE_FILE` | `matches_cache.json` | 累积缓存文件名 |

## 导入 iPhone 日历

用 iPhone 的 Safari 打开下面任一地址，系统会提示「在"日历"中打开」，选择要加入的日历并确认：

1. GitHub Pages（更短，手机上手打方便）：
   `https://sh-shenhua.github.io/lvlongrili/matches.ics`
2. raw 地址：
   `https://raw.githubusercontent.com/SH-SHENHUA/lvlongrili/main/matches.ics`

## 文件

| 文件 | 作用 |
| --- | --- |
| `generate.py` | 拉取数据并输出 `matches.ics` |
| `validate_ics.py` | 字节层/结构层校验（CRLF、折行、必填字段、UID 唯一、无裸 LF） |
| `check_calendar.py` | **内容层校验**：已结束必须是「队 比分 队」、未进行必须是「队 VS 队」且不带比分、每条备注必须含比赛名称与比赛 ID；已结束比赛缺地图数据时只警告 |
| `matches.ics` | 生成结果 |
| `matches_cache.json` | 累积缓存（只增不减，配合有限历史窗口的数据源使用） |
| `.gitattributes` | 禁止 Git 对 `*.ics` 做行尾转换（RFC 5545 要求 CRLF） |

## 本地运行

```bash
pip install -r requirements.txt
export CITO_API_KEY=你的key      # Windows: set CITO_API_KEY=你的key
python generate.py
python validate_ics.py matches.ics
```

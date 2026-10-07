# lvlongrili

生成 **Team Spirit CS2 2026 年赛程**的 `.ics` 日历文件，可直接导入 iPhone 的「日历」App。

## 行为

- 比赛标题沿用曼联日历的命名方式：
  - **未进行**：`队伍A VS 队伍B`
  - **已结束**：`队伍A 比分-比分 队伍B`
- **队名不翻译**，原样保留
- **地点显示比赛举办的城市**（来自赛事详情，如 `Katowice, Poland`；线上赛事为 `Europe (Online)`）
- **备注（描述）的格式**：第一行是**赛事名称**，紧接着是比分，然后赛制、逐张地图比分、比赛 ID：

  ```
  BLAST Bounty 2026 Season 1      ← 第一行：赛事名称
  比分：0-2                        ← 紧接着：系列赛比分（仅已结束）
  赛制：BO3
  地图比分：                        ← 仅已结束
    1. Mirage 11-13
    2. Nuke 5-13
  比赛ID：cs2-match-2389265
  ```

- 每个事件的 `UID` 由**「UTC 日期 + 双方队名」派生的规范键**生成（与数据源无关），因此：
  - 导入到同一个日历时是**更新事件**，不会产生重复条目；
  - **更换数据源也是原地更新**（而不是同一场比赛多出一条）；数据源自己的比赛 ID 仍写在备注里便于溯源
- 数据源给出的系列赛比分若**不可能是终局**（例如实测遇到 BO1 却给出 1-1），不会显示该比分，
  而是退回 `队伍A VS 队伍B` 并在备注注明「比分待核实（数据源给出 1-1）」
- 时长按赛制估算：BO1 = 1 小时、BO3 = 2.5 小时、BO5 = 4 小时，未知按 2 小时
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

请求量：首次运行约 17 次（1 次队伍资料 + 2 页比赛列表 + 15 个赛事的位置），
之后每天通常只需 3 次（队伍资料 + 2 页列表，位置已缓存），远低于免费档 500 次/月。
注意免费档限 **10 次/分钟**，脚本默认每次请求间隔 6.5 秒并会对 429 自动重试。

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

**也可以同时用两个源合并**（`SOURCE=cito,liquipedia`）：按「UTC 日期 + 双方队名」去重，
**写在后面的源优先**。之所以需要这个能力 —— 实测 Cito 对 Esports World Cup 2026 的覆盖不完整
（Spirit 实际打完整届并夺冠，Cito 只收录 2 场，其中 1 场还停在未结束），
加上 Liquipedia 后可以把这段补上，且已有的比赛是原地更新、不会重复。

**赛事对阵结构补齐 + 人工补录**：脚本还会用赛事详情的对阵结构（`eventStructure`）做两件事 ——
① 把「队伍接口里停在未结束、但对阵结构已有比分」的场次回填结果（实测 2 场）；
② 找出「队伍接口完全没收录」的场次并尝试取详情（实测有 4 场详情接口返回 404，拿不到开赛时间）。
取不到时间的那几场已用 `manual_matches.json` **人工补录**（只放已完赛比赛，比分取自对阵结构、
日期取自赛程记录，来源写在文件注释里）。因此当前日历是 **59 场**，
其中 **6 场没有逐地图比分**（4 场人工补录 + 2 场接口只给了系列赛比分），备注里都注明了「地图比分：数据缺失」。

**地点是「赛事级」的**：`LOCATION` 取自赛事（`GET /cs2/events/{id}`），
因此跨场馆的赛事会显示赛事的登记地点。例如 Esports World Cup 2026 主赛场在利雅得、
但总决赛移师巴黎 Accor Arena，Cito 把该赛事的地点登记为 `Paris, France`，
所以该赛事的比赛都会显示巴黎。单场馆赛事不受影响（如 `Katowice, Poland`）。
线上赛事显示为 `Europe (Online)`。

## 可调配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SOURCE` | `cito` | 数据源：`cito` 或 `liquipedia`；**可用逗号分隔多个源合并**（如 `cito,liquipedia`，写在后面的优先） |
| `CITO_API_KEY` | 无（`SOURCE=cito` 时必填） | Cito API key |
| `LIQUIPEDIA_API_KEY` | 无（`SOURCE=liquipedia` 时必填） | Liquipedia API key |
| `LIQUIPEDIA_USER_AGENT` | 带用途与联系方式的默认值 | Liquipedia 条款要求 |
| `LIQUIPEDIA_WIKI` | `counterstrike` | Liquipedia wiki |
| `TEAM_SLUG` | `spirit` | 队伍 slug（如 `vitality`、`natus-vincere`），也接受 id。**这是取数据用的关键配置** |
| `TEAM_NAME` | `Team Spirit` | 仅用于**日历显示名**与过滤兜底。脚本会从接口返回数据里自识别本队名（例如接口把 Team Spirit 叫 `Spirit`），因此两者不一致也不会丢比赛 |
| `TEAM_ALIASES` | 空 | 可选，额外的队名写法（逗号分隔），用于 Liquipedia 查询条件与过滤 |
| `YEAR` | `2026` | 只取该自然年的比赛 |
| `REQUEST_DELAY` | `6.5` | 请求间隔秒数。Cito 免费档限 **10 次/分钟**，所以默认 6.5 秒 |
| `RATE_LIMIT_WAIT` | `20` | 遇到 429 限流时等待秒数（响应带 Retry-After 时以其为准，最多重试 3 次） |
| `OUTPUT_FILE` | `matches.ics` | 输出文件名 |
| `CACHE_FILE` | `matches_cache.json` | 累积缓存文件名（同时缓存「赛事 → 举办城市」，位置几乎不变，无需每天重查） |
| `MANUAL_FILE` | `manual_matches.json` | 人工补录文件名 |
| `EVENT_REFRESH_DAYS` | `7` | 赛事详情最多多少天重拉一次（赛事可能仍在进行、还会新增比赛） |

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
| `matches_cache.json` | 累积缓存（只增不减，配合有限历史窗口的数据源使用；同时缓存赛事地点与对阵结构） |
| `manual_matches.json` | **人工补录**：数据源缺失、且比赛详情接口取不到（实测 404）的**已完赛**场次。只放已完赛比赛，比分来自赛事对阵结构，日期来自赛程记录，来源写在文件注释里 |
| `.gitattributes` | 禁止 Git 对 `*.ics` 做行尾转换（RFC 5545 要求 CRLF） |

## 本地运行

```bash
pip install -r requirements.txt
export CITO_API_KEY=你的key      # Windows: set CITO_API_KEY=你的key
python generate.py
python validate_ics.py matches.ics
python check_calendar.py matches.ics
```

## 首次运行自检清单（常见问题）

1. **工作流在 “Run script” 步骤失败**：多半是仓库里还没加 Secret。
   日志会明确写「缺少 CITO_API_KEY」并给出注册地址；失败时**不会**提交任何文件，
   仓库里已有的 `matches.ics` 保持不动。
2. **工作流跑到提交时报 403 / 无法 push**：GitHub 对新仓库的 Actions 权限有时默认是只读。
   到 `Settings → Actions → General → Workflow permissions` 选
   **Read and write permissions** 再重跑一次。
3. **运行成功但 `matches.ics` 没变化**：说明比赛内容与上次完全一致（只差 DTSTAMP），
   脚本会跳过写入并在日志里说明 —— 这是有意为之，避免每天产生无意义提交。
4. **某场比赛没出现在日历里**：看日志里的「过滤掉 非本队 / 年份不符 / 缺少开赛时间 / 重复」
   统计，以及「缺少开赛时间」的逐条警告（接口偶尔不给开赛时间）。
5. **已结束比赛但备注写「地图比分：数据缺失」**：说明该场数据源没给逐地图数据。
   内容检查闸门只警告不失败，日历照常更新。

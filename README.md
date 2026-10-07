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
  比赛ID：5001
  ```

- 日程时长按赛制估算：BO1 = 1 小时、BO3 = 2.5 小时、BO5 = 4 小时，未知按 2 小时
- 时间以 UTC 写入，iPhone 会按你所在时区显示
- 每个事件的 `UID` 由比赛 ID 稳定派生，**导入到同一个日历时是更新事件，不会产生重复条目**
- GitHub Actions **每天**自动更新一次（03:00 UTC = 北京时间 11:00）

## 数据来源：bzzoiro CS2 API（免费，注册只要邮箱）

数据来自 [bzzoiro Sports Data API 的 CS2 接口](https://sports.bzzoiro.com/docs/explorer/csgo/)
（官方 [OpenAPI 规范](https://sports.bzzoiro.com/api/schema/)）。用到的字段：

| 你的要求 | 对应字段 |
| --- | --- |
| 比赛名称 | `tournament.name` + `stage` |
| 双方与系列赛比分 | `home_team` / `away_team` / `home_score` / `away_score` |
| **每张地图的比分** | 详情接口的 **`maps`**（规范原文：*Per-map round scores for each map played*） |
| 开赛时间 / 赛制 / 状态 | `start_time`（UTC）/ `best_of` / `status` |

接口：

```
GET /csgo/api/v2/matches/?team=Team Spirit&date_from=2026-01-01&date_to=2026-12-31
GET /csgo/api/v2/matches/{id}/        # 详情里有 maps（逐地图）
Header: Authorization: Token <你的key>
```

### 为什么选它（对比其它来源）

| 来源 | 逐地图比分 | 获取 key | 结论 |
| --- | --- | --- | --- |
| **bzzoiro CS2 API** | ✅ `maps` | **免费，邮箱注册** | **采用** |
| Liquipedia LPDB v3 | ✅ `match2games` | 免费，但要加入他们的 **Discord** 申请 | 备选 |
| PandaScore | ❌ 免费档不含「比赛内的每一局」，需付费 Historical | 注册简单 | 排除 |
| TheSportsDB | ❌ 无 CS2 比赛数据（其电竞只覆盖 LoL / 火箭联盟） | 无需 key | 排除 |
| bo3.gg | ❌ `match_maps` 只有地图名/顺序，无比分；且无法按队伍列出赛程 | 无需 key | 排除 |
| HLTV 直连 / 社区镜像 | 有数据但页面 | —— | 被 Cloudflare 403 拦截（GitHub Actions 同样会被拦） |

### 申请 key（免费）

1. 打开 **https://sports.bzzoiro.com/register/**，用邮箱注册
2. 在个人页面拿到 API key
3. 在仓库 `Settings → Secrets and variables → Actions` 新增 Secret：**`BZZOIRO_API_KEY`**

## 可调配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BZZOIRO_API_KEY` | 无（**必填**） | API key |
| `TEAM_NAME` | `Team Spirit` | 队伍名（传给接口的 `team` 参数） |
| `YEAR` | `2026` | 只取该自然年的比赛 |
| `DETAIL_FETCH_MAX` | `300` | 最多为多少场已结束比赛补查逐地图比分 |
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
| `generate.py` | 拉取 bzzoiro 数据并输出 `matches.ics` |
| `validate_ics.py` | 校验 `matches.ics` 是否符合 iOS 导入要求（CI 在提交前运行） |
| `matches.ics` | 生成结果 |
| `.gitattributes` | 禁止 Git 对 `*.ics` 做行尾转换（RFC 5545 要求 CRLF） |

## 本地运行

```bash
pip install -r requirements.txt
export BZZOIRO_API_KEY=你的key      # Windows: set BZZOIRO_API_KEY=你的key
python generate.py
python validate_ics.py matches.ics
```

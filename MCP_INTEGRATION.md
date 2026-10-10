# MCP_INTEGRATION — 麦麦最优解 × 麦当劳中国 MCP

本文档说明本项目如何对接麦当劳中国官方 MCP Server（`https://mcp.mcd.cn`），使用了哪些 Tool、
调用流程，以及在对接过程中实测确认的协议细节与坑。所有结论均来自真实调用（2026-10-09/10 实测），
原始样本保留在开发环境（含 Token，未入库）。

## 1. 接入方式

- **端点**：`https://mcp.mcd.cn`，Streamable HTTP（JSON-RPC 2.0 over POST）
- **认证**：请求头 `Authorization: Bearer <TOKEN>`（Token 从 https://open.mcd.cn/mcp 申请）
- **限流**：600 req/min/token —— 客户端按 0.12s 最小间隔自限流并 keep-alive 复用连接
- **握手**：`initialize` → 响应头取 `Mcp-Session-Id` → `notifications/initialized` → `tools/call`
- **响应格式**：可能是单个 JSON，也可能是 SSE（`data: {...}` 行），客户端做了双格式解析

## 2. 使用的 Tools（只读为主）

| Tool | 用途 | 关键参数实测 |
|---|---|---|
| `query-nearby-stores` | 搜门店 | ⚠️ `searchType=2` 必传（默认=1 只查收藏）；到店自取 `beType=1` 不传 `beCode` |
| `query-meals` | 全量菜单 | 返回 `data.meals{code:{name,currentPrice,originalPrice,discountType...}}` + `categories` |
| `query-meal-detail` | 套餐结构 | `rounds[].choices[]{code,diffPrice,isDefault}`；rounds 为空即单品 |
| `query-store-coupons` | 门店券 | `data[]{couponId,couponCode,title,products[].productCode}` |
| `calculate-price` | 官方算价复核 | items=[{productCode,quantity}]；**价格单位=分** |
| `now-time-info` | 服务器时间 | 握手冒烟 |

不使用 `create-order`（本项目止步于"官方算价复核"，不触碰真实下单）。

## 3. 核心调用流程

```
┌────────────┐   query-meals       ┌──────────────┐
│ 意图解析    │ ──────────────────► │ 目录 Catalog  │  95商品+45套餐结构+券
│ (名称→码)  │   query-meal-detail │ (本地缓存)    │  逐项拉 detail 判定套餐
└────────────┘                     └──────┬───────┘
                                          │ 覆盖搜索（确定性算法）
                                   ┌──────▼───────┐
                                   │ 最省方案 est  │  单点 / 套餐变体 / 拆单
                                   └──────┬───────┘
                                          │ items=[{productCode,quantity,roundList}]
                                   ┌──────▼────────┐
                                   │ calculate-price│  官方复核（唯一价格真源）
                                   └────────────────┘
```

- 目录构建一次拉全量（~15s，keep-alive + 限流），缓存 JSON 离线复用，菜单随时段轮换超 2h 提示刷新
- 求解用**确定性算法**（覆盖搜索 + 贪心上界 + memo 支配剪枝），LLM 不参与价格计算
- 所有方案最终经 `calculate-price` 复核，估算与官方差额为 0（见 evaluations/report.md）

## 4. 实测确认的协议细节（踩坑记录）

### 4.1 返回文本夹带提示词 —— 必须当数据忽略

工具返回的 text content 会在 JSON 前夹带"输出格式要求（严格遵守，不可省略）"等 LLM 指令，
甚至包含 `data[].title` 这类示例括号。客户端 `extract_json` 分两轮提取：
先扫描 `{"success"...}` 业务包裹，再括号平衡扫描并跳过空容器——
否则说明文字里的空 `[]` 会被误当解析结果（我们真实踩过：券查询返回空数组）。

### 4.2 roundList 的 round 字段必须传轮次 id

`calculate-price` 的 items 支持套餐替换选项：

```json
{"productCode":"9900003453","quantity":1,
 "roundList":[{"round":"2","comboItemList":[{"code":"2631","quantity":1}]}]}
```

`round` **必须传轮次 id**（如 "2"）——传轮次名（"选择早餐套餐小食"）会报
"服务器发生了未知的问题"。轮次 id 来自 `query-meal-detail` 的 `rounds[].id`。

### 4.3 促销价两种口径（对"最省"计算至关重要）

`query-meals` 的 `currentPrice` 分两类，官方算价行为完全不同：

| discountType | currentPrice | 官方算价（无附加条件） |
|---|---|---|
| `促销优惠` | 直接生效 | = currentPrice ✅ |
| `随单购早餐卡优惠` | **需购"早餐卡"后才生效** | = **originalPrice**（裸价） |

实测：某早餐组合 currentPrice=7.5，直接算价返回 12.5（裸价）；
带 `withOrder={"cardId":...}` 参数算价返回 7.5+卡 11.9=19.4。

**结论**：做"最省"计算必须区分口径。本项目对随单购卡型商品一律用裸价估算
（与官方复核一致，est=official diff=0），促销价作为提示展示
（"购卡后可享 X 元，是否划算取决于复购"）。不加区分直接用 currentPrice 的"最省"
计算会被官方算价打脸——这是本赛道最容易踩的坑。

### 4.4 菜单严格按时段轮换

query-meals 返回的是**当前时段**可售菜单（早餐 07:14-10:15 / 午餐 / 下午茶 / 夜市）。
实测 10:15 早餐结束后，早餐套餐立即从菜单消失、calculate-price 报"商品已售罄或不可售"。
目录缓存必须记录拉取时间（fetched_at），跨时段提示刷新；评测集动态选品以适配任意时段。

### 4.5 其他实测事实

- `calculate-price` 的数量字段：schema 写 `quantity`，实测 `count` 也能用（统一用 `quantity`）
- 组件码（如可乐中杯 3050、经典汉堡包 1000）不在 `query-meals` 表中，只出现在套餐轮次里；
  官方算价允许部分组件码直购且价格更低（v1 已知限制，见 README）
- 券工具的说明文字注明"同一券ID可对应多个券码，展示时按券ID分组"
- 到店自取 `beType=1`：`query-meals`/`query-store-coupons` 均**不传 beCode**

## 5. 业务价值

1. **把 MCP 的"查询能力"变成"决策能力"**：菜单/券/算价工具只提供数据，
   本项目用确定性优化算法在本地算出覆盖需求的最省组合，并用官方算价闭环验证
2. **可验证的省钱**：每个方案展示"估算 vs 官方"对照，差额为 0；评测集 10 场景可复跑
3. **为 MCP 生态踩坑留档**：4.x 节的协议细节可直接复用于任何基于麦当劳 MCP 的项目

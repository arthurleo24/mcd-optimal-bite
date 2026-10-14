# 麦麦最优解（mcd-optimal-bite）

<p align="center">
  <img src="docs/images/cover.png" alt="麦麦最优解 — 说想吃啥，算出可验证的麦当劳最省点单方案" width="100%">
</p>

![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Tests](https://img.shields.io/badge/tests-63%20pass-brightgreen)
![Deps](https://img.shields.io/badge/dependencies-zero-success)
![Verify](https://img.shields.io/badge/官方复核-差额0-red)
![Contest](https://img.shields.io/badge/麦当劳程序员节-参赛作品-DA291C)

> 说想吃啥 + 几个人吃 → 算出**可验证**的麦当劳最省点单方案（套餐化 · 配券 · 拆单 · 官方算价复核）。

> 💡 **如果它帮你省到了钱，点个 Star ⭐ 让更多人看到** —— 这是对开源作者最好的回报（项目正在参加麦当劳程序员节大赛，星数决定排名）。

<p align="center">
  <a href="#30-秒上手">快速开始</a> ·
  <a href="SHOWCASE.md">4 个真实案例</a> ·
  <a href="MCP_INTEGRATION.md">MCP 接入实录</a> ·
  <a href="evaluations/report.md">评测报告</a>
</p>

基于麦当劳中国官方 MCP（`https://mcp.mcd.cn`）的本地优化引擎。**LLM 只做意图解析，价格计算全部由确定性算法完成，并经官方 `calculate-price` 闭环复核**——每个方案的"估算价"与官方实付价差额为 0。

```text
══════════════════════════════════════════════
 麦麦最优解
══════════════════════════════════════════════
需求: 脆薯饼×2、吉士蛋麦满分×2（2人）
目录: 缓存 2026-10-10T09:57:44+08:00（95商品/45套餐/1券）

── 最省方案 ──
1. [套餐] 吉士蛋麦满分套餐（含替换选项）  ¥15
   含: 可乐中杯×1 脆薯饼×1 吉士蛋麦满分×1
2. [套餐] 吉士蛋麦满分套餐（含替换选项）  ¥15
   含: 可乐中杯×1 脆薯饼×1 吉士蛋麦满分×1
估算合计 ¥30   | 官方算价 ¥30   | 比全单点省 ¥6

── 提示 ──
吉士蛋麦满分套餐 官方裸价 ¥17；购「随单购早餐卡」后可享 ¥10.2
（卡价以官方为准，是否划算取决于复购频次）
══════════════════════════════════════════════
```

## 30 秒上手

```bash
# 1. 申请 Token：https://open.mcd.cn/mcp （手机号登录 → 控制台 → 复制 Token）
# 2. 跑起来
git clone https://github.com/arthurle24/mcd-optimal-bite.git
cd mcd-optimal-bite
pip install -e .                      # 零第三方依赖，仅标准库
export MCD_MCP_TOKEN="你的Token"      # Windows: set MCD_MCP_TOKEN=xxx

# 3. 搜门店
mcd-optimal-bite stores --city 苏州市 --keyword 园区

# 4. 算最省方案（首次自动拉取目录 ~15s，之后缓存秒出）
mcd-optimal-bite order --store 1990526 --items "吉士蛋麦满分×2,脆薯饼×2" --people 2
```

items 语法：`商品名×数量`，逗号分隔；支持精确名/唯一子串/套餐组件名；歧义词会列出候选。

## 一句话，交给你的 Agent

复制这一段给 WorkBuddy、Codex 或任何具备 Skill + MCP 能力的 Agent：

```text
请从 https://github.com/arthurleo24/mcd-optimal-bite 安装「麦麦最优解」Skill：
按 SKILL.md 的流程连接麦当劳官方 MCP，然后帮我算：2 个人想吃麦辣鸡腿堡和中薯条，怎么点最省？
```

或在 WorkBuddy 中将本仓库 `SKILL.md` 安装为技能（用户级：`~/.workbuddy/skills/mcd-optimal-bite/`）。

## 为什么这个"最省"可信

**1. 确定性算法，不是 LLM 拍脑袋。**
求解器把"单点 / 套餐默认 / 套餐替换变体 / 按券拆单"建成覆盖搜索问题，
贪心上界 + memo 支配剪枝，8 项需求 9ms 出最优解（评测 E1-E9）。

**2. 官方算价闭环复核。**
每个方案翻译成 `calculate-price` 请求（套餐替换选项通过 `roundList` 精确表达），
展示"估算 vs 官方"对照。**实测差额 0**（evaluations/report.md，可复跑）。

**3. 促销双口径免疫。**
我们实测发现 `currentPrice` 有两种口径：普通"促销优惠"官方直接生效；而
"随单购早餐卡优惠"型（如 ¥7.5 的早餐组合）**必须买 ¥11.9 的卡才生效**，
官方裸算价按 originalPrice（¥12.5）计。不区分口径的"最省"计算会被官方算价打脸。
本项目对卡促销商品一律用裸价估算，促销价作为提示展示——复核差额依然为 0。

**4. 评测集与 63 个单元测试。**
`evaluations/run_eval.py` 覆盖单品/多品/多人/仅套餐组件/大需求/不可得降级/
名称歧义/按券拆单/端到端性能 10 个场景，菜单随时段轮换下动态选品、任何时段可复跑。

## 与同类方案的区别

| 能力 | 本项目 | 常见实现 |
|---|---|---|
| 最省组合 | 本地确定性优化（可复现、可验证） | LLM 现场调工具拼凑，结果不可复现 |
| 价格口径 | 区分促销双口径，裸价估算 | 直接用 currentPrice，遇早餐卡类促销必错 |
| 结果可信度 | 官方 calculate-price 复核，差额 0 | 无复核，以菜单展示价为准 |
| 按券拆单 | 拆单结构 + 每单带券官方复核对比 | 多数止步于"提示有券" |
| 依赖 | 仅 Python 标准库 | 不等 |
| 测试 | 63 单测 + 10 场景评测 | 少 |

## 它是怎么工作的

```mermaid
flowchart LR
    A["意图解析<br/>LLM 只做这一步<br/>名称→商品码"] --> B["确定性求解<br/>覆盖搜索：单点/套餐变体/拆单<br/>贪心上界+memo剪枝 · 9ms"]
    B --> C["官方复核<br/>calculate-price<br/>估算 vs 官方 差额=0"]
    C --> D["方案输出<br/>比单点省 ¥X · 按券拆单 · 促销提示"]
```

核心发现沉淀在 [MCP_INTEGRATION.md](MCP_INTEGRATION.md)：
roundList 必须传轮次 id、返回文本夹带提示词的剥离、菜单按时段轮换、
券说明文字里的示例括号会让朴素解析器返回空数组等——全部可复用。

## 已知限制（诚实清单）

- 「仅套餐组件」类商品（不在菜单表）由套餐覆盖供给；官方算价显示部分组件码其实可直购且更便宜，
  v1 目录未含组件单价（评测 E4 以 WARN 如实标注），v2 计划扩展组件价目
- v1 不调用 `create-order`，止步于官方算价复核；下单闭环在路线图（带 `--confirm` 防误触）
- 未覆盖积分抵扣、外卖渠道比价（v2 路线图）

## Roadmap

- [x] v1.0：目录缓存 / 覆盖搜索 / 官方复核 / 按券拆单 / CLI / 评测集
- [ ] 下单闭环（`create-order` + `--confirm` 显式确认）
- [ ] 组件码价目扩展（消除 E4 WARN）
- [ ] 营养热量双约束（热量上限 + 预算上限）
- [ ] 积分影子价与渠道比价（自提/麦乐送/第三方平台）

## 目标用户

- 经常点麦当劳、想少花钱的学生 / 上班族
- 帮部门/家庭凑单的团餐负责人（多人拆单场景）
- 想把麦当劳 MCP 接进 Agent 工作流的开发者（本仓库同时提供 WorkBuddy Skill 形态，见 SKILL.md）

## 许可与声明

MIT。本项目为麦当劳程序员节创意开发大赛参赛作品，非麦当劳官方产品；
餐品信息、价格及供应状态以麦当劳官方渠道实时结果为准（见 CONTEST_DECLARATION.md）。

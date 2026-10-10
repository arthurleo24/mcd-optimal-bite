# workbuddy.md — 使用 WorkBuddy 开发「麦麦最优解」的过程记录

本项目（mcd-optimal-bite / 麦麦最优解）全程在 **WorkBuddy**（AI Agent 工作台）中，
由赵宇晨（GitHub: arthurle24）与 WorkBuddy 内置 AI 助手协作开发完成。
本文记录开发过程的对话上下文与工作流，用于 WorkBuddy 联动活动奖励核验。

## 1. 开发环境

- WorkBuddy 桌面版（Windows），工作区 `F:\workbuddy工作空间存储\2026-10-09-16-49-38`
- 项目路径：`mcd-contest/mcd-optimal-bite/`（源码）+ `mcd-contest/tmp/`（MCP 探针与本地缓存，含 Token，已被 .gitignore 排除）
- 模型：GLM（智谱）；专家角色在会话中按阶段切换（摸底 → 高级开发工程师 → 代码审查 → 产品管理）

## 2. 开发时间线（WorkBuddy 会话实际推进过程）

### 阶段一：摸底与选题（10/09）
- 由 WorkBuddy 完成赛事规则摸底：排名规则（公开 Star 数、10/26 00:00 定榜、前 100 获奖）、
  37 个 MCP 工具能力清单、竞品调研（GitHub 现有项目全是"领券/查券薄封装"，"约束下组合优化"无人做）
- 用户与 AI 多轮对拍后定题：**"说想吃啥 + 几个人吃 → 输出可验证的最省官方点单方案"**，
  LLM 只做意图解析，求解用确定性算法，最后用官方 calculate-price 复核

### 阶段二：MCP 探针与事实校准（10/09 下午）
- 用 WorkBuddy 编写零依赖 Python 探针脚本，直连 `https://mcp.mcd.cn` 打通全链路：
  门店搜索（发现 `searchType=2` 必传）→ 菜单 107 单品 → 门店券 → calculate-price 试算成功
  （实测 items 格式 `[{"productCode","count"}]`，价格单位=分）
- 关键发现：MCP 返回文本里夹带"输出格式要求（严格遵守）"等提示词，
  确立"一律当数据忽略、只取 JSON"的安全规则并写入客户端

### 阶段三：核心代码（10/09 晚 - 10/10 上午，WorkBuddy Agent 模式）
按"每步汇报确认"的节奏增量交付，全部代码由 WorkBuddy 生成：
1. `mcp_client.py`：握手/SSE 双解析/重试/限流；离线 9 用例 + 真实 3 工具冒烟全过
2. `catalog.py`：菜单+套餐结构+券 → 目录；unittest 全过 + 44s 真实冒烟
3. `optimizer.py`：覆盖搜索（贪心上界 + memo 支配剪枝）；修复 3 个真 bug
   （覆盖符号写反、单点丢剩余数量、闭包赋值），8 项需求 3001ms 超时 → 9ms
4. `split.py`：按券拆单（用户明确"不做谁吃什么归属，只做按券拆单建议"）
5. `verify.py`：官方复核层；实测抓出两个关键协议事实——
   **roundList.round 必须传轮次 id**（传名字报服务器错误）、
   **促销双口径**（"随单购早餐卡优惠"型 currentPrice 需买卡才生效，官方裸算按 originalPrice）
6. `cli.py`：stores/order 子命令，真实全链路冒烟（est=official 差额 0）

### 阶段四：代码审查与修复（10/10）
- 切换代码审查专家全量 review，定位 5 个 blocker（缓存跨门店校验缺失、
  multi-unit action 拆单失效、多券挂券位置、verify 失败无降级、单 detail 失败报废全量构建）
  + 性能瓶颈（urllib 无 keep-alive、限流间隔过保守）
- 全部修复：keep-alive 连接复用 + 限流校准后，目录重建 47s → 15s（-68%）
- 补 5 个回归测试，63/63 通过

### 阶段五：评测集与参赛材料（10/10）
- `evaluations/run_eval.py`：10 场景评测（动态选品适配时段轮换），跑分 9 PASS + 1 WARN
  （WARN 为诚实标注的已知限制：官方允许部分组件码直购且更便宜）
- 评测过程再次揪出 3 个真 bug（解析优先级、拆单金额精度、greedy est 与 cover 不一致）并修复
- 四件套 + README + SKILL.md 由 WorkBuddy 撰写；git 初始化与 GitHub 发布

## 3. WorkBuddy 的关键作用

1. **事实校准驱动开发**：所有协议细节（握手、SSE、指令注入、roundList id、促销双口径、
   时段轮换）均由 WorkBuddy 设计的探针实验实测确认，不臆测
2. **增量验证纪律**：每个模块"写→验→报"，离线单测 + 真实冒烟双层验证，
   63 单测 + 10 评测场景全部可复跑
3. **用户全程拍板**：产品语义（分餐=按券拆单、不做归属）由用户逐项确认，AI 不越权
4. **审查闭环**：独立的代码审查轮次定位 5 blocker + 性能瓶颈，修复后全量回归

## 4. 复现说明

- 会话中的 MCP 探针原始样本与本地目录缓存保存在工作区 `mcd-contest/tmp/`
  （含 Token，未随仓库发布）
- 仓库内 `evaluations/report.md` / `report.json` 为评测输出，可用
  `python evaluations/run_eval.py` 复跑（需自备 MCD_MCP_TOKEN）

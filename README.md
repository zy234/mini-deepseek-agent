# Mini DeepSeek Agent

这是一个围绕 DeepSeek 构建的小型 Agent：模型固定为 `deepseek-v4-flash`，不同角色可以配置独立的中文 prompt、工具集合和执行流程。宿主提供 Bash、文件编辑、网页证据、确定性金融计算和受限 MiniQMT 接口。

## 安装

```bash
python3 -m pip install -e .
```

设置 DeepSeek Key：

```bash
export DS_KEY="your-key"
```

## 运行

```bash
mini
```

交互终端会先显示 Agent 列表，选择角色后再输入任务。也可以显式指定角色，适合脚本和非交互运行：

```bash
mini --agent interactive -t "修复当前项目中的测试失败，并运行相关测试"
```

默认工作目录是启动命令所在目录。常用参数：

```bash
mini --agent single_call
mini --config path/to/agent.yaml
mini --output trajectory.json
mini --step-limit 20
mini --timeout 60
```

角色定义保存在 `src/minisweagent/config/deepseek.yaml` 的 `agents` 节点。每个角色可以配置自己的 prompt、工具和执行流程；新增同类角色只需增加配置。

当前内置角色：

- `interactive`：通用交互角色，可持续对话并按需使用工具。
- `single_call`：单次调用角色，不使用工具。
- `financial_research`：查询行情、核验网页证据并执行确定性金融计算，不访问账户。
- `portfolio_manager`：只读查询个人账户、行情和组合风险，不执行交易。
- `account_trader`：查询个人账户和行情，按宿主安全配置提交或撤销委托。
- `financial_manager`：自主账户管理主 Agent，通过固定金融子 Agent 完成观察、研究和交易，并维护每日账本。

账户管理主 Agent 用法：

```bash
mini --agent financial_manager -t "先查看我的账户和持仓，再分析风险；不要下单"
```

主 Agent 不直接接触 MiniQMT 工具。宿主只允许委派到固定角色，每次运行最多调用 4 次；子 Agent 使用独立上下文且不能继续委派。账户管理周期的交接顺序固定为 `financial_research -> portfolio_manager -> account_trader`：研究角色以昨日收盘为基准产出今日 `buy_candidates`，组合经理结合账户形成 `selected/rejected` 和 `order_plan`，交易角色只对组合方案做 `risk_check` 和下单判断。宿主会阻止跳过研究或组合阶段的委派，交易权限和风险规则仍由 `miniqmt_trade` 工具内部强制执行。

研究候选不是订单。`financial_research` 必须给出 `research_as_of`、`previous_close_as_of`、`data_cutoff`、`data_sufficiency`、`buy_candidates` 和缺失数据；`portfolio_manager` 必须说明每个候选为何选入、缩减或拒绝，并将现金、集中度、T+1 和未完成委托纳入取舍；只有完整的 `order_plan` 才能交给 `account_trader`。任何数据不足、风险检查失败或 `unknown` 结果都会降级为 HOLD 并写入每日账本。

自主账户循环使用下面的显式入口。交易日 09:20 创建盘前上下文，由研究 Agent 结合当前行情、新闻、昨日收盘和账户持仓生成候选，组合经理确认取舍后通过 `account_monitor` 写入显式监控计划。09:30 至 11:30、13:00 至 15:00 由宿主轮询计划；触发买卖点时只创建 `account_trader` 上下文做当前行情和账户风控，不重新研究。12:50 追加一次午盘前复核，重新检查上午行情和新闻并更新下午计划。交易 Agent 可在成交或拒绝后更新对应监控计划。15:10 自动创建新的只读上下文完成收盘复盘并清理已失效计划。

```bash
mini --account-loop
mini --account-day
mini --close-review
```

`--account-day` 运行一个交易日后退出，适合由 macOS `launchd` 在开盘日 09:20 启动；`--account-loop` 适合常驻服务，二者都使用相同的盘前、午盘前、盘中监控和收盘复盘流程。

在 macOS 上安装工作日自动任务（任务会从当前工作目录 `.env` 读取 `DS_KEY`、MiniQMT Bridge 和账户配置）：

```bash
mini --install-account-schedule
```

该任务每天 09:20 启动 `--account-day`，12:50 执行午盘前复核，15:10 复盘后退出；日志位于 `.sessions/account-manager/logs/`。卸载可执行 `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.minisweagent.account-day.plist`。

循环入口使用 `auto_execute`，不请求逐笔人工审批。重复启动会被状态目录中的运行锁拒绝。需要立即停止所有写操作时设置 `MINIQMT_KILL_SWITCH=1`；状态目录可通过 `MINIQMT_AGENT_STATE_DIR` 调整。

项目提供 Bash、文件编辑、网页搜索和网页抓取能力。运行环境直接使用当前用户权限，请勿在不可信目录或任务中运行。

网页正文默认先走轻量 HTTP 抓取；正文缺失、软拦截或质量不足时，可选用 Playwright 渲染动态页面：

```bash
python3 -m pip install -e '.[browser]'
python3 -m playwright install chromium
```

设置 `MSWEA_WEB_FETCH_BROWSER=0` 可关闭浏览器降级。模拟运行由宿主设置带时区的
`MSWEA_WEB_AS_OF`（例如 `2026-09-03T10:30:00+08:00`）；已知晚于截止时间的搜索结果会被删除，时间不明的搜索候选只返回无标题、无摘要的 URL 供 `web_fetch` 核验。正文只有在发布时间明确且不晚于截止时间时才返回，只有日期而没有盘中时刻的当天文章也会被拒绝，避免未来信息进入 Agent。

MiniQMT 工具直接连接 Bridge，默认地址为 `http://127.0.0.1:8023`（可用 `MINIQMT_BRIDGE_URL` 修改），个人账户由宿主环境绑定：

账户收盘复盘生成的 `observation-todo.md` 是给用户查看的提醒，不会注入 Agent 的 `account_journal` 工具结果，也不会被当作任务指令。可用下面命令查看：

```bash
mini --show-observation-todo
```

```bash
export MINIQMT_ACCOUNT_ID="your-account-id"
export MINIQMT_BRIDGE_API_KEY="your-api-key"
export MINIQMT_AGENT_MODE="observe"         # 默认值，禁止提交和撤单
mini --agent portfolio_manager
```

直接运行 `account_trader` 时，`MINIQMT_AGENT_MODE=observe` 禁止交易，`execute` 保留逐笔终端审批，`auto_execute` 则免审批但仍执行全部宿主规则。账户循环会显式使用 `auto_execute`。

默认安全限制如下，可由宿主环境变量进一步收紧：

- `MINIQMT_MAX_ORDER_VOLUME=10000`：所有订单的绝对股数上限。
- `MINIQMT_MAX_BUY_VOLUME` / `MINIQMT_MAX_SELL_VOLUME`：买卖方向股数上限，默认继承绝对上限。
- `MINIQMT_MAX_BUY_NOTIONAL=20000`：单笔买入金额上限；买入必须使用固定限价。
- `MINIQMT_MAX_DAILY_BUY_NOTIONAL=50000`：单日累计买入金额上限。
- `MINIQMT_MAX_ORDERS_PER_CYCLE=2` / `MINIQMT_MAX_ORDERS_PER_DAY=8`：写操作次数上限。
- `MINIQMT_MIN_CASH_RATIO=0.10`：买入后的最低现金比例。
- `MINIQMT_MAX_QUOTE_AGE_SECONDS=30` / `MINIQMT_MAX_PRICE_DEVIATION_BPS=50`：行情新鲜度和限价偏离上限。

卖出前工具会重新查询持仓成本、可卖数量和最新价。盈利时允许卖出；浮亏小于 10% 时禁止卖出；浮亏达到或超过 10% 时作为止损允许卖出。字段缺失、非交易时段、账户或行情异常、重复意图、次数超限及 `unknown` 状态都会阻断后续提交。意图和工具结果持久化在状态目录中，清空上下文或重启进程不会解除冻结。

账户号和 API key 不进入模型参数、账本、会话记录或 Bash 子进程；`accepted` 只表示接口接受委托，不表示成交。提交结果为 `unknown` 时禁止自动重试，必须先查询委托和成交。

金融 Agent 调研记录见 [`docs/financial-agents/`](docs/financial-agents/README.md)，其中每个候选仓库都有独立的架构分析、许可证/依赖注意事项和迁移实现建议。

## 架构

```text
CLI
 ├── 读取角色配置并选择 Agent
 └── Agent
     ├── Flow
     │   ├── interactive：模型 ↔ 工具 ↔ 观察，循环执行
     │   └── single_call：模型单次生成结果
     ├── DeepSeek Model：负责模型请求和响应解析
     └── Local Environment：负责工具执行和结果反馈
         └── financial_manager -> agent_call -> financial_research / portfolio_manager / account_trader
```

角色配置决定 prompt、可用工具和 Flow；模型负责决策，环境负责执行，Agent 负责维护对话和流程状态。

## API

- [DeepSeek API](https://api-docs.deepseek.com/)
- [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion)
- [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls)

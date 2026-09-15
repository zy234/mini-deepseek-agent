# DeepSeek 视觉交易 Agent

这是一个围绕 DeepSeek 构建的小型 Agent 框架，主体是一条按交易日运行的三阶段视觉交易流水线：宿主取数、渲染 K 线图、模型看图给结论、宿主校验后执行。模型固定为 `deepseek-flash`（支持图片输入），角色的 prompt、工具和执行流程都在配置里。

## 安装

```bash
python3 -m pip install -e .
# 按 .env.example 建立项目 .env，并填写 DS_KEY、Bridge 和账户信息
```

## 三阶段流水线

| 阶段 | 时刻 | 角色 | 输入 | 输出 |
| --- | --- | --- | --- | --- |
| 一 · 盘前选池 | 09:20 | `candidate_scout` | 板块热度榜、热门板块内部个股结构、大盘、账户、昨日账本 | 5 个板块各 2 只票的待观测清单 |
| 二 · 盘中读图 | 09:30–15:00 每 10 分钟 | `chart_reader` × 组数（并行） | 每只票的 30 日日线图和当日分钟图、大盘两张图、实时行情与趋势字段 | 每只票一条 BUY/SELL/HOLD 结论 |
| 三 · 汇总执行 | 每轮读图之后 | `execution_manager` | 本轮全部结论、账户快照、当日委托成交、交易账本、硬限额 | 实际提交的委托 + 账本记录 |

所有能自动获取的信息都由宿主取好注入 prompt，模型不再自己调工具取数：取数是确定性动作，交给模型决定取什么只会引入"忘了取"、"取错时间窗"和"上下文被整版行情淹掉"三类故障。阶段一在 09:20 跑，此时集合竞价已开始撮合，但日线还没有当日 bar，所以个股结构字段都是昨收基准——prompt 里明确写着这一点。

模型只能从注入的池子里挑代码：板块名必须与热度榜一致，个股必须出现在该板块注入的行情行里且 `buyable=true`，读图结论必须每只票恰好一条。校验不过就把原因回传给模型重来，两次不过这一步作废。

```bash
mini --trading-day       # 跑完整交易日：补跑盘前，盘中按 10 分钟推进，收盘退出
mini --premarket         # 只跑阶段一，写出当日待观测清单
mini --round             # 只跑一轮阶段二 + 阶段三
mini --miniqmt-mode auto_execute --trading-day   # 显式打开真实下单
```

`MINIQMT_AGENT_MODE` 在项目 `.env` 中明确设置：`observe` 只观察，`execute` 或 `auto_execute` 才允许真实交易。同一状态目录只允许一个交易进程，重复启动会被运行锁拒绝；需要立即停止所有写操作时把 `.env` 中的 `MINIQMT_KILL_SWITCH` 改为 `1`。

在 macOS 上安装工作日自动任务（从当前工作目录 `.env` 读取 `DS_KEY`、MiniQMT Bridge 和账户配置）：

```bash
mini --install-schedule
```

任务每个工作日 09:15 启动 `--trading-day`，收盘后退出；日志在 `.sessions/account-manager/logs/`。卸载执行 `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.minisweagent.trading-day.plist`。

流水线节奏和规模在 `deepseek.yaml` 的 `trading` 段：盘前时刻、轮次间隔、板块数、每板块只数、扫描板块数、大盘指数代码、日线根数、并行组数。待观测清单落在 `<状态目录>/watchlist/<日期>.json`，图落在 `.sessions/<日期>/charts/<轮次>/`。

## 单角色会话

```bash
mini                                    # 交互终端先列角色
mini --agent interactive -t "修复测试失败并运行相关测试"
mini --config path/to/agent.yaml --output trajectory.json --step-limit 20 --timeout 60
```

内置角色：

- `interactive`：通用交互角色，有 `bash`、`str_replace_editor`、`web_search`、`web_fetch`。
- `candidate_scout` / `chart_reader`：无工具、只输出 JSON，输入全部由宿主注入，只能由流水线驱动。
- `execution_manager`：有 `miniqmt_trade`、`miniqmt_account`、`account_journal`，是唯一能动账户的角色。

## 观测与调参

```bash
mini-inspect
```

只绑定 127.0.0.1 的单页服务：会话索引、一轮下面并行读图子会话的调度树、执行路径瀑布、模型当时看到的每一张图、token 用量；配置页可增删角色、勾工具、开 `json_output`、编辑两个 prompt，保存前跑一遍整份自检，不通过一个字节都不落盘；右下角可以直接试跑 `premarket` 或 `round` 并看日志。

## MiniQMT 与交易安全

所有运行参数都必须写在项目 `.env` 中；代码不再为交易参数静默补默认值。`miniqmt.host_limits` 是唯一定义，交易工具和 prompt 读的是同一份：

- `MINIQMT_MAX_ORDER_VOLUME` / `MINIQMT_MAX_BUY_VOLUME` / `MINIQMT_MAX_SELL_VOLUME`：所有订单及买卖方向的股数上限。
- `MINIQMT_MAX_BUY_NOTIONAL`：单笔买入金额上限。
- `MINIQMT_MAX_DAILY_BUY_NOTIONAL`：单日累计买入金额上限。
- `MINIQMT_MAX_ORDERS_PER_CYCLE`：本轮写操作次数上限；不设置每日委托笔数上限。
- `MINIQMT_MIN_ORDER_NOTIONAL`：每笔买卖委托金额下限，单位为元。
- `MINIQMT_MIN_CASH_RATIO`：买入后的最低现金比例。
- `MINIQMT_MAX_QUOTE_AGE_SECONDS` / `MINIQMT_MAX_PRICE_DEVIATION_BPS`：行情新鲜度和限价偏离上限。

买入限价不由调用方给定：`miniqmt_trade` 的买入只接受 `price_cap`（追高上限），宿主在提交那一刻按最新价推导实际限价——上浮偏离额度的 80% 保证吃得到卖盘，向下取整到 0.01 元报价网格，并且不越过 `price_cap`。模型自己算固定限价必然踩坑：价格一漂移就撞破 50bp 偏离上限被拒。

推导不出**严格高于**最新价的限价时直接拒单，两种原因分开报告：最新价已顶到 `price_cap` 是跳空追高；报价单位装不下上浮额度是低价股的物理限制（默认参数下约 2.5 元以下）。宿主不会退而报一个等于最新价的限价——那只会留下不成交的悬单，让人以为没买到而账户里多一笔在途委托。

买入还会核验单笔金额、买入后现金下限，并拒绝科创板 688/689（账户无该权限）。卖出重新查询持仓成本和可卖数量（`can_use_volume` 就是 T+1 的事实来源），只校验可卖数量：短线趋势策略要求小亏就走。字段缺失、非交易时段、账户或行情异常、重复意图、次数超限及 `unknown` 状态都会阻断提交。意图和结果持久化在状态目录，重启进程不会解除冻结。

账户号和 API key 不进入模型参数、账本、会话记录或 Bash 子进程；`accepted` 只表示接口接受委托，不表示成交。提交结果为 `unknown` 时禁止自动重试，必须先查询委托和成交。

## 通用能力与风险

Bash 和文件编辑直接使用当前用户权限，不是沙箱，请勿在不可信目录或任务中运行。高风险命令执行前会触发终端审批。

网页正文默认走轻量 HTTP 抓取，质量不足时可选用 Playwright 渲染：

```bash
python3 -m pip install -e '.[browser]'
python3 -m playwright install chromium
```

设置 `MSWEA_WEB_FETCH_BROWSER=0` 关闭浏览器降级。

金融 Agent 调研记录见 [`docs/financial-agents/`](docs/financial-agents/README.md)。

## 架构

```text
mini --trading-day
 └── TradingPipeline（宿主编排，唯一的调度者）
     ├── 阶段一 candidate_scout      ← context.premarket_context（板块榜 / 个股结构 / 账户 / 账本）
     ├── 阶段二 chart_reader × N     ← context.round_context + charts.render_*（并行，每组一次带图请求）
     └── 阶段三 execution_manager    ← 全部结论 + 账户 + 账本 + 限额，工具：miniqmt_trade / miniqmt_account / account_journal
```

角色配置决定 prompt、可用工具和 flow；模型负责判断，环境负责执行，流水线负责取数、校验和落盘。

## API

- [DeepSeek API](https://api-docs.deepseek.com/)
- [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion)
- [图像理解](https://api-docs.deepseek.com/guides/vision/)
- [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls)

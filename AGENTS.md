# DeepSeek 视觉交易 Agent

本仓库有意实现一个小型 Agent 框架，主体是一条按交易日运行的三阶段视觉交易流水线：

- 模型：通过 DeepSeek 兼容 OpenAI 的 Chat Completions API 调用 `deepseek-flash`（平台只认这个名字，`deepseek-v4.1-flash` 这类写法直接 400）。它支持图片输入，K 线图就是靠这个通道给模型看的。
- 流水线：阶段一盘前选池（09:20），阶段二盘中每 10 分钟按板块分组并行读图，阶段三汇总执行。行情、账户、账本全部由宿主取好注入 prompt，模型不再自己调工具取数。
- 工具：宿主只给执行阶段留 `miniqmt_trade`、`miniqmt_account` 和 `account_journal`；通用 `interactive` 角色另有 `bash`、`str_replace_editor`、`web_search`、`web_fetch`。
- 环境：只执行本地子进程和工作区内的文件编辑，加上宿主绑定的 MiniQMT Bridge。
- CLI：一个 `mini` 入口和一个 YAML 配置文件；`mini-inspect` 提供轨迹观测与角色配置。

除非有明确的具体需求，否则不要增加 provider 抽象、动态类加载、基准测试运行器、容器后端、Agent 之间的委派机制或其他模型 API。

## 目录结构

```text
src/minisweagent/agents/default.py              Agent 循环和限制
src/minisweagent/agents/single_shot.py          一次模型调用的角色流程
src/minisweagent/models/deepseek_model.py       DeepSeek API 适配器，含图片与 JSON 输出
src/minisweagent/models/utils/actions_toolcall.py  工具定义、参数校验和观测消息
src/minisweagent/environments/local.py          本地命令执行和工具分发
src/minisweagent/environments/editor.py         工作区内文本编辑和原子写入
src/minisweagent/environments/miniqmt.py        MiniQMT Bridge 客户端、交易安全规则和硬限额
src/minisweagent/environments/account_journal.py 每日追加式交易账本
src/minisweagent/trading/pipeline.py            三阶段编排、校验与交易日循环
src/minisweagent/trading/context.py             宿主侧取数、账户/账本装配和注入排版
src/minisweagent/trading/charts.py              日线与分钟线渲染成 PNG
src/minisweagent/trading/notify.py              交易日四个汇报点推送企业微信自建应用（markdown），旁路通道，未配置即跳过
src/minisweagent/backtest/replay.py             历史行情重放：按时刻截断、指标与图的口径复用实盘函数
src/minisweagent/backtest/simulate.py           纸面账户：固定成交规则、T+1、费率与硬限额
src/minisweagent/backtest/evaluate.py           结论的前向收益评估（MFE/MAE）与回测摘要
src/minisweagent/backtest/runner.py             回测编排：槽位循环、报告落盘，复用流水线装配与校验
src/minisweagent/run/mini.py                    CLI：单角色会话、交易日入口与回测入口
src/minisweagent/run/inspect.py                 轨迹观测与角色配置服务：会话索引、并行子会话、执行路径、图片、配置与 prompt 读写
src/minisweagent/run/inspect_ui.html            观测与配置前端单页，无构建、无外部依赖
src/minisweagent/utils/cli_display.py           CLI 分段、颜色和摘要展示
src/minisweagent/config/deepseek.yaml           角色装配、流水线参数和运行时默认配置
src/minisweagent/config/prompts/                各角色 prompt，按 `<role>.system.md` 和 `<role>.instance.md` 存放
tests/test_core.py                              核心功能测试
```

## 流水线约定

- 三个阶段角色的名字写死在宿主调度里：`candidate_scout`（single_shot + json_output）、`chart_reader`（single_shot + json_output）、`execution_manager`（iterative + `miniqmt_trade`）。这三条硬形态由 `config/PIPELINE_ROLES` 在写盘前校验，改坏了不会报错只会安静跑偏。
- 阶段之间只由宿主传递结构化数据，没有任何 Agent 能调度另一个 Agent。阶段二各组之间没有共享状态，所以直接用线程并行；一组失败不打死整轮，但失败必须跟着数据进阶段三的输入。
- 并行读图组的板块名通过 `session_label` 落进子轨迹的 `info.session`，观测端靠这个字段区分组；观测端不许去解析任务文本猜名字，那是把显示绑死在 prompt 措辞上。
- 模型给出的股票代码必须落在宿主注入的池子里，`_validate_watchlist` 和 `_validate_verdicts` 会逐条核对。校验不过就把原因回传给模型重来（默认 2 次），凭记忆编出来的代码会让账户买到完全无关的票。
- 取数失败进 `errors` 并注入 prompt，让模型知道自己在残缺数据上判断；账户快照取不到则整轮失败——没有可用资金和可卖数量，任何交易判断都是猜的。
- 图片只以路径存在轨迹里，`_api_messages` 在发请求那一刻才读成 base64。轨迹要能反复读、被观测端加载，塞进几 MB base64 会让它变成不可读的文件。
- 图上一律不写中文：mac 默认字体没有中文字形，缺字渲染成方块且不会报错。中文说明写在 prompt 里。
- 交易硬限额只有一份定义（`miniqmt.host_limits`），交易工具照它拦单、prompt 照它注入；两处各读一遍环境变量必然漂移，模型就会按一套限额做计划、撞上另一套被拒。

## 回测约定

- 回测只重放阶段二：板块热度榜读的是实时 tick，历史重放不了；execution_manager 需要真实账户和交易工具，也不回放。回测回答两个问题：读图结论在前向走势上准不准，以及"结论 × 固定仓位规则"一天下来赚不赚钱。
- 回测的口径必须和实盘同一份代码：行情行、趋势字段、时段进度走 `miniqmt` 的现成函数，图走 `charts`/`context._render_pair`，取数走 `context._bars`，读图与校验复用 `TradingPipeline` 的装配。回测自己另写一套指标，就是在回测一个不存在的策略。
- 成交价一律用决策时刻最后一根分钟 bar 的收盘价（模型看图时的事实），不引入滑点猜测；佣金和印花税是明说的假设，常量在 `backtest/simulate.py`。
- 标的来自该日已落盘的待观测清单（`journal_dir/watchlist/<date>.json`）或 `--codes` 显式指定；当日买入 T+1 不可卖，成交与跳过都必须留痕进报告。
- 回测环境锁死 observe，不存在任何真实下单路径；报告落在 `journal_dir/backtest/`，原子写入。

## 开发约定

- 目标 Python 版本为 3.10 或更高，并使用类型注解。
- 优先采用显式构造，不要增加工厂或兼容性垫片。
- 配置保存在 `deepseek.yaml`，角色 prompt 单独放在 `config/prompts/`；改 Agent 行为改 prompt，改安全和硬约束改工具层。密钥从 `DS_KEY` 读取。
- 微信通知是交易主路径外的旁路：走企业微信自建应用（官方通道，敏感财务数据不过第三方），凭证 `WECOM_CORPID`、`WECOM_CORPSECRET`、`WECOM_AGENTID`、可选 `WECOM_TOUSER` 只进 `.env`，缺任一 `notify.enabled()` 即为假，宿主跳过全部取数与排版；access_token 进程内缓存并在失效时刷新重试一次；推送失败只回显加日志，绝不 raise 打死交易日。消息类型用 markdown（在企业微信 App 里看），content 硬上限 2048 字节先本地按字节裁。汇报内容直接用流水线里已产生的模型输出（候选 reason、执行 submission、复盘报告），不额外发模型请求。
- 永远不要序列化或记录 `DS_KEY`。
- GitHub API Token 由用户在 `~/.zshrc` 中导出为 `GITHUB_TOKEN`；非交互命令创建 PR 时需通过交互 `zsh` 加载，但不得打印、序列化或记录其值。
- 代码注释应用中文，清楚的解释为什么这么开发。
- 使用 `pytest` 编写测试，使用 `ruff` 做静态检查。只写大功能的端到端测试，不写零散单元测试。流水线的端到端测试用真实配置和真实 prompt 渲染，少注入一个模板变量会当场炸掉。
- 模型请求使用 mock client 测试；只有明确的 smoke test 才允许发起真实 DeepSeek 请求。
- `LocalEnvironment` 不是 sandbox。任何扩大命令权限的改动都必须说明影响。
- `mini-inspect` 只绑定 127.0.0.1，可写范围只有 `--config` 指向的配置文件和它旁边的 `prompts/`；轨迹目录始终只读，图片接口只允许读取轨迹目录内的图片文件。轨迹里有账户持仓、账本和完整决策过程，扩大可写范围或改绑定地址前必须先和用户确认门禁方案。
- `mini-inspect` 的试跑用子进程起 `mini --premarket` 或 `mini --round`，装配逻辑不重复一份；模式由 `--miniqmt-mode` 显式传入。同一时刻只允许一个运行，服务退出会终止它，不留能继续下单的孤儿进程。
- 配置文件是纯数据，机器会整份重写它，所以不要在 `deepseek.yaml` 里放注释；说明写在本文件。
- 角色配置的硬约束集中在 `config/validate_agents`：写盘前跑一遍，配置错误不许拖到下一次启动。
- 轨迹用临时文件加原子替换落盘：观测端会边跑边读，覆盖写会让它读到半截 JSON。
- `str_replace_editor` 只能访问工作区路径；编辑必须经过路径校验、唯一匹配检查和原子写入。
- 开发时代码逻辑尽量精简，不要为了旧逻辑兼容，新的改动应直接使用最新 idea。
- 模糊/非必要改动，要先问用户，获得明确同意后再推进。

运行检查：

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```

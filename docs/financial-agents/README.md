# GitHub 金融 Agent 调研

本目录记录适合当前 `mini-swe-agent` 的金融 Agent、投研框架和量化基础设施。调研重点不是照搬完整项目，而是识别可以在当前轻量架构中独立实现的能力。

## 当前框架边界

- 模型固定为 DeepSeek Chat Completions，默认 `deepseek-flash`（支持图片输入）。
- Agent 使用一个显式的迭代循环，工具由角色配置选择。
- 宿主工具是 `bash`、`str_replace_editor`、`web_search`、`web_fetch`。
- 本地命令和文件编辑由宿主执行；Agent 不持有交易账户、券商连接或密钥。
- 金融计算应尽量由确定性 Python 工具完成，LLM 负责解释、归纳和提出待验证假设。
- 大股票池应采用一次市场级批量筛选，不能对 5000 多只股票逐一执行完整 LLM 图。

## 仓库索引

| 文档 | 仓库 | 建议 |
|---|---|---|
| [tradingagents.md](tradingagents.md) | TauricResearch/TradingAgents | 参考多角色投研和市场级编排 |
| [tradingagents-astock.md](tradingagents-astock.md) | simonlin1212/TradingAgents-astock | 优先参考 A 股角色、字段和规则 |
| [tradingagents-cn.md](tradingagents-cn.md) | hsliuping/TradingAgents-CN | 只参考中文化流程，先核对混合许可证 |
| [finrobot.md](finrobot.md) | AI4Finance-Foundation/FinRobot | 优先参考确定性估值计算与证据链 |
| [valuecell.md](valuecell.md) | ValueCell-ai/valuecell | 只参考 Agent 产品分工，不引入全栈 |
| [langalpha.md](langalpha.md) | ginlix-ai/LangAlpha | 只参考持久化工作区，不引入其运行时 |
| [finskills.md](finskills.md) | Geeksfino/finskills | 优先移植 Skill prompt 和报告模板 |
| [finnewshunter.md](finnewshunter.md) | DemonDamon/FinnewsHunter | 参考财经新闻管线，不引入整套基础设施 |
| [openinvest.md](openinvest.md) | longsizhuo/openInvest | 参考审计、反方辩论和反前视验证 |
| [mira.md](mira.md) | byteseek/Mira | 参考可刷新 thesis 和证据跟踪 |
| [ai-hedge-fund.md](ai-hedge-fund.md) | virattt/ai-hedge-fund | 参考 mandate、alpha model 和回测接口 |
| [openbb.md](openbb.md) | OpenBB-finance/OpenBB | 作为未来数据适配器候选，不直接嵌入 |
| [qlib.md](qlib.md) | microsoft/qlib | 作为未来量化研究后端候选，不直接嵌入 |

## 调研情况

- ai-hedge-fund: 5种角色prompt，行情由Financial Datasets API 提供，需申请。


## 推荐落地顺序

1. `market_snapshot`：定义带 `as_of` 日期的行情、财务和市场状态 JSON。
2. `a_share_research`：增加一个中文市场级研究角色，先筛选候选集合，再生成研究报告。
3. `financial_calc`：将收益、回撤、估值、因子和组合风险计算做成确定性脚本。
4. `research_record`：保存数据源、证据 URL、缺失字段、结论和模型调用轨迹。
5. 在回测环境中复用同一份 point-in-time snapshot，严格禁止使用执行日之后的数据。

所有仓库的 Star、更新时间和依赖信息均为 2026-09-02 调研时的快照；接入前仍需重新检查上游版本、许可证和 API 行为。

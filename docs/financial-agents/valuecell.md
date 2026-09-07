# ValueCell-ai/valuecell

- 地址：https://github.com/ValueCell-ai/valuecell
- 调研快照：约 11k Star，Apache-2.0，最近提交 2026-03-09
- 定位：面向金融应用的社区多 Agent 平台，覆盖深度研究、新闻、策略和交易。

## 仓库实现方案

ValueCell 提供 DeepResearch Agent、策略 Agent、新闻检索 Agent，并支持多家 LLM、多个市场和 OKX/Binance 等交易所。README 同时描述 LangChain/Agno/A2A 集成、Web 产品、模型配置和交易 guardrail。完整项目包含前后端和较多部署配置，Python 版本要求为 3.12+。

## 适合迁移的部分

- 将“研究、策略、新闻”作为独立角色的产品分工。
- 交易 guardrail 的思想：模型提出意图，宿主校验资金、持仓、数量和风险后才执行。
- 市场和模型配置显式化，而不是硬编码在 prompt 中。

## 当前框架中的实现建议

仅在 `deepseek.yaml` 增加金融角色和配置变量，不引入 A2A、Agno、前端或交易所 SDK。定义统一的 `OrderIntent`/`ResearchCandidate` 数据结构，所有真实执行仍由外部 MiniQMT 环境负责。新闻和行情工具继续使用当前 `web_search`、`web_fetch` 以及本地 snapshot。

## 不应直接引入的部分

- Web UI、数据库、Redis 和异步任务系统。
- Binance/OKX 凭证和任何 Agent 直接交易通道。
- 为支持多供应商而增加新的 provider 抽象层。

## 验证要求

测试模型配置缺失、交易意图越权、数量/价格不合法和 Agent 失败时的拒绝行为。接入前重新检查仓库当前分支的 Python 版本、发布包和第三方依赖许可证。

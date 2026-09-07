# TauricResearch/TradingAgents

- 地址：https://github.com/TauricResearch/TradingAgents
- 调研快照：约 102k Star，Apache-2.0，最近提交 2026-09-01
- 定位：模拟真实投研团队的多 Agent 金融交易研究框架。

## 仓库实现方案

仓库按角色拆分分析和决策：基本面、技术面、新闻、社交情绪分析师先产出报告；牛方和熊方研究员进行辩论；Research Manager 汇总；Trader 形成交易计划；风险团队和 Portfolio Manager 决定是否批准。新版还包含结构化输出、checkpoint、决策日志、回测日期一致性和多供应商模型。

核心依赖包括 LangChain、LangGraph、Backtrader、Pandas、Stockstats、Yahoo Finance、Redis/SQLite checkpoint 等。它是一个完整的投研图，而不是一个可直接塞进当前仓库的小工具。

## 适合迁移的部分

1. **角色职责**：作为当前 YAML 中的不同 Agent profile 和中文 system prompt。
2. **牛熊辩论**：对同一个候选集合生成两份相互独立的论证，再由一个汇总角色裁决。
3. **结构化决策**：统一输出 `rating`、`thesis`、`risks`、`catalysts`、`evidence` 和 `confidence`。
4. **决策记录**：将每轮分析输入、来源和结论写入 JSONL，而不是只保留最终自然语言。

## 当前框架中的实现建议

第一阶段不要引入 LangGraph。新增 `a_share_research` 角色，依次调用 `web_search`/`web_fetch` 和一个本地 `market_snapshot` 脚本。大股票池先由脚本完成硬过滤，Agent 只处理候选集合。

可以把研究流程建模为四个显式阶段：`collect`、`analyze`、`debate`、`decide`。每阶段仍由当前 `DefaultAgent` 循环驱动，阶段结果以 JSON 文件或消息中的结构化区块传递。风险和组合约束由宿主或确定性脚本检查，Agent 不直接提交订单。

## 不应直接复制的部分

- LangGraph 全图、Redis、远程 checkpoint 和完整模拟交易所。
- Yahoo Finance/Alpha Vantage 的美股假设，尤其是交易制度和日期语义。
- 逐 ticker 完整调用的研究方式；5000 只股票应使用一次市场级批量分析。

## 验证要求

为每份输入保存 `source_day`，断言所有数据日期不晚于研究日；用 mock model 测试无工具回答、工具失败、格式错误和牛熊结论冲突；对确定性指标做独立单元测试。

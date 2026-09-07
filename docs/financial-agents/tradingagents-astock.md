# simonlin1212/TradingAgents-astock

- 地址：https://github.com/simonlin1212/TradingAgents-astock
- 调研快照：约 3.1k Star，Apache-2.0，最近提交 2026-09-02
- 定位：面向 A 股制度和数据源深度改造的 TradingAgents fork。

## 仓库实现方案

该项目保留 TradingAgents 的分析、辩论、Trader、风险和 Portfolio Manager 链路，但把分析师扩展为 7 类：市场、舆情、新闻、基本面、政策、游资、解禁。它明确处理 A 股的 T+1、涨跌停、最小交易单位和 ST 等约束。

README 列出的数据源包括 mootdx、腾讯财经、东方财富、新浪财经和同花顺，覆盖 K 线、实时估值、龙虎榜、板块、解禁和财报等。数据源以免费直连为主，稳定性、字段定义和历史可追溯性需要自行验证。

## 适合迁移的部分

- A 股专属研究维度：政策冲击、龙虎榜/游资、限售解禁和减持。
- A 股约束字段：`limit_up`、`limit_down`、`tradable_lot`、`t_plus_one`、`is_st`。
- 7 类分析师的职责划分和每类报告的输入/输出边界。
- A 股候选报告的中文模板。

## 当前框架中的实现建议

先设计一个版本化的 `market_snapshot`：

```json
{
  "as_of": "2026-09-01",
  "universe": [{"code": "600000.SH", "name": "...", "close": 0, "volume": 0}],
  "market": {"index_return": 0, "breadth": 0, "limit_up": 0, "limit_down": 0},
  "events": [],
  "data_quality": {"complete": true, "sources": []}
}
```

用本地脚本批量获取和清洗数据，再让一个 `a_share_research` Agent 对 snapshot 做市场判断。只有进入具体订单审核的股票才进行 ticker 级深度研究。输出候选代码必须与宿主提供的 universe 交集，不能让 LLM 生成不在股票池中的代码。

## 许可证与风险

该仓库本身标注 Apache-2.0，可以作为代码参考，但仍需保留上游版权和检查第三方数据源条款。免费财经接口可能限流、改字段或缺失历史数据；不能把“接口返回非空”当作数据完整性证明。

## 验证要求

针对交易日、复权方式、涨跌停、T+1 可卖数量、ST 过滤和停牌字段写 fixture。对每个股票日检查日期、价格、成交量槽位，回测时确保 snapshot 的 `as_of` 早于执行日。

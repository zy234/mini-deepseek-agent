# Geeksfino/finskills

- 地址：https://github.com/Geeksfino/finskills
- 调研快照：约 279 Star，Apache-2.0，最近提交 2026-03-05
- 定位：面向 Claude/Agent 的金融分析 Skill 集合，覆盖美股和 A 股。

## 仓库实现方案

仓库提供 30 个 Skill，每个 Skill 通常由 `SKILL.md`、方法论参考和输出模板组成，并通过一个 FinData Toolkit 获取行情、财报、宏观数据和组合指标。A 股目录包含低估值、董监高增减持、情绪偏差、高股息、行业轮动、组合健康、事件驱动、因子筛选和 ESG 等主题。

它的价值主要在于分析流程、触发条件、评分规则和报告模板，而不是复杂运行时。

## 适合迁移的部分

优先迁移三个小 Skill：

1. A 股基本面/低估值筛选。
2. 情绪与基本面偏差分析。
3. 组合健康诊断。

每个 Skill 只保留中文触发条件、所需字段、步骤、输出 schema、数据缺口和风险声明。不要原样引入 Claude 专有的技能发现协议。

## 当前框架中的实现建议

新增 `skills/` 目录，并在 YAML 的 Agent profile 中配置默认 Skill。当前 `DefaultAgent` 已经支持不同 system/instance template 和工具列表，因此第一版可以将 Skill 编译成 prompt 模板，不需要动态类加载。

数据工具统一返回表格或 JSON，Skill 不直接抓网页。真实数据缺失时必须输出 `missing_data`，不能用模型常识填充。报告末尾保存来源 URL 和数据日期。

## 许可证与边界

仓库标注 Apache-2.0，但仍需逐文件检查第三方脚本和数据源许可证。FinSkills 的美股脚本不能直接用于 A 股，尤其是 SEC/FRED、Form 4、股息贵族等规则。

## 验证要求

用固定 fixture 检查筛选排序、因子方向、组合集中度和缺失字段行为；使用 mock 工具调用测试 Skill 是否能在数据不可用时如实降级。

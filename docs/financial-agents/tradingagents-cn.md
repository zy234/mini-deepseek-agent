# hsliuping/TradingAgents-CN

- 地址：https://github.com/hsliuping/TradingAgents-CN
- 调研快照：约 31k Star，README 标注 v1.1.0，最近提交 2026-07-24
- 定位：TradingAgents 的中文增强版和 A 股/港股/美股学习研究平台。

## 仓库实现方案

项目将上游框架扩展为 FastAPI + Vue 3 + MongoDB + Redis + Docker，并加入多供应商模型、批量分析、用户管理、配置中心、报告导出和模拟交易。README 明确说明开源部分与 `app/`、`frontend/` 专有部分采用混合授权，商业使用需要联系作者。

## 适合迁移的部分

- 中文化的 Agent 角色命名、研究报告结构和教学文档组织。
- Tushare、AkShare、BaoStock 等 A 股数据源的适配思路。
- 批量筛选、模拟交易和报告导出的产品需求清单。

## 当前框架中的实现建议

只从公开、明确授权的文件中提炼 prompt、字段和测试案例，不复制 `app/` 或 `frontend/` 代码。当前仓库先实现一个中文 `a_share_research` profile 和本地 JSON 报告；数据源适配器保持独立脚本，避免把 MongoDB/Redis/FastAPI 引入核心 Agent。

## 许可证与风险

不能把“Apache 2.0”徽章理解为全仓库可自由商用。README 明确指出部分目录为专有代码，商业使用需授权；接入前必须逐目录核对 LICENSE、COPYRIGHT 和第三方数据源条款。

## 验证要求

检查报告中的市场、日期、数据源和免责声明字段；对 A 股交易制度、数据源切换和批量分析失败做 fixture。任何迁移代码都应保留来源说明并经过许可证审查。

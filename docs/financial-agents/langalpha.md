# ginlix-ai/LangAlpha

- 地址：https://github.com/ginlix-ai/LangAlpha
- 调研快照：约 1.7k Star，Apache-2.0，最近提交 2026-09-02
- 定位：面向金融市场的持久化 Agent 工作区和研究工作台。

## 仓库实现方案

LangAlpha 把每个研究目标映射为持久化 workspace，支持渐进式工具发现、Programmatic Tool Calling、MCP 金融数据、记忆、自动化、并行子 Agent 和 Web/TUI。后端依赖 LangGraph、PostgreSQL、Redis、浏览器抓取、Daytona 沙箱等，且要求 Python 3.13+。

## 适合迁移的部分

- “研究目标 -> 工作区 -> 日后增量更新”的持久化模型。
- 工具文档按需发现，减少每轮 prompt 体积。
- 将长任务的中间结果、来源和摘要写入本地文件。
- 后台任务与交互式对话分离的产品思路。

## 当前框架中的实现建议

用已有 `.sessions` 轨迹和新增 `research/` 目录实现轻量 workspace，不引入 PostgreSQL/Redis。为金融角色提供一份精简的工具说明，只有在需要时调用 `web_search` 或 `web_fetch`。长研究通过多个显式阶段和落盘文件恢复，不做并行子 Agent，先保证数据日期和证据可审计。

## 不应直接引入的部分

- LangGraph、DeepAgents、Daytona、浏览器沙箱和 MCP 服务器集群。
- Python 3.13 专属运行时设计。
- 多租户、OAuth、SSE 重连和生产 Web 基础设施。

## 验证要求

测试同一研究目标跨会话恢复、重复运行幂等、文件 hash 冲突、工具失败可见性和来源日期边界。任何后台化能力都不能绕过宿主审批和本地权限边界。

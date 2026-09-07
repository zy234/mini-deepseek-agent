# OpenBB-finance/OpenBB

- 地址：https://github.com/OpenBB-finance/OpenBB
- 调研快照：约 72k Star，许可证元数据为 NOASSERTION，最近提交 2026-07-30
- 定位：面向分析师、量化研究者和 AI Agent 的开放金融数据平台。

## 仓库实现方案

OpenBB 提供统一的数据访问层，覆盖行情、财务、宏观、另类数据和分析工具，并通过 Python/CLI/API 等方式暴露。它的主要价值是 provider 和数据模型生态，不是一个与当前 `DefaultAgent` 直接相同的 Agent 循环。

## 适合迁移的部分

- provider 与标准数据模型分离的思路。
- 为数据请求统一设置 `symbol`、`start`、`end`、`as_of`、来源和质量状态。
- 将数据供应商失败区分为未配置、网络失败、无数据、字段解析失败。

## 当前框架中的实现建议

不要把 OpenBB 作为默认依赖。先定义本仓库自己的 `market_snapshot` schema 和一个 provider 适配接口；需要时再把 OpenBB 作为可选外部进程或独立环境调用。Agent 只看到标准化 JSON，不依赖具体 provider 名称。

对于 A 股，先使用已有本地数据或专门的 A 股适配器；不能因为 OpenBB 有统一 API 就假定其 A 股数据完整。所有返回数据必须记录来源、抓取时间、交易日和字段完整性。

## 许可证与风险

GitHub API 的许可证字段为 NOASSERTION，接入前必须检查仓库 LICENSE 和各 provider 的单独条款。金融数据的再分发、缓存和商业使用可能有额外限制。

## 验证要求

为每个 provider 写契约测试：字段类型、时区、复权、重复行、空响应、限流和日期边界。禁止把 provider 的异常吞掉后返回“正常但为空”的 snapshot。

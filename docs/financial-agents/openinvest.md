# longsizhuo/openInvest

- 地址：https://github.com/longsizhuo/openInvest
- 调研快照：约 82 Star，MIT，最近提交 2026-09-02
- 定位：面向 Agent 的可审计投资决策委员会和研究引擎。

## 仓库实现方案

项目强调隔离的多 Agent 投资委员会、协调者/工人架构、证据链、反方辩论、长期回测和反前视偏差。研究记录使用 YAML frontmatter + Markdown，强调保存负结果和可复现的决策过程，而不是展示预测收益。

## 适合迁移的部分

- 每个结论必须关联证据、日期和数据来源。
- Bull/Bear/裁判使用隔离输入，避免一个 Agent 的结论污染另一个 Agent。
- 回测结果和研究结论分开保存。
- 明确记录 HOLD、数据不足和“没有发现 alpha”，不要强制 BUY/SELL。

## 当前框架中的实现建议

新增一个 `research_record` 结构，写入 `.sessions` 之外的金融研究目录：

```text
research/
  20260901-600000/
    request.json
    evidence.jsonl
    bull.md
    bear.md
    verdict.md
    validation.json
```

当前 Agent 仍负责对话和工具调用；记录器只接受已完成的消息和工具观察。若实现辩论，给 Bull/Bear 相同的原始 snapshot，不互相传递未验证结论，最后由确定性 schema 校验裁判输出。

## 许可证与风险

MIT 允许较宽松的代码参考，但仍需检查依赖许可证。项目公开的低命中率和负结果不能被包装成投资能力证明，恰好应作为验证纪律的参考。

## 验证要求

写一个反前视测试：任何 evidence 的 `as_of` 晚于决策日都必须使研究记录失败。测试 HOLD、空证据、互相矛盾结论和中途工具失败的记录完整性。

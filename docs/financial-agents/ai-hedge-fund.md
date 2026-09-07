# virattt/ai-hedge-fund

- 地址：https://github.com/virattt/ai-hedge-fund
- 调研快照：约 63k Star，MIT，最近提交 2026-08-07
- 定位：教育和研究用途的 AI Hedge Fund Team，提供投资人 Agent、alpha model、mandate 和回测概念。

## 仓库实现方案

项目用多个投资风格 Agent 生成信号，并通过 mandate 描述策略、资金、风险和再平衡周期。当前版本支持从命令行运行单次 cycle 或回测，使用 LangChain、多模型供应商、Pandas、SciPy、Textual 等依赖。README 明确声明不进行真实交易。

## 多 Agent Prompt 设计

源码中的 LLM 投资人都继承同一个 `LLMAgent`，persona 子类只定义 `name` 和 `get_system_prompt()`。这是一种“公共执行器 + 个性化 system prompt”的设计，不是每个 persona 都拥有一套不同的工具链。

### 共同的输入 Prompt

每次调用发送两条消息：

1. **system**：persona 的投资方法、信号规则、置信度刻度、硬性数据边界和 JSON 输出格式。
2. **user**：由 `FundamentalsSnapshot.render()` 生成的紧凑文本，包含公司、行业、最近已披露的财务摘要和最多 20 个 TTM 历史行。

snapshot 中包含的典型字段有市值、P/E、ROE、毛利率、营业利润率、净利率、负债权益比、流动比率、营收增速、EPS、每股账面价值和每股自由现金流。Python 在送入模型前计算 ROE/净利率均值、毛利率趋势、BVPS CAGR 等派生值，减少模型自行算数。

Prompt 明确要求：只能使用给出的数据；把最近一条已披露 filing 当作“现在”；不能使用之后发生的事件或补造数字；数据不足时返回 `neutral`。所有 persona 都要求只输出下面的 JSON：

```json
{"signal":"bullish|bearish|neutral","confidence":0,"reasoning":"2-4 sentences"}
```

其中 `confidence` 范围为 0-100，解析后转换为 `Signal.value`：看涨为正、看跌为负、中性为零，再乘以置信度百分比。

### 五个投资人 Persona Prompt

| Agent | Prompt 中的检查清单 | 触发 bullish / bearish 的核心条件 |
|---|---|---|
| `buffett` | 能力圈、护城河、ROE/利润率、管理层资本配置、财务实力、估值、十年持有 | 耐久业务且价格合理 / 业务弱化或价格要求完美 |
| `graham` | 安全边际、P/E 与 PB、流动比率、负债、盈利稳定性、对增长溢价保持怀疑 | 资产负债表稳健且有安全边际 / 财务弱、盈利不稳或价格透支希望 |
| `munger` | 逆向思考、业务质量、激励与资本配置、真实自由现金流、估值、“太难理解”清单 | 明显优秀且价格不荒谬 / 平庸恶化、数字可疑或估值需相信愚蠢假设 |
| `lynch` | 增长分类、PEG、收入到盈利的传导、利润率、EPS 趋势、资产负债表 | 可见盈利增长且 PEG 合理 / 增长放缓但估值昂贵、故事价格脱离数据 |
| `druckenmiller` | 最近季度的加速/减速、利润率拐点、EPS 动量、市场已计价预期、不对称性、避免大亏 | 新近加速且价格未充分反映 / 明显恶化或旧预期仍支撑高估值 |

这些名字只是公开投资思想的风格化 prompt，不代表真实人物背书。`druckenmiller` 的 prompt 还特别声明当前只看到基本面快照，没有宏观、利率和价格行为数据，不能假装拥有这些信息。

### 五个角色的具体 Prompt 与工具

下面是根据上游 `get_system_prompt()` 整理的可直接迁移版本。五个角色的公共约束、输出 schema 和数据边界相同，只有投资检查清单和信号判断不同。

#### 1. Buffett / `buffett`

```text
你是 Warren Buffett，以长期企业所有者而不是交易员的视角评估一家公司。

按以下清单分析：
1. 能力圈：仅凭给定数据能否理解这项业务？
2. 护城河：ROE 是否持久，利润率是否稳定或改善，是否有定价权？
3. 管理层质量：账面价值是否复利增长，资本配置是否理性，杠杆是否适度，自由现金流是否持续？
4. 财务实力：债务是否低，流动比率是否健康，盈利是否连续？
5. 估值：市值和 P/E 相对业务质量与增长是否合理？优秀公司合理价格优于普通公司低价。
6. 长期前景：是否愿意持有十年？

信号规则：优秀且耐久、价格合理或便宜为 bullish；业务弱化或价格要求完美为 bearish；证据混合或优秀业务明显过贵为 neutral。
置信度 90-100 表示证据强且结论罕见明确，70-89 表示较有把握，40-69 表示混合，10-39 表示弱或投机。
只能使用给定数据；把最近 filing 当作现在；不能使用之后的知识或编造数字；数据不足时返回 neutral。
只输出 JSON：{"signal":"bullish|bearish|neutral","confidence":0-100,"reasoning":"2-4句"}
```

#### 2. Graham / `graham`

```text
你是 Benjamin Graham，以防御型投资者的视角评估一家公司；只关心价格与已证明价值的关系。

按以下标准分析：
1. 安全边际：价格相对盈利能力和账面价值是否足够低；结合 P/E、PB、EPS 和每股账面价值判断。P/E 显著高于 15-20 通常需要你不会轻易接受的额外证明。
2. 财务实力：流动比率是否舒适地高于 1.5，负债权益比是否适度；资产负债表弱则无论前景如何都不合格。
3. 盈利稳定性：给出的完整历史是否持续盈利，是否存在剧烈波动。
4. 增长溢价：对为预测增长支付高价保持怀疑，已证明的盈利比故事更重要。

信号规则：财务稳健且有真实安全边际为 bullish；财务弱、盈利不稳或价格透支希望为 bearish；企业尚可但没有安全边际为 neutral。
置信度 90-100 表示所有定量标准都清晰满足，70-89 表示大部分满足，40-69 表示混合，10-39 表示投机。
只能使用给定数据；把最近 filing 当作现在；不能编造数字；数据不足时返回 neutral。
只输出 JSON：{"signal":"bullish|bearish|neutral","confidence":0-100,"reasoning":"2-4句"}
```

#### 3. Munger / `munger`

```text
你是 Charlie Munger，以严格、宁缺毋滥的方式评估一家公司；宁可错过十个好机会，也不接受一个坏机会。

按以下心智模型分析：
1. 反向思考：什么会让投资失败？寻找利润率恶化、杠杆上升和 ROE 下滑。
2. 业务质量：优秀业务应多年保持高资本回报，不依赖英雄式假设；看完整历史而不是单个好年份。
3. 激励与资本配置：账面价值是否复利增长，自由现金流是否真实且增长，还是持续消耗资本？
4. 价格：优秀业务可以接受合理价格；检查 P/E 是否匹配实际增长和质量。
5. 太难理解清单：数字无法形成清晰图景时，直接归入 too-hard pile 并返回 neutral。

信号规则：明显优秀且价格不荒谬为 bullish；业务平庸/恶化、数字看起来不诚实或估值需要愚蠢假设为 bearish；太难判断或好公司太贵为 neutral。
置信度 90-100 表示质量和价格罕见地同时匹配，70-89 表示较稳固，40-69 表示证据混合，10-39 表示几乎没有优势。
只能使用给定数据；把最近 filing 当作现在；不能编造数字；结论要直接，不要用空泛的保留话术。
只输出 JSON：{"signal":"bullish|bearish|neutral","confidence":0-100,"reasoning":"2-4句"}
```

#### 4. Lynch / `lynch`

```text
你是 Peter Lynch，像在 Magellan 一样评估公司：知道自己买的是什么，也知道为什么买。

按以下清单分析：
1. 分类：根据增长和利润率历史判断 fast grower（盈利增长 20%+）、stalwart（10-12%）、slow grower 或 turnaround；信号必须符合类别。
2. PEG：将 P/E 与数据中实际可见的盈利增长比较；P/E 明显低于增长率有吸引力，明显高于增长率则是在为故事付费。
3. 故事是否兑现：营收增长是否转化为盈利增长，利润率是否保持/改善，EPS 是否逐季上升。
4. 资产负债表：避免债务过重，强资产负债表能让增长故事熬过坏年份。
5. 盈利驱动股价：长期只关心盈利能否继续增长，以及为增长付出了多少价格。

信号规则：真实可见的盈利增长且 PEG 有吸引力为 bullish；增长减速但估值溢价或故事价格脱离数据为 bearish；公司尚可但已充分定价或无法分类为 neutral。
置信度 90-100 表示增长便宜且清晰，70-89 表示故事良好且价格公平，40-69 表示混合，10-39 表示无法判断。
只能使用给定数据；把最近 filing 当作现在；不能编造数字；无法用简单语言解释时返回 neutral。
只输出 JSON：{"signal":"bullish|bearish|neutral","confidence":0-100,"reasoning":"2-4句"}
```

#### 5. Druckenmiller / `druckenmiller`

```text
你是 Stanley Druckenmiller，评估一家公司当前的轨迹相对市场预期是否发生拐点。你关心最近变化，不关心三年前的静态样貌；只有赔率不对称时才出手。

按以下清单分析：
1. 拐点：比较最近季度与更早季度，营收增长是在加速还是减速，利润率是在上行还是反转；方向和变化率比绝对水平重要。
2. 盈利轨迹：最近几个期间的 EPS 动量是在增强还是衰减？
3. 已计价预期：高 P/E 配合加速可能仍可买；低 P/E 配合恶化通常是陷阱。判断估值隐含的市场信念是否与趋势冲突。
4. 不对称性：只有拐点和价格同时匹配才考虑较大仓位，普通机会应当不持仓。
5. 不要大亏：基本面恶化叠加杠杆上升只能做 bearish 或放弃，不能模糊持有。

信号规则：清晰加速且价格尚未充分反映为 bullish；清晰恶化或反转且价格仍按旧轨迹定价为 bearish；没有明显拐点或价格已充分反映为 neutral。
置信度 90-100 表示拐点明确且赔率不对称，70-89 表示趋势变化较可靠，40-69 表示早期/混合，10-39 表示没有优势。
当前输入只有基本面快照，没有宏观、利率或价格行为数据；只能根据基本面轨迹判断，不得假装拥有其他数据。只能使用给定数据、不能编造数字。
只输出 JSON：{"signal":"bullish|bearish|neutral","confidence":0-100,"reasoning":"2-4句"}
```

### 角色工具权限（实际上没有模型侧工具）

这五个 Agent 在上游都只实现 `LLMClient.complete(system, user)`，没有 function/tool calling，也不会自行访问行情网站、财报 API、文件系统或券商接口。它们能看到的唯一输入是宿主构造好的 `FundamentalsSnapshot.render()` 文本。

宿主在调用 prompt 之前完成以下数据操作：

```text
DataClient.get_financial_metrics(
    ticker, as_of, period="ttm", limit=20
)
DataClient.get_company_facts(ticker)
    -> build_snapshot()
    -> snapshot.render()
    -> LLMAgent.complete(system_prompt, user_prompt)
```

因此五个角色的工具矩阵相同：

| 角色 | 模型侧工具 | 宿主侧可用数据 | 不可做的事 |
|---|---|---|---|
| Buffett | 无 | point-in-time 财务 snapshot | 自己查新闻、补数字、下单 |
| Graham | 无 | 同一份财务 snapshot | 自己计算未提供的事实、访问网络 |
| Munger | 无 | 同一份财务 snapshot | 读取其他 Agent 结论、执行交易 |
| Lynch | 无 | 同一份财务 snapshot | 自行搜索增长数据、调用外部 API |
| Druckenmiller | 无 | 同一份财务 snapshot | 假装拥有宏观/价格行为数据、下单 |

这也是迁移到当前 `mini-swe-agent` 时应保持的边界：可以由宿主新增 `market_snapshot`、`web_search` 或确定性 `financial_calc` 工具，但不要把这些工具直接交给 persona。工具先返回可审计数据，角色只对数据做判断；订单数量、价格、持仓和风险始终由宿主环境处理。

### Prompt 缓存和失败语义

`prompt_key(agent, model, system, user)` 对完整 prompt 做 SHA-256 截断，`PromptCache` 将精确的 system、user、raw response、parsed 结果、snapshot hash 和时间写入一个 JSON 文件。相同模型、persona 和未变化快照会命中缓存，回测重跑不会重复付费；即使解析失败，原始响应也会保留用于排查。

缓存默认位于 `~/.hedge-fund/cache/llm/`，不是仓库工作区。snapshot 的 `content_hash` 和渲染文本排除 `as_of`，因此两个日期之间如果没有新 filing，会复用同一个 prompt 结果；这也是回测成本可控且结果可重放的关键。

- snapshot 数据层错误直接抛出，不能伪装成中性信号。
- 历史不足（少于 4 个已披露周期）返回 `abstained=true` 的零信号。
- LLM 调用失败或 JSON 解析失败返回 `abstained=true` 的零信号，同时记录原因。
- `abstained` 在组合时被排除在分子和分母之外；真正的 `neutral` 则是有效投票，会稀释其他观点。

## 适合迁移的部分

- `mandate`：把研究目标、股票池、风险预算和调仓频率从自然语言中独立出来。
- `alpha model`：将某个可回测信号定义为输入、输出和参数，而不是让 LLM 任意改写交易逻辑。
- 单次 cycle 与历史回测共用同一策略接口。
- 终端输出同时保留结构化 JSON 和人类摘要。

## 当前框架中的实现建议

可以定义极简 YAML：

```yaml
name: a_share_screening
universe: csi300
rebalance: daily
risk_budget: 0.1
research_only: true
```

`DefaultAgent` 读取 mandate，调用市场级 snapshot 和确定性筛选脚本，输出候选及理由。回测环境自行执行订单和收益计算，Agent 只能产生 `OrderIntent` 或研究候选，不能访问券商 API。

## 端到端流程

仓库的核心路径是下面这条固定流水线：

```text
FundSpec / mandate + as_of + universe
              |
              v
      获取最近收盘价（仅 <= as_of）
              |
              v
  对每个 strategy、ticker、model 顺序调用 predict()
              |
              v
  每个 persona: build_snapshot -> prompt/cache -> JSON -> Signal
              |
              v
  strategy 内按 model weight 做 conviction 加权平均
              |
              v
  可选横截面去均值（market-neutral）
              |
              v
  按 strategy capital slice 合并成全基金 target weights
              |
              v
  硬性 risk limits：单票上限，再做 gross exposure 缩放
              |
              v
  target weights 与持仓做差，先卖后买，broker 执行
              |
              v
  CycleRecord 保存 signals、convictions、clamps、orders、fills、NAV
```

这里的“多 Agent”调用是**顺序 fan-out**：`run_cycle()` 先遍历策略，再遍历可交易 ticker，最后遍历该策略的 staff。源码没有把五个 persona 的自然语言报告互相塞回 prompt，也没有在 Agent 内部做自由形式的多轮辩论；协作发生在统一 `Signal` 之上的确定性加权和风险阶段。

### 一个周期如何形成组合

对 ticker `t`，同一 strategy 的 conviction 是各个非 abstain signal 的加权平均：

```text
conviction_t = sum(model_weight * signal.value) / sum(model_weight)
```

然后按所有 ticker 的 `sum(abs(conviction))` 归一化到 `gross_target`。启用 `market_neutral` 时，先减去横截面均值，再归一化，形成相对看好/看空的多空组合。多个 strategy 的 sleeve 再按 capital slice 合并，最后才应用全基金风险上限。风险阶段只会缩小仓位，不会把被削掉的风险重新分配给其他股票。

### 单次运行与回测

- **单次 cycle**：`run_cycle()` 使用一个持久 broker 状态，完成 snapshot、信号、组合、风控、下单和记录。
- **基金回测**：`backtest_fund()` 从 benchmark 的真实交易日生成 daily/weekly/monthly 网格，对每个日期重复 `run_cycle()`，使用持久 `SimBroker`，所以持仓和现金跨周期保留。
- **单模型简化回测**：`BacktestEngine.run_alpha()` 可只测试一个 `AlphaModel`，按阈值和固定持有天数模拟交易；它不替代基金级组合和风险流程。

每个周期都会保留 `CycleRecord`，包括原始 signals、每个策略的 conviction、风险 clamp、订单、成交、持仓和 NAV。这使得 `fund why ...` 一类解释可以从记录重放，而不是重新询问模型。

## 源码定位

以下链接是本节结论对应的上游源码入口，接入前应以当前分支为准重新核对：

- [LLMAgent 公共执行器](https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/signals/llm_agent.py)
- [AlphaModel 接口](https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/signals/base.py)
- [五个 persona prompt：Buffett](https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/signals/buffett.py)、[Graham](https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/signals/graham.py)、[Munger](https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/signals/munger.py)、[Lynch](https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/signals/lynch.py)、[Druckenmiller](https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/signals/druckenmiller.py)
- [Point-in-time snapshot 与 prompt 文本](https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/features/snapshot.py)
- [单周期编排](https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/pipeline/run_cycle.py)、[组合加权](https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/portfolio/construction.py)、[基金回测](https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/backtesting/fund.py)

## 数据接口记录

上游默认使用 Financial Datasets API，基础地址为 `https://api.financialdatasets.ai`，请求需要环境变量 `FINANCIAL_DATASETS_API_KEY`，通过 `X-API-Key` 请求头鉴权。

| 接口 | 主要参数 | 用途 |
|---|---|---|
| `GET /financial-metrics/` | `ticker`、`filing_date_lte`、`period=ttm`、`limit` | 获取截至指定日期已经公开的财务指标和每股数据 |
| `GET /company/facts/` | `ticker` | 获取公司名称、行业、板块、交易所等元信息 |

`FundamentalsSnapshot` 主要由这两个接口构建：财务指标接口提供历史周期，`company/facts` 补充公司元信息。成功响应会由 `CachedDataClient` 缓存到 `~/.hedge-fund/cache/data/`；网络、鉴权、限流和服务器错误应抛出，不能静默转为空数据。

当前环境未配置 `FINANCIAL_DATASETS_API_KEY`，因此尚未完成鉴权后的真实接口测试。后续可用同样的 `DataClient` 协议替换为 A 股数据源或本地 MiniQMT 数据。

## 不应直接引入的部分

- LangChain 全套 provider 和 Textual UI。
- 把教育性示例当作实盘策略。
- 让模型直接决定数量、价格、现金和持仓状态。

## 验证要求

测试 mandate 缺字段、股票池为空、调仓日不一致、回测和单次 cycle 输入不同以及 Agent 失败时的 fail-closed 行为。收益计算必须由宿主回测环境完成。

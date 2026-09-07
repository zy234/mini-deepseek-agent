# DemonDamon/FinnewsHunter：Prompt 与工具实现调研

- 地址：https://github.com/DemonDamon/FinnewsHunter
- 调研源码：`main` 分支，2026-09-02 读取
- 关注范围：Agent prompt、新闻/数据工具、可迁移到当前项目的实现细节

## Prompt 是什么

### 1. NewsAnalyst prompt

源码位置：`backend/app/agents/news_analyst.py` 的 `NewsAnalystAgent.analyze_news()`。

它是一段拼接输入数据的单次分析模板：

```text
你是一位经验丰富的金融新闻分析专家……

【新闻标题】
{news_title}

【新闻内容】
{news_content[:2000]}

【关联股票】
{stock_codes}

请输出结构化分析报告：
1. 摘要
2. 情感倾向和 -1 到 +1 评分
3. 关键信息表格
4. 短/中/长期市场影响
5. 投资建议和风险提示
```

具体约束有：

- 角色身份：10 年以上经验的金融新闻分析师。
- 输入截断：正文最多 2000 个字符。
- 输出格式：Markdown；要求固定标题、表格和列表。
- 情绪字段：利好/利空/中性，以及一位小数评分。
- 表格规则：同一类别必须在同一行，多条内容用 `<br>`。
- 事实要求：有数字时给出具体数字，数据必须来自新闻内容。

可借鉴的是“固定分析维度”和“输入边界”；不宜照搬的是超长 Markdown 格式约束。该 prompt 没有把 `news_url` 放进去，也没有要求逐条证据引用，因此结果不能天然审计。

### 2. Bull/Bear prompt

源码位置：`backend/app/agents/debate_agents.py` 的 `BullResearcherAgent.analyze()`、`BearResearcherAgent.analyze()`。

两者共享相同的输入块：当前时间、股票代码/名称、相关新闻摘要、额外背景；区别在于立场。

看多 prompt 要求：

- 列出 3-5 个看多理由，并用新闻或数据支撑。
- 分析 1-3 个月和 3-12 个月催化剂。
- 做估值和同行比较。
- 给出预期收益空间及达成条件。
- 即使看多也列出风险。

看空 prompt 要求：

- 列出 3-5 个风险因素，并用新闻或数据支撑。
- 分析短期利空和中长期结构性风险。
- 评估估值过高、同行劣势和下行空间。
- 反驳常见看多逻辑，指出乐观预期的不确定性。

这部分最值得借鉴的是“正反两个独立视角 + 证据要求 + 时间跨度”。当前项目不需要真的创建两个 Agent，可以在一个 `news_research` prompt 中加入“同时检查正向和反向解释”的清单。

### 3. InvestmentManager prompt

源码位置：`backend/app/agents/debate_agents.py` 的 `InvestmentManagerAgent.make_decision()`。

输入是股票信息、Bull 文本、Bear 文本和市场背景；输出分为：

1. 分别评价看多/看空论点质量。
2. 评价数据是否充分、缺少什么数据。
3. 给出短期和中长期综合判断。
4. 输出最终评级、持仓者操作、观望者操作和监测指标。
5. 评估预期收益、潜在下行和风险收益比。

可迁移的 prompt 技巧是先要求“评价证据充分性”，再要求方向判断。不可直接迁移“买入/加仓/清仓”等交易指令，因为当前项目的新闻 Agent 应只产出研究事实和影响判断。

### 4. 动态搜索 prompt

源码位置：`backend/app/agents/orchestrator.py` 的 `_build_debate_prompt()` 和 `_check_manager_interrupt_or_search()`。

它要求模型在发言末尾输出文本标记：

```text
[SEARCH: "最新的毛利率数据" source:akshare]
[SEARCH: "最近的行业新闻" source:bochaai]
[SEARCH: "竞品对比分析"]
```

并限制“只有确实缺少数据时才请求，每次最多 1-2 个”。经理还可以只回复“决策就绪”或一个搜索请求。

这个设计可借鉴的是：搜索请求应具体描述“缺哪项数据”，而不是泛泛地说“再搜一下”。但当前项目已有原生 function tool-call，应该把 `web_search` 作为真正工具调用，不要实现 `[SEARCH]` 文本协议。

## 工具是怎么实现的

### 1. 新闻获取工具

源码位置：`backend/app/financial/tools.py` 的 `FinancialNewsTool`。

实现流程：

1. 接收 `keywords`、`stock_codes`、`limit`、`provider`。
2. 构造 `NewsQueryParams`，由参数模型统一校验。
3. 从 Provider Registry 获取新闻 fetcher，支持指定 provider 或自动降级。
4. `await fetcher.fetch(params)` 获取 `NewsData` 列表。
5. 统一返回 `{success, count, provider, data}`，其中 `data` 是模型序列化后的字典。
6. provider 不存在、没有可用数据源和未知异常分别返回错误信息及可用 provider 列表。

借鉴点：输入参数模型、统一返回字段、显式报告 provider 和错误原因。当前项目的 `web_search` 已经采用类似思想，不需要引入 Provider Registry。

### 2. 股票价格工具

源码位置：`backend/app/financial/tools.py` 的 `StockPriceTool`。

实现流程：

- 接收 `symbol`、`interval`、`limit`、`adjust`、`provider`。
- 先把周期和复权类型转换成枚举，参数无效立即返回失败。
- 通过 Registry 选择 `stock_price` fetcher。
- 返回实际 provider、记录数和标准化 K 线数组。

对新闻研究的直接借鉴价值有限。它说明价格数据应使用独立的标准化工具，不应混进新闻正文分析 prompt。

### 3. 爬虫基类

源码位置：`backend/app/tools/crawler_base.py` 的 `BaseCrawler`。

它定义了统一的 `NewsItem`：

```text
title, content, url, source, publish_time,
author, keywords, stock_codes, summary, raw_html
```

网页抓取过程包含：

- `requests.Session` 复用连接和 User-Agent。
- 超时、最多 3 次重试；503 不重试。
- 根据 `apparent_encoding` 和 utf-8/gb2312/gbk/gb18030 探测中文编码。
- BeautifulSoup 解析 HTML，按选择器提取正文。
- 清理 HTML 标签、全角空格、多余空白。
- 对标题和 URL 做股票相关关键词过滤。

可借鉴的是统一新闻字段、编码处理和失败重试；不要把整套站点爬虫搬进当前 Agent，当前项目已经有 `web_fetch` 的通用正文提取边界。

### 4. 增强爬虫

源码位置：`backend/app/tools/crawler_enhanced.py` 的 `EnhancedCrawler`。

它把抓取拆成几个可替换阶段：

1. `requests`、Playwright、Jina 三类引擎。
2. 主引擎失败或正文过短时切换备用引擎。
3. 可选 URL 缓存。
4. `ContentExtractor` 提取标题、正文、发布时间。
5. 根据正文长度、段落数量和标题质量计算 `quality_score`。
6. 质量过低时再次尝试更强的引擎。
7. 批量抓取时逐 URL 延时，避免连续请求。

可迁移的最小部分是“内容质量不足时返回质量问题并让模型知道”，而不是引入 Playwright/Jina。当前项目的 `web_fetch` 已有 `content_truncated`、`published_at`、`content_type` 和错误码，可以把这些字段直接映射成质量诊断。

### 5. TextCleanerTool

源码位置：`backend/app/tools/text_cleaner.py`。

这是一个本地文本处理工具，不访问网络：

- `clean`：移除 URL、邮箱、特殊字符和多余空格。
- `tokenize`：jieba 分词，可去停用词。
- `keywords`：jieba TF-IDF 提取关键词。
- `normalize_stock_code`：去除 `SH/SZ/HK` 前缀。

工具通过 `execute(**kwargs)` 接收 `text`、`operation`、`top_k` 等参数，返回 `{success, result, count}`。NewsAnalyst 虽然默认创建了这个工具，但 `analyze_news()` 实际直接调用 LLM，没有把清洗工具接入调用链，这是一个实现落差。

## 对当前项目最有价值的 prompt 改造

建议新增一个只开放 `web_search`、`web_fetch` 的 `news_research` 角色，prompt 采用下面的短模板：

```text
你是金融新闻事实研究员，只做事实提取和影响分析，不生成交易指令。

工作顺序：
1. 提取标题、来源、发布时间和原文事实。
2. 判断事实对应的公司、股票代码、行业；不确定时标记 uncertain。
3. 分别检查正向、负向和中性解释，禁止只依据标题判断。
4. 判断影响方向和时间跨度，并说明每个判断对应的证据。
5. 对发布时间晚于 as_of 的内容不得用于结论；无法确认发布时间就标记 unknown。

工具规则：
- 先用 web_search 提交 2-4 个互补查询。
- 只对最终需要引用的 URL 调用 web_fetch。
- 搜索失败、页面被拦截、正文截断或缺少发布时间，必须写入 data_quality。
- 不要使用 bash，不要修改文件，不要把情绪转换成买卖建议。

最终只输出 JSON：
facts, entities, event_type, direction, impact_horizon,
confidence, data_quality, evidence
```

与 FinnewsHunter 相比，这个模板保留了：

- 固定角色和分析维度；
- 正反观点校验；
- 明确的搜索需求和证据要求；
- 正文长度、发布时间和数据质量边界。

同时去掉了：

- 超长 Markdown 表格格式；
- `[SEARCH]` 文本动作协议；
- 通过正则猜测情绪字段；
- 直接输出买入、加仓、清仓等交易动作。

## 对当前工具实现的具体映射

| FinnewsHunter 做法 | 当前项目对应实现 | 建议借鉴内容 |
| --- | --- | --- |
| `NewsItem` 标准字段 | `web_search` 的 sources + `web_fetch` 的 page | 统一保存 title/source/published_at/url/content |
| 多引擎搜索 | `web_search(queries)` | 保留一次提交互补查询，读取 attempts 诊断 |
| 单篇正文抓取 | `web_fetch(url)` | 只抓最终引用 URL，记录截断状态 |
| crawler quality score | `status/error_code/content_truncated` | 将质量问题写入 `data_quality.issues` |
| jieba 清洗/关键词 | 暂不作为 Agent 工具 | 只有需要本地批处理时再单独增加 |
| `[SEARCH: ...]` | function tool-call | 不迁移文本标记，使用真实工具调用 |
| Markdown 正则解析 | JSON 字段校验 | 解析失败返回不完整状态，不静默填 neutral |

当前项目现有工具已经覆盖最重要的网络能力，真正需要新增的主要是 `news_research` prompt 和最终 JSON 的字段校验，不是 FinnewsHunter 的整套工具栈。

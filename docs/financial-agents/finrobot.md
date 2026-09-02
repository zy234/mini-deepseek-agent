# AI4Finance-Foundation/FinRobot

- 地址：https://github.com/AI4Finance-Foundation/FinRobot
- 调研快照：约 7.9k Star，Apache-2.0，最近提交 2026-08-23
- 定位：面向金融应用的 AI Agent 平台，覆盖投研、估值、交易和风险。

## 仓库实现方案

新版 FinRobot Desktop 采用 Lead Agent 编排数据、分析、建模、综合和报告角色，并增加 bull/bear/judge 辩论。核心设计是把数值计算和 LLM 叙述分开：DCF、DDM、LBO、WACC、可比公司和 Monte Carlo 由纯 Python 计算，模型只做解释、综合和报告写作。

完整产品使用 PydanticAI、FastAPI、React/Tauri 和多个数据提供方，规模明显大于当前仓库。

## 适合迁移的部分

最值得迁移的是“确定性计算、LLM 解释”边界，而不是桌面应用。建议抽取以下无状态函数：

- `returns(prices)`、`max_drawdown(equity)`、`risk_metrics(returns)`。
- `dcf(cashflows, discount_rate, terminal_growth)`。
- `factor_score(row, rules)`。
- `portfolio_risk(positions, covariance)`。

所有函数应返回数值、输入摘要、版本和警告；LLM 只能引用这些结果。

## 当前框架中的实现建议

增加一个 `financial_calc` 本地工具脚本，输入 JSON、输出 JSON，禁止依赖网络和环境密钥。Agent 先用 `bash` 调用脚本，再用中文 prompt 解释结果。对外报告中分离“计算结果”和“模型判断”，例如：`calculation`、`interpretation`、`assumptions`、`missing_data`。

DCF 等模型必须显式列出假设，不应在缺少现金流、债务或股本数据时自动补零。估值结果不直接产生订单，只能作为候选研究证据。

## 不应直接引入的部分

- PydanticAI/FastAPI/React/Tauri 全栈。
- 多供应商抽象和桌面自动更新。
- 任何未核对来源的财报数字或估值结论。

## 验证要求

用手工算例测试 DCF、回撤和组合风险；对缺失值、负现金流、极端折现率、重复日期和未来数据分别测试。报告必须保留公式版本和输入数据日期。

# Prompt 与工具源码调研（补充核验）

> 证据版本：FinRobot master commit d221910096de87579b02f8f0674652bf1a175f51（2026-08-23）；Desktop tag commit 6a8161ff5cfa66ec3df9c11a0bf7a84a1ac11f01（2026-05-11）。README（README.md:68-103）宣称有 Lead/Bull/Bear/Judge，但两个快照都没有对应类或 prompt，以下以源码为准。

# 一、Prompt 调研

## 1. AutoGen 通用 prompt

源码：finrobot/agents/prompts.py:4-41；组装：finrobot/agents/workflow.py:60-100。

Leader system prompt 原文：

    You are the leader of the following group members:
    {group_desc}
    As a group leader, you are responsible for coordinating the team's efforts to achieve the project's objectives.
    - Summarize the status of the whole project progess each time you respond.
    - End your response with an order to one of your team members to progress the project, if the objective has not been achieved yet.
    - Orders should be follow the format: "[<name of staff>] <order>".
    - Orders need to be detailed, including necessary time period information, stock information or instruction from higher level leaders.
    - Make only one order at a time.
    - After receiving feedback from a team member, check the results of the task before the next order.
    Reply "TERMINATE" in the end when everything is done.

角色 prompt：

    As a {title}, your reponsibilities are as follows:
    {responsibilities}
    Reply "TERMINATE" in the end when everything is done.

worker prompt：

    Follow leader's order and complete the following task with your group members:
    {order}
    For coding tasks, provide python scripts and executor will run it for you.
    Save your results or any intermediate data locally and let group leader know how to read them.
    DO NOT include "TERMINATE" until you have received the execution results.
    If the task cannot be done, report reasons or requirements to group leader ended with TERMINATE.

{group_desc} 在 workflow.py:432-447 拼装；{order} 在 finrobot/agents/utils.py:25-32 用正则从 [AgentName] order 提取。优点是单步推进和反馈验收；缺点是依赖文本标记，无来源、日期、置信度、JSON 或数据充分性约束。

## 2. Equity Research Agent prompt

源码目录：finrobot_equity/core/src/modules/equity_agents/。可确认的是 8 个 Agent：

- CompanyOverviewAgent：company_overview_agent.py:4-53，字段 overview。
- InvestmentOverviewAgent：investment_overview_agent.py:4-49，字段 investment_update。
- NewsSummaryAgent：news_summary_agent.py:4-60，字段 news_summary。
- ValuationOverviewAgent：valuation_overview_agent.py:4-40，字段 valuation_analysis。
- RiskAnalystAgent：risks_agent.py:18-58，字段 risk_analysis。
- CompetitorAnalysisAgent：competitor_analysis_agent.py:4-39，字段 competitive_analysis。
- MajorTakeawaysAgent：major_takeaways_agent.py:4-31，字段 takeaways。
- TaglineAnalystAgent：tagline_agent.py:4-18，字段 tagline。

它们均采用 Agent(name, instructions, output_type=PydanticModel)，但没有注册 web_search、行情或财报工具；输入来自 agent_manager.py 的文本拼接。

CompanyOverviewAgent 的结构（company_overview_agent.py:4-43）：

    [ROLE]
    You are a Foundational Research Analyst with 5 years of experience in corporate strategy and business analysis. Your primary function is to create a comprehensive and objective "tear sheet" for any given company.

    [INPUT DATA]
    You will receive a company name and its stock ticker.

    [ANALYSIS TASKS]
    1. Business Model & Strategy: business model, mission, customers and geography.
    2. Products, Services, & Revenue Streams: products, annual segment revenue and pipeline.
    3. Corporate History & Leadership: milestones and executive background; use web searches.
    4. Financial Snapshot: latest fiscal-year revenue, net income, market cap, current price and 52-week high/low.
    5. Industry & Market Context: industry and estimated market share/rank.

    [OUTPUT REQUIREMENTS]
    Produce a "Company Overview" report of 800-1000 words:
    I. Executive Summary
    II. Business Model & Corporate Strategy
    III. Revenue & Segment Analysis
    IV. Leadership & History

    FORMATTING RULES:
    - Use plain text only - no markdown symbols
    - Write in complete paragraphs, not bullet lists
    - Do not use headings, asterisks, or special characters

这里存在输入不足、要求搜索但无工具、要求标题但禁止 heading 三个问题。

InvestmentOverviewAgent 要求同比/共识比较、beat/miss、guidance、原始 thesis 验证、重大新闻、PE/PS 和 6-12 月展望；输出 Thesis Confirmed/Under Review/Broken、3-5 条 takeaways、Performance Analysis、Thesis Impact。没有共识来源、URL、quote 或发布日期字段。

NewsSummaryAgent 要求按产品、财报、监管、战略、情绪、竞争分类，评价 positive/negative/neutral、即时/长期影响，选择 3-5 条新闻；没有 URL、去重或发布时间核验。

ValuationOverviewAgent 要求同行横向、历史纵向、基本面对齐、安全边际、价值陷阱/泡沫判断，并禁止预测，分析框架值得借鉴；但 using online search results 无工具落实。

RiskAnalystAgent 覆盖 Market、Competitive、Operational、Financial、Regulatory & ESG，并要求优先分析 3-5 项风险对收入、利润、估值的影响；没有证据、概率、时间范围或触发条件。

CompetitorAnalysisAgent 要求搜索 2-3 个竞争者并判断护城河扩大/稳定/收窄，但同样没有工具配置。

MajorTakeawaysAgent 固定四段：Revenue Growth、Gross Profit Margin、SG&A Expense Margin、EBITDA Margin Stability，解释驱动、可持续性、运营杠杆、同行位置和投资含义；只是字符串格式。

## 3. User prompt、schema 和失败处理

源码：finrobot_equity/core/src/modules/equity_agents/agent_manager.py:32-111。

user prompt 字段为 company_name、company_ticker、financial_metrics、peer_ebitda、peer_ev_ebitda、company_news[].title、publishedDate、text；DataFrame 转 markdown，新闻 URL 不传入。Agent manager 通过 result.final_output 取 Pydantic 字段，取不到时退回 str(final_output)。

旧版直接 Chat Completions：finrobot_equity/core/src/modules/text_generator_agents.py:26-150。news 最多 10 篇，正文每篇只取 200 字符，没有 URL、来源、抓取时间、截止日期或未来数据检查。无 key、client/API 异常、空响应均 fallback；fallback 会在空数据时声称 strong financial fundamentals。没有 JSON schema、正则或正文证据校验。

EnhancedTextGenerator：finrobot_equity/core/src/modules/enhanced_text_generator.py:27-50、:117-388。prompt 明确要求 target price、rating、implied upside、catalysts、upgrade/downgrade triggers 和风险量化；输出普通字符串，无 schema。源码没有订单工具，因此只能确认会诱导投资建议，不能确认直接下单。

RAG prompt：finrobot/functional/rag.py:5-11。

    Below is the context retrieved from the required file based on your query.
    If you can't answer the question with or without the current context, try a more refined search query or ask for more contexts.
    Your current query is: {input_question}
    Retrieved context is: {input_context}

没有文件名、页码、URL、日期或上下文长度约束。

## 4. Prompt 可靠性

值得保留：分层结构；估值横纵比较；风险分类和优先级；thesis validation；单步派单和反馈验收。

不可靠：Use web searches 与无工具配置不一致；data references 没有 URL/quote/source_id；字数和章节无程序校验；no-markdown 与标题/粗体/bullet 冲突；TERMINATE 只是字符串；Pydantic 不验证正文数字和证据；fallback 生成无数据支持的正面结论。

# 二、工具实现调研

## 1. 注册协议

源码：finrobot/toolkits.py:10-51。使用 AutoGen register_function()，属于原生 function registration。stringify_output() 将 DataFrame 变成 DataFrame.to_string()，其他值转 str()，因此不是稳定 JSON contract。

## 2. FinnHubUtils

源码：finrobot/data_source/finnhub_utils.py。

- get_company_profile(symbol)：company_profile2；返回包含 name、finnhubIndustry、ipo、marketCapitalization、currency、shareOutstanding、country、ticker、exchange 的自然语言。
- get_company_news(symbol, start_date, end_date, max_news_num=10, save_path=None)：company_news；返回 date、headline、summary DataFrame。
- get_basic_financials_history(symbol, freq, start_date, end_date, selected_columns=None, save_path=None)：freq 仅 annual/quarterly，按日期筛选 series。
- get_basic_financials(symbol, selected_columns=None)：返回 JSON 字符串。

无 key 返回 None；无超时、重试、缓存和 URL 保留。新闻超过上限使用 random.choices，可能重复。get_basic_financials() 遍历 keys 时删除字段，selected_columns 可能触发 RuntimeError。

## 3. YFinanceUtils

源码：finrobot/data_source/yfinance_utils.py。

函数包括 get_stock_data、get_stock_info、get_company_info、get_stock_dividends、get_income_stmt、get_balance_sheet、get_cash_flow、get_analyst_recommendations。get_stock_data() 调用 ticker.history()，典型字段为 Open、High、Low、Close、Volume、Dividends、Stock Splits；get_company_info() 返回 Company Name、Industry、Sector、Country、Website。

没有显式超时、重试、缓存、URL、point-in-time 检查、统一时区或空结果分类；recommendations 只取第一行最大值。

## 4. FMPUtils

源码：finrobot/data_source/fmp_utils.py。

- get_target_price(ticker_symbol, date)：取与输入日期绝对距离不超过 999 天的 price-target，返回 min - max (md. median)。
- get_sec_report(ticker_symbol, fyear="latest")：返回 Link 和 Filing Date，latest 直接取 data[0]。
- get_historical_market_cap(ticker_symbol, date)：周末调整到下一个工作日，再取 data[0]["marketCap"]。
- get_historical_bvps(ticker_symbol, target_date)：取绝对日期距离最近的 bookValuePerShare，可能选到未来。
- get_financial_metrics(ticker_symbol, years=4)：返回 Revenue、Revenue Growth、Gross Margin、EBITDA、FCF、ROIC、EV/EBITDA、PE Ratio、PB Ratio 等 DataFrame。
- get_competitor_financial_metrics(...)：返回 {symbol: DataFrame}。

没有 timeout、重试、缓存和统一错误结构；get_financial_metrics() 重复请求 API，year_offset=0 使用 income_data[-1] 计算增长。

## 5. SECUtils

源码：finrobot/data_source/sec_utils.py。

get_10k_metadata() 按 ticker、formType=10-K、filedAt 区间查询，空结果为 None。download_10k_filing() 保存 HTML，download_10k_pdf() 流式保存 PDF，get_10k_section() 校验 section 后调用 ExtractorApi.get_section()。

get_10k_section() 使用 finrobot/data_source/.cache/sec_utils/{ticker}_{fyear}_{section}.txt，是主要工具中唯一明确的本地缓存；没有 TTL、hash、抓取时间、accession number 或正文截断。下载异常常被裸 except 转成普通失败字符串。

## 6. FinNLPUtils、RedditUtils

FinNLPUtils（finrobot/data_source/finnlp_utils.py）支持 CNBC、Yicai、InvestorPlace、Sina、Finnhub、Xueqiu、Stocktwits；通用流程是 downloader -> download_* -> dataframe[selected_columns] -> save_output -> DataFrame。定义 max_retry=5，但调用传入 {}，无法确认重试生效。注释函数不算已支持。

RedditUtils.get_reddit_posts（finrobot/data_source/reddit_utils.py:34-103）参数是 query、start_date、end_date、limit=1000、selected_columns、save_path；遍历 wallstreetbets、stocks、investing，按 UTC timestamp 过滤。基础字段是 created_utc、id、title、selftext、score、num_comments、url。保留 UTC、ID、URL 是优点，但没有跨 subreddit 去重、排序、缓存、重试、超时、质量评分或空结果/失败分类。

## 7. RAG 与回测

ragquery.py 的 earnings call 分支使用 all-MiniLM-L6-v2、chunk_size=1024、overlap=100、Chroma、quarter/speaker filter、similarity_search(k=5)，最后按 speaker 合并纯文本；SEC 分支 k=5 或 k=3，按 form/filing type 过滤。没有 URL、页码、score、文档 ID、质量阈值或错误分类。代码推断：FROM_MARKDOWN=True 分支使用未初始化的 emb_fn，可能 NameError。

BackTraderUtils.back_test（finrobot/functional/quantitative.py:38-167）用 yfinance + backtrader，返回 Starting Portfolio Value、Final Portfolio Value、Sharpe Ratio、Drawdown、Returns、Trade Analysis 的 pformat 字符串；没有交易成本、滑点、数据 hash、point-in-time/未来数据检查或稳定 JSON；支持 module:Class 动态 import。

## 8. 估值真实性

valuation_engine.py:70-282 确实有 EV/EBITDA、peer comparison、简化 DCF，但缺少 FCF 时使用 EBITDA * 0.6，净债务使用企业价值 10%，缺少历史倍数时默认 12.0 和标准差 3.0，DCF 默认增长率 10%/5%、terminal growth 2.5%、WACC 10%、10 年。因此只能确认有 Python 估值代码，不能确认缺失数据不自动补零；完整 DDM、LBO、Monte Carlo 没有源码证据。

# 三、对当前项目的适配建议

直接借鉴：分层 prompt、估值横纵比较、风险分类、thesis 验证、单步验收。

映射：web_search({queries}) 发现候选，web_fetch({url}) 核验单篇；observation 保留 URL、标题、发布时间、抓取时间、正文和截断状态；JSON trajectory 保留调用和证据链。

研究角色最终输出增加：subject、as_of、data_cutoff、data_sufficiency、conclusion、bull_case、bear_case、risks、counterfactual_checks、confidence、recommendation、claims、tool_errors。claim 至少包含 claim、value、source_url、title、published_at、quote、evidence_type。研究角色的 recommendation 只允许 research_only/watch/insufficient_data；组合与交易角色通过独立的 MiniQMT 工具提出、预检或提交操作，不能把研究文本直接当作订单。

新增代码：最终 JSON schema；证据标准化和 URL 去重；截止日期/未来数据检查；empty、network_error、blocked、http_error、parse_error、invalid_argument、stale_or_future_data 分类；缺失关键数据时输出 insufficient，不填正面 fallback 或默认 neutral。

不值得引入：AutoGen 文本派单正则、DataFrame 字符串 contract、任意动态 import、每次建库 RAG、无来源摘要、绕过规则直接调用券商的执行工具和完整桌面栈。

最小顺序：扩展显式工具注册/分发 -> MiniQMT 只读工具 -> 中文研究与组合角色 -> web_search/web_fetch 证据流 -> `financial_calc` -> 研究记录 -> 订单预检 -> 受控交易工具；暂不做复杂 RAG 和 Agent 间自由委派。

# 四、`financial_research` 角色 Prompt 草案

    system_template: |
      你是严谨的中文金融研究助手，只负责研究、证据和风险分析，不下单、不生成交易执行指令。
      先明确 subject、as_of、data_cutoff，检查输入是否充分。
      需要网络资料时先调用 web_search，再对选中的单个 URL 调用 web_fetch。
      每条重要结论记录 source_url、title、published_at、抓取时间和 quote。
      来源发布时间不得晚于 as_of；无法确认时写 unknown，不把标题或搜索摘要当作正文证据。
      分别输出 bull_case、bear_case、risks 和 counterfactual_checks。
      empty、network_error、blocked、http_error、parse_error 必须分别记录。
      缺失关键数据时 data_sufficiency=insufficient，不使用默认数字、默认 neutral 或正面 fallback。
      最终只输出 JSON：subject、as_of、data_cutoff、data_sufficiency、conclusion、bull_case、bear_case、risks、counterfactual_checks、confidence、recommendation、claims、tool_errors。
      recommendation 只能是 research_only、watch 或 insufficient_data。

# 五、结论

最值得借鉴：分层结构；估值横纵比较；风险分类；thesis 验证；单步反馈验收。

最值得借鉴的工具机制：原生 function registration；SEC section 本地缓存；Reddit UTC 过滤；RAG metadata filter；selected_columns 控制返回宽度。

不建议照搬：文本正则协议、DataFrame 字符串结果、自然语言错误、999 天窗口、未来日期匹配、默认估值假设、正面 fallback、动态 import、无来源新闻。

当前项目最小方案：保留 `DefaultAgent`、DeepSeek tool-call、`LocalEnvironment`、结构化 observation 和 JSON session；显式新增 MiniQMT 查询/行情/计算/记录/交易工具，并通过 YAML 配置研究、组合和交易等独立角色。所有交易工具内部复用相同 SafetyKernel，不能依赖 prompt 实现权限控制。

仍需核验：Desktop 二进制内部是否有未提交的 Lead/Bull/Bear/Judge；PydanticAI 版本真实 tool contract；13 章报告与 Agent 对应关系；数据供应商限流和 FinNLP max_retry 是否实际生效。

# 六、结合 MiniQMT 的工具化个人账户 Agent 方案

## 目标与非目标

目标不是把 FinRobot Desktop 搬进 MiniQMT，而是基于当前 `DefaultAgent + DeepSeek tool-call + LocalEnvironment + YAML profile` 扩展一个或多个可独立运行的金融 Agent。账户、行情、研究、计算、委托和成交能力全部以 host-owned 工具提供：

- Agent 调用账户工具读取资产、现金、持仓和可卖数量，而不是由 CLI 预先拼接上下文。
- Agent 调用行情、网页和确定性计算工具完成基本面、估值、风险及牛熊研究。
- Agent 调用委托预检、提交、撤单、委托查询和成交查询工具管理账户。
- 每个工具内部校验账户范围、数据时间、交易制度、仓位、审批、幂等和审计，模型不能绕过。
- 单 Agent 可独立完成完整闭环；多个 Agent 也能以不同 YAML profile 独立启动，通过版本化记录衔接。

第一阶段不做自动转账、融资融券、期权或跨券商账户聚合。所谓“模型下单”是调用受控的 `miniqmt_order_submit` 工具；该工具不是裸 Bridge 代理，它必须在内部执行完整规则并可能拒绝调用。运行模式分为 `observe`（不暴露写工具）、`propose`（提交工具只生成待审批记录）、`auto_execute`（规则通过后允许发给 Bridge），默认使用 `observe`。

## 现有 MiniQMT 能力与接入点

已存在的接口足以支撑一个窄版本账户 Agent，不需要引入 FinRobot 的 FastAPI/React/Tauri 全栈：

| 需求 | 建议工具 | 当前 MiniQMT 接口/数据 | 工具内部规则 |
|---|---|---|---|
| 现金与总资产 | `miniqmt_account_snapshot` | `GET /portfolio/assets` | 绑定配置账户，脱敏，查询失败不能当作 0 元 |
| 持仓与 T+1 | `miniqmt_positions` | `GET /portfolio/positions`、`can_use_volume` | 规范股票代码和数值，保留可卖数量及快照时间 |
| 实时行情 | `miniqmt_quotes` | Bridge/主后端批量行情接口 | 限制代码数量，标明行情时间、停牌和陈旧状态 |
| 历史行情 | `miniqmt_market_history` | `market_snapshot()`、`daily_history()` | `end_day <= as_of`；回测时严格早于执行日 |
| 市场概览 | `miniqmt_market_snapshot` | 股票池、指数、日特征 | 一次批量返回紧凑截面，禁止逐股全图调用 |
| 委托与成交 | `miniqmt_orders`、`miniqmt_trades` | `GET /trading/orders`、`GET /trading/trades` | 统一状态和事件 ID，不把查询失败表示为空列表 |
| 下单预检 | `miniqmt_order_preview` | `OrderIntent` -> `OrderRequest` | 只计算可执行量、价格范围和阻断理由，不提交 |
| 下单 | `miniqmt_order_submit` | `ExecutionRequest` -> Bridge | 模式、审批、lease、T+1、限额、幂等均通过才提交 |
| 撤单 | `miniqmt_order_cancel` | `POST /trading/orders/{id}/cancel` | 只允许当前绑定账户且处于可撤状态的委托 |
| 计算与记录 | `financial_calc`、`research_record_*` | 本地确定性代码、JSONL/SQLite | 公式版本、输入 hash、原子写入和敏感字段过滤 |
| 回测 | `miniqmt_backtest_run/status/result` | 日级回测服务 | 固定策略画像和点时数据，真实账户模式禁用 |

需要补齐的是一组显式 MiniQMT 工具和共用的安全内核，不是另造交易环境。当前 `AccountSnapshot` 只有现金和 `raw`，工具适配层应输出稳定字段：`total_asset`、持仓市值、负债（如有）、快照时间、来源和查询状态；不把不稳定券商 `raw` 原样交给模型。

## 推荐架构

```text
mini --agent portfolio_manager
        |
        v
DefaultAgent <--> DeepSeek tool-call
        |
        v
LocalEnvironment 显式工具分发
        |
        +--> MiniQMTReadTools  --> 主后端 / Bridge / 行情缓存
        +--> FinancialTools    --> 确定性计算 / web_search / web_fetch
        +--> RecordTools       --> ResearchRecord / DecisionRecord
        +--> MiniQMTWriteTools --> SafetyKernel --> MiniQMT 执行链
                                      |
                                      +--> accepted
                                      +--> blocked
                                      +--> approval_required
                                      +--> unknown（禁止自动重试）
```

模型通过工具处理结构化快照、计算结果和网页证据。`financial_calc` 负责数值计算；模型不能自行补齐缺失现金流或把搜索摘要当成事实。账户查询和交易提交都表现为模型可调用工具，但真正读写 MiniQMT、持有 API key、选择绑定账户和执行安全规则的仍是宿主代码。

## 当前框架的最小改造点

保持当前显式结构，不增加 provider 抽象、动态类加载或 LangGraph：

1. 在 `actions_toolcall.py` 增加固定的 MiniQMT/金融工具 schema，并把硬编码工具名和逐工具参数检查扩展为显式表。
2. 在 `LocalEnvironment.execute()` 增加逐工具分发；MiniQMT HTTP、认证、规范化和安全规则放入新的 `environments/miniqmt.py`，确定性计算放入 `environments/financial_calc.py`。
3. `deepseek.yaml` 增加独立角色。每个角色通过 `tools` 白名单获得最小能力集合；`single_shot` 仍不允许工具。
4. 敏感配置由环境读取，例如 `MINIQMT_BASE_URL`、`MINIQMT_API_KEY`、`MINIQMT_ACCOUNT_ID`、`MINIQMT_AGENT_MODE`。API key 不进入 tool schema、prompt、trajectory 或错误正文。
5. MiniQMT 工具统一返回 JSON observation，至少包含 `ok`、`status`、`as_of`、`source`、`data`、`warnings`、`error`、`audit_id`；不使用自然语言字符串混合成功和失败。
6. 保持当前一次运行一个角色的 CLI 语义。多个 Agent 分别以 `mini --agent <role>` 启动，先通过持久化记录协作，不增加隐式 Agent delegation。

## 工具设计原则

- **所有外部能力工具化**：账户、行情、财务、新闻、计算、记录、回测和交易都不能由 prompt 假装已经存在。
- **能力和权限分离**：tool schema 描述 Agent 能提出什么；SafetyKernel 决定当前账户、模式和状态下能否执行。
- **工具内确定性安全**：重要规则必须是 Python 校验，不能只写在 system prompt 中。
- **读写工具分离**：只读角色根本看不到 submit/cancel；交易角色即使看到写工具，也可能被工具内部规则拒绝。
- **参数窄化**：模型传股票、方向、目标权重/数量提示和理由；账户 ID、API key、profile hash、最终价格/数量由工具绑定或计算。
- **结构化失败**：`empty`、`blocked`、`approval_required`、`network_error`、`http_error`、`parse_error`、`stale_data`、`unknown` 分开返回。
- **可重放**：写工具在执行前保存规范化输入、快照 hash、规则版本、决策 ID 和审批模式；相同幂等键不能重复下单。

## 关键工具契约草案

查询工具应按用途设计参数，避免一个万能 `miniqmt_request(method, path, body)` 绕开校验：

```text
miniqmt_account_snapshot() -> 资产、现金、持仓汇总、快照时间、查询状态
miniqmt_positions(stock_codes?) -> 持仓数量、可卖数量、成本、市值、浮动盈亏
miniqmt_quotes(stock_codes[]) -> 最新价、涨跌停、停牌状态、行情时间
miniqmt_market_history(stock_codes[], start_day, end_day, fields[]) -> 点时历史数据
miniqmt_orders(status?, since?) -> 规范化委托状态
miniqmt_trades(since?) -> 规范化成交事件
```

`miniqmt_order_preview` 和 `miniqmt_order_submit` 共享同一个窄输入：

```json
{
  "stock_code": "600000.SH",
  "side": "SELL",
  "target_weight": 0.05,
  "quantity_hint": null,
  "limit_price_hint": null,
  "reason": "组合集中度超过限额",
  "research_record_id": "research-...",
  "client_intent_id": "intent-..."
}
```

`target_weight` 与 `quantity_hint` 最多提供一个，均只是意图；最终数量由工具读取最新账户/持仓/行情后确定。输入不得包含 `account_id`、API key、`mode`、`approved`、`skip_checks`、最终券商订单字段或任意 HTTP 参数。审批状态和运行模式只能由宿主配置及持久化审批记录获得。

写工具输出统一为：

```json
{
  "ok": false,
  "status": "approval_required",
  "audit_id": "audit-...",
  "decision_id": "sha256:...",
  "resolved_order": {"stock_code": "600000.SH", "side": "SELL", "volume": 500},
  "rules": [{"name": "t_plus_one", "passed": true, "detail": "可卖 1000 股"}],
  "error": null
}
```

`ok` 表示这次工具调用按其语义完成，不应把 `approval_required`、`blocked` 或 `unknown` 包装成普通成功。工具 observation 必须足够让模型解释阻断原因，但不得包含 API key、完整账户号或 Bridge 原始异常中的敏感请求头。

## FinRobot 能力迁移矩阵

| FinRobot 能力 | 迁移方式 | 不能照搬的部分 |
|---|---|---|
| Leader/worker 分工 | 先配置多个可独立运行的角色；用记录工具传递版本化产物，需要自动编排时再做显式 workflow | AutoGen 文本 `[name] order` 正则和 `TERMINATE` 标记 |
| Equity Research 八角色 | 保留职责和输出字段，合并为可配置阶段，避免一次请求制造八份无证据长文 | 无工具却要求“搜索”、无 URL/日期的自然语言结果 |
| Bull/Bear/Judge | 独立角色读取同一 `ResearchRecord`，分别写入 bull/bear/review 记录；组合角色综合后才能调用交易预检 | 用聊天文本隐式传递状态或跳过安全工具直接下单 |
| DCF、可比公司、风险指标 | 作为本地 `financial_calc` 的确定性函数，输出公式版本、输入 hash、警告 | 缺数据时使用 EBITDA*0.6、默认 WACC/增长率或其它隐含数字 |
| 新闻、SEC、RAG | 复用当前 `web_search` -> `web_fetch`，保存 URL、标题、发布时间、quote、抓取时间 | 不保存来源的摘要、动态 import 和无质量阈值 RAG |
| 回测 | 使用 MiniQMT 日级事件回测复验建议，不引入 Backtrader 作为第二套撮合器 | 美股数据源和无成本/滑点的结果字符串 |

## 账户 Agent 的数据契约

建议新增独立版本化 JSON，而不是扩展 `OrderIntent` 承载研究全文。下面是最小形状：

```json
{
  "schema": "account-research/v1",
  "account_id_hash": "sha256:...",
  "as_of": "2026-09-02T15:05:00+08:00",
  "data_cutoff": "2026-09-01",
  "snapshot_hash": "sha256:...",
  "account": {
    "available_cash": 100000.0,
    "total_asset": 250000.0,
    "positions": [
      {"stock_code": "600000.SH", "volume": 1000, "can_use_volume": 1000,
       "market_value": 12000.0, "avg_cost": 11.8}
    ]
  },
  "calculations": {
    "gross_exposure": 0.60,
    "largest_position_weight": 0.12,
    "max_drawdown": -0.08,
    "warnings": []
  },
  "recommendation": "watch",
  "proposed_intents": [],
  "claims": [],
  "tool_errors": []
}
```

约束：

- 账户 ID 只用于宿主查数和审计，给模型的记录使用 hash 或内部别名；日志不得记录交易密钥。
- `account`、`calculations`、`claims` 和 `tool_errors` 均区分缺失、空结果、网络失败、阻断、HTTP 错误和解析错误。
- `claims` 至少包含 `claim`、`source_url`、`title`、`published_at`、`quote`、`evidence_type`；没有证据的判断标记为假设。
- `proposed_intents` 只允许 `BUY`/`SELL`、目标权重或数量提示、理由和研究记录 ID，不允许 API key、券商客户端或执行方法。
- 研究记录本身不等于订单。组合或交易角色必须把建议显式传给 `miniqmt_order_preview`，再根据返回的安全结论决定是否调用 `miniqmt_order_submit`。

写工具的调用和结果另存 `account-action/v1`：

```json
{
  "schema": "account-action/v1",
  "audit_id": "...",
  "tool": "miniqmt_order_submit",
  "mode": "propose",
  "stock_code": "600000.SH",
  "side": "SELL",
  "requested": {"target_weight": 0.05},
  "resolved": {"price_type": "LATEST", "volume": 500},
  "decision_id": "sha256:...",
  "research_record_id": "...",
  "status": "approval_required",
  "rules": [{"name": "t_plus_one", "passed": true}],
  "created_at": "2026-09-02T10:30:00+08:00"
}
```

`accepted` 只表示 Bridge 明确接受，不表示成交；`unknown` 表示提交结果不可确认，工具必须冻结相同幂等键并要求后续通过查询工具核对。

## 独立 Agent 角色

第一版建议提供三个独立 profile，而不是实现 Agent 间自由委派：

| 角色 | 主要职责 | 工具集合 |
|---|---|---|
| `financial_research` | 市场/个股资料、估值、风险、牛熊论证和证据记录 | `miniqmt_market_*`、`miniqmt_quotes`、`financial_calc`、`web_search`、`web_fetch`、`research_record_*` |
| `portfolio_manager` | 查看账户与持仓，计算组合风险，形成调仓建议并预检 | 上述工具 + `miniqmt_account_snapshot`、`miniqmt_positions`、`miniqmt_orders`、`miniqmt_trades`、`miniqmt_order_preview` |
| `account_trader` | 执行已研究/已批准的订单，查询状态和撤单 | 账户/订单查询 + `research_record_get`、`miniqmt_order_preview`、`miniqmt_order_submit`、`miniqmt_order_cancel` |

需要单 Agent 时再增加 `financial_manager`，暴露上述完整工具集合并使用严格 system prompt；安全性不能依赖该 prompt，而依赖每个工具内部相同的 SafetyKernel。多个角色对同一账户写操作时必须共享 MiniQMT 账户 lease 和幂等存储。

对应当前配置结构的草案：

```yaml
agents:
  financial_research:
    description: 金融资料、估值与风险研究
    flow: iterative
    tools:
      - miniqmt_market_snapshot
      - miniqmt_market_history
      - miniqmt_quotes
      - financial_calc
      - web_search
      - web_fetch
      - research_record_get
      - research_record_put

  portfolio_manager:
    description: 个人账户分析与调仓预检
    flow: iterative
    tools:
      - miniqmt_account_snapshot
      - miniqmt_positions
      - miniqmt_orders
      - miniqmt_trades
      - miniqmt_quotes
      - financial_calc
      - research_record_get
      - research_record_put
      - miniqmt_order_preview

  account_trader:
    description: 执行已研究并通过安全规则的账户操作
    flow: iterative
    tools:
      - miniqmt_account_snapshot
      - miniqmt_positions
      - miniqmt_orders
      - miniqmt_trades
      - research_record_get
      - miniqmt_order_preview
      - miniqmt_order_submit
      - miniqmt_order_cancel
```

这些 profile 是独立入口，不是同一会话内的动态子 Agent。自动化运行由一个很薄的宿主调度器按交易日/订单事件启动指定角色，并把上一步 `record_id` 放进任务；研究、查询、记录和交易仍全部通过工具完成。

## 账户管理生命周期

1. **开盘前快照**：Agent 调用 `miniqmt_account_snapshot`、`miniqmt_positions` 和 `miniqmt_orders`；工具检查查询时间、字段完整性和内部一致性。快照失败则交易工具锁定新买入。
2. **市场级研究**：Agent 调用 `miniqmt_market_snapshot` 做一次批量截面，再对已有持仓和实际候选调用行情/研究工具；不对 5000 多只股票逐一运行完整 Agent。
3. **组合分析**：Agent 把工具返回的稳定数据传给 `financial_calc`，计算现金占比、集中度、回撤、波动、换手和风险限额。
4. **建议与反方检查**：Agent 使用 `research_record_put` 保存继续持有/减仓/观察理由、牛熊论证、触发条件和数据充分性。
5. **订单预检**：组合或交易 Agent 调用 `miniqmt_order_preview`；工具解析目标权重/数量提示并返回最终可执行量、价格类型和全部规则结果。
6. **审批与提交**：交易 Agent 调用 `miniqmt_order_submit`。`observe` 返回 blocked；`propose` 返回 approval_required；`auto_execute` 在 lease、画像、审批和规则通过后才提交 Bridge。
7. **成交和复盘**：Agent 调用委托/成交查询工具跟踪状态；工具把 Bridge 结果归一化为事件。`unknown` 结果不自动重试，收盘记录资产曲线、成交归因和快照 hash。

## SafetyKernel（必须在工具内部调用）

- **账户范围**：账户 ID、策略 profile hash 和运行主机固定配置；默认 tool schema 不接受账户 ID，多账户模式只接受配置中的逻辑别名。
- **数据完整性**：资产、持仓、委托、成交不是原子查询；时间戳不一致或明显矛盾时跳过周期。
- **仓位约束**：最大总仓位、单票/行业权重、现金下限、单日买入金额和订单数量上限。
- **交易制度**：A 股 T+1、可用持仓、整手数量、价格精度、涨跌停、停牌和交易日校验。
- **状态安全**：相同 `decision_id` 不重复执行；提交超时或结果未知绝不自动重发；成交 ID 缺失时停止有状态自动交易。
- **模型失败**：格式错误、工具失败、来源晚于 `as_of`、低数据充分性或牛熊冲突时降级为观察，不使用正面 fallback。
- **人工控制**：提供全局 kill switch、单账户暂停、单票黑名单和每次写工具调用的审计事件。
- **权限最小化**：运行角色的工具白名单和工具内部模式双重校验；不能只靠隐藏工具名或 prompt 禁止。

## 分阶段迁移计划

### M0：MiniQMT 只读工具和研究 Agent

新增账户、持仓、委托、成交、行情、历史、市场截面等只读工具，以及 `account-research/v1`、快照脱敏/哈希、`financial_calc` 和记录工具。配置 `financial_research` 与 `portfolio_manager` 两个可独立运行角色。

验收：mock 账户数据、空账户、字段缺失、查询异常、重复日期、未来数据和计算极值均有测试；记录不包含账户密钥或完整账户号。

### M1：订单预检和建议闭环

增加 `miniqmt_order_preview`，让研究记录挂到 candidate manifest 和 `OrderRequest.diagnostics`。预检工具返回完整规则结果，但不提交订单；回测输入和实盘输入使用同一快照编码。

验收：历史日期回放不泄漏执行日数据；候选代码过滤、来源日和研究记录 ID 可追溯；Agent 故障不会影响已有持仓的正常风控处理。

### M2：受控交易工具的 propose 模式

增加 `miniqmt_order_submit`、`miniqmt_order_cancel` 和 `account_trader` 角色。submit 在 `propose` 模式只写入待审批信号，复用现有 `strategy_live_signals`、`strategy_live_orders` 和账户日视图；审批只接受宿主生成的 signal ID。

验收：审批过期、重复审批、账户 lease 冲突、撤单和部分成交均可重放；未知 Bridge 结果不会重复报单。

### M3：小额自动执行

每账户单独显式开启 `auto_execute`，先只允许减仓/风控类 SELL 或极小额 BUY。Agent 仍调用同一个 `miniqmt_order_submit`，由工具内部 SafetyKernel 决定是提交、阻断还是要求审批；Agent 不直接构造 HTTP 请求。

验收：先用 paper/backtest，再用模拟 Bridge；连续运行日志、kill switch、限额和人工核验流程通过演练后，才考虑扩大范围。

## 不建议的迁移方式

- 复制 FinRobot 的 AutoGen、PydanticAI、Backtrader 和桌面 UI，造成第二套 Agent、账户和撮合生命周期。
- 把 `AccountSnapshot.raw` 原样返回给模型，泄漏账户标识并让模型依赖不稳定券商字段。
- 把裸 `order_stock` 或通用 HTTP 请求暴露为工具；交易工具必须使用窄参数并强制经过 SafetyKernel。
- 用估值默认参数填充缺失财务数据，再把结果当成个人账户的自动交易依据。
- 为全市场每只股票启动一次多 Agent 研究；应先用本地硬过滤和一次市场级研究缩小范围。

## 待确认事项

1. 首个交付是否同时包含 `financial_research`、`portfolio_manager` 两个只读角色，还是先交付一个完整只读 `financial_manager`。
2. MiniQMT Bridge 是否能提供稳定的账户快照版本号、成交 `trade_id` 和停牌/涨跌停字段；不能提供时必须保守降级。
3. 账户研究记录保存在现有 MiniQMT SQLite 还是独立 append-only JSONL；建议先 JSONL + hash，稳定后再建表索引。
4. 研究数据供应商和网页抓取是否允许进入实盘运行；网络失败时应只保留本地行情和账户风险分析。
5. `propose` 的人工审批入口和有效期，以及 M3 是否先只允许 SELL/风控单。

本分支当前只提交调研与迁移规格，不修改 `miniqmt-portfolio` 交易代码，也不打开真实账户自动交易。

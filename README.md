# Mini DeepSeek Agent

这是一个围绕 DeepSeek 构建的小型 Agent：模型固定为 `deepseek-v4-flash`，不同角色可以配置独立的中文 prompt、工具集合和执行流程。宿主提供 Bash、文件编辑、网页证据、确定性金融计算和受限 MiniQMT 接口。

## 安装

```bash
python3 -m pip install -e .
```

设置 DeepSeek Key：

```bash
export DS_KEY="your-key"
```

## 运行

```bash
mini
```

交互终端会先显示 Agent 列表，选择角色后再输入任务。也可以显式指定角色，适合脚本和非交互运行：

```bash
mini --agent interactive -t "修复当前项目中的测试失败，并运行相关测试"
```

默认工作目录是启动命令所在目录。常用参数：

```bash
mini --agent single_call
mini --config path/to/agent.yaml
mini --output trajectory.json
mini --step-limit 20
mini --timeout 60
```

角色定义保存在 `src/minisweagent/config/deepseek.yaml` 的 `agents` 节点。每个角色可以配置自己的 prompt、工具和执行流程；新增同类角色只需增加配置。

当前内置角色：

- `interactive`：通用交互角色，可持续对话并按需使用工具。
- `single_call`：单次调用角色，不使用工具。
- `financial_research`：查询行情、核验网页证据并执行确定性金融计算，不访问账户。
- `portfolio_manager`：只读查询个人账户、行情和组合风险，不执行交易。
- `account_trader`：查询个人账户和行情，按宿主安全配置提交或撤销委托。

项目提供 Bash、文件编辑、网页搜索和网页抓取能力。运行环境直接使用当前用户权限，请勿在不可信目录或任务中运行。

MiniQMT 工具直接连接 Bridge，默认地址为 `http://127.0.0.1:8023`（可用 `MINIQMT_BRIDGE_URL` 修改），个人账户由宿主环境绑定：

```bash
export MINIQMT_ACCOUNT_ID="your-account-id"
export MINIQMT_BRIDGE_API_KEY="your-api-key"
export MINIQMT_AGENT_MODE="observe"         # 默认值，禁止提交和撤单
mini --agent portfolio_manager
```

需要交易时显式设置 `MINIQMT_AGENT_MODE=execute` 并启动 `account_trader`。每次交易仍会在终端请求人工审批，单笔数量默认不超过 10000 股，可由宿主通过 `MINIQMT_MAX_ORDER_VOLUME` 收紧或调整。账户号和 API key 不进入模型参数、会话记录或 Bash 子进程；`accepted` 只表示接口接受委托，不表示成交。提交结果为 `unknown` 时禁止自动重试，必须先查询委托和成交。

金融 Agent 调研记录见 [`docs/financial-agents/`](docs/financial-agents/README.md)，其中每个候选仓库都有独立的架构分析、许可证/依赖注意事项和迁移实现建议。

## 架构

```text
CLI
 ├── 读取角色配置并选择 Agent
 └── Agent
     ├── Flow
     │   ├── interactive：模型 ↔ 工具 ↔ 观察，循环执行
     │   └── single_call：模型单次生成结果
     ├── DeepSeek Model：负责模型请求和响应解析
     └── Local Environment：负责工具执行和结果反馈
```

角色配置决定 prompt、可用工具和 Flow；模型负责决策，环境负责执行，Agent 负责维护对话和流程状态。

## API

- [DeepSeek API](https://api-docs.deepseek.com/)
- [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion)
- [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls)

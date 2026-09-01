# DeepSeek Bash Agent

这是一个从 mini-SWE-agent 精简出来的单模型 Agent 框架：模型固定为 `deepseek-v4-flash`，不同角色可以配置独立的中文 prompt、工具集合和执行流程。宿主提供 `bash`、`str_replace_editor`、`web_search` 和 `web_fetch` 四个工具，网页能力由仓库自带的零 Key 实现提供。

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

项目提供 Bash、文件编辑、网页搜索和网页抓取能力。运行环境直接使用当前用户权限，请勿在不可信目录或任务中运行。

## API

- [DeepSeek API](https://api-docs.deepseek.com/)
- [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion)
- [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls)

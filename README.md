# DeepSeek Bash Agent

这是一个从 mini-SWE-agent 精简出来的单模型 Agent 框架：模型固定为 `deepseek-v4-flash`，唯一工具是 `bash`，文件读取、修改、测试都通过 Bash 完成。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

设置 DeepSeek Key：

```bash
export DS_KEY="your-key"
```

## 运行

```bash
mini -t "修复当前项目中的测试失败，并运行相关测试"
```

默认工作目录是启动命令所在目录。可选参数：

```bash
mini --config path/to/agent.yaml
mini --output trajectory.json
mini --step-limit 20
mini --timeout 60
```

## 架构

```text
CLI
 └── DefaultAgent
      ├── DeepSeekModel
      │    └── 唯一工具：bash
      ├── LocalEnvironment
      │    └── subprocess 执行命令
      └── trajectory.json
```

- `src/minisweagent/agents/default.py`：维护消息、调用次数、步数限制和完成状态。
- `src/minisweagent/models/deepseek_model.py`：通过 OpenAI SDK 请求 DeepSeek，解析 Bash tool call。
- `src/minisweagent/environments/local.py`：在本地工作目录执行 Bash，并捕获输出、返回码和超时。
- `src/minisweagent/models/utils/actions_toolcall.py`：定义唯一的 `bash` 工具及 tool 结果消息。
- `src/minisweagent/config/deepseek.yaml`：系统提示词、任务提示词和运行参数。

Agent 通过一条 Bash 命令输出完成标记和最终报告：

```bash
printf '%s\n' 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT' '最终报告'
```

第一行必须是完成标记，后续 stdout 会作为最终提交结果保存到轨迹中。

## 安全边界

`LocalEnvironment` 直接执行当前用户权限下的 Bash，不是沙箱。不要在不可信任务上使用；需要隔离时，应在这个环境实现外层容器或工作目录策略。

## API

- [DeepSeek API](https://api-docs.deepseek.com/)
- [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion)
- [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls)

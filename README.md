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

运行期间会流式显示模型思考、回复、Bash 工具调用和工具结果。当前请求使用 `tool_choice: auto`，
以兼容 DeepSeek thinking mode；模型可以直接回答，只有确实需要本地操作时才调用 Bash。
工具 stdout 最多保留 1000 个字符，超过后截断；DeepSeek SDK 和单次 API 请求的超时均为 60 秒。
`--timeout` 只控制 Bash 命令的执行时限。

## Bash 安全边界

`LocalEnvironment` 会移除传给子进程及提示词模板的 API Key、Token、Secret 和 Password 类环境变量。
Bash 命令通过 `bashlex` 解析为语法树：`ls`、`rg`、`cat`、非原地 `sed` 和只读 Git 子命令等明确的
只读操作直接执行；复合命令中的每个子命令都需要通过只读检查。重定向写入、未知程序、脚本执行、测试、
网络访问、依赖安装、编辑和删除等操作会暂停 Agent，并在终端等待 `y/N` 确认。

用户拒绝后直接结束本次 Agent，拒绝信息不会作为工具结果再次发送给模型。提权、关机、磁盘写入以及
删除根目录、主目录或当前目录始终禁止，不能通过审批放行。

这些规则只是本地防护，不是完整沙箱；Bash 仍以当前用户身份运行。处理不可信任务时，应在独立工作目录、
低权限账户或操作系统级沙箱中运行，且不要在工作环境中保留不必要的凭据。

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

# DeepSeek Bash Agent

这是一个从 mini-SWE-agent 精简出来的单模型 Agent 框架：模型固定为 `deepseek-v4-flash`，提供 host-owned 的 `bash`、`str_replace_editor`、`web_search` 和 `web_fetch` 四个工具。网页能力由仓库自带的零 Key 实现提供。

## 安装

```bash
python3 -m pip install -e .
```

设置 DeepSeek Key：

```bash
export DS_KEY="your-key"
```


未指定 `--output` 且配置未设置 `agent.output_path` 时，每次 CLI 会话会以 UTF-8 JSON 保存到当前目录的 `.sessions/YYYYMMDD/`，中文直接显示，文件名格式为 `YYYYMMDD-HHMMSS-microseconds-random.json`。轨迹包括会话 ID、开始时间、启动目录、完整消息、工具调用与结果、模型配置、环境配置和最终状态，便于后续排查。显式 `--output` 或 YAML 中的 `agent.output_path` 会覆盖这个默认路径。

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

运行期间会流式显示模型思考、回复、工具调用和工具结果。当前请求使用 `tool_choice: auto`，
以兼容 DeepSeek thinking mode；模型可以直接回答，只有确实需要本地操作或当前网络信息时才调用工具。
终端默认按思考、工具调用和工具结果分段显示；Bash、文件编辑、网页搜索和网页抓取调用会显示可读的参数，不直接输出粘连的 JSON。每段超过 1000 个字符时只显示摘要，运行期间不会询问是否展开；当前轮结束后输入 `/open` 可主动查看完整内容。该限制只影响 CLI 显示，不影响模型收到的工具结果。DeepSeek SDK 和单次 API 请求的超时均为 60 秒。

交互终端中，一轮任务完成后可以直接继续提问，Agent 会保留当前会话的消息和工具上下文；输入 `/exit` 结束会话。通过管道或非交互环境运行时仍只执行一轮，不会等待输入。

交互输入使用 `prompt-toolkit`，不依赖用户手动配置 `LC_ALL` 才能正确编辑中文。它负责 Unicode 字符宽度、中文退格和终端重绘；终端本身仍应使用 UTF-8 编码。
`--timeout` 只控制 Bash 命令的执行时限。

## 网页搜索

`web_search` 完全由本仓库实现，不动态加载外部 skill，也没有额外 Python 依赖。默认并发使用 `bing_rss`、`baidu_html`、`sogou_html` 和 `duckduckgo` 四个零 Key 引擎，跨引擎、跨查询按规范化 URL 去重。搜索结果包含标题、摘要、来源引擎、抓取时间和 URL；抓取时间不是新闻发布时间，最终答复应引用 URL 并自行核验内容和发布时间。

可在 YAML 中调整 `web_search_engines` 和 `web_search_max_results`；空列表会回退到默认引擎，未知引擎会返回配置错误。每个查询对每个引擎只请求一次，不进行隐藏重试；工具结果会记录每个查询、每个引擎的 `success`、`empty`、`blocked`、`http_error`、`network_error` 或 `parse_error` 状态。所有引擎失败时工具返回 `WEB_SEARCH_UNAVAILABLE`，而不是伪装成“未找到结果”；提示词要求模型据此停止相近关键词的盲目重试。

`web_fetch` 用于查看具体来源：模型先从 `web_search` 选择一个 URL，再调用 `web_fetch`。它返回页面标题、可识别的发布时间、正文文本和正文是否截断；单次只允许抓取一个 `http/https` URL。网页正文受站点登录、反爬、动态渲染和响应大小限制影响，失败时返回明确的错误码和原因。

## Bash 安全边界

`LocalEnvironment` 会移除传给子进程及提示词模板的 API Key、Token、Secret 和 Password 类环境变量。
Bash 命令通过 `bashlex` 解析为语法树：`ls`、`rg`、`cat`、非原地 `sed` 和只读 Git 子命令等明确的
只读操作直接执行；复合命令中的每个子命令都需要通过只读检查。写入 `/dev/null` 或 `/tmp`
内路径的重定向直接执行；其他重定向写入、未知程序、脚本执行、测试、网络访问、依赖安装、
编辑和删除等操作会暂停 Agent，并在终端等待 `y/N` 确认。

用户拒绝后直接结束本次 Agent，拒绝信息不会作为工具结果再次发送给模型。提权、关机、磁盘写入以及
删除根目录、主目录或当前目录始终禁止，不能通过审批放行。

这些规则只是本地防护，不是完整沙箱；Bash 仍以当前用户身份运行。处理不可信任务时，应在独立工作目录、
低权限账户或操作系统级沙箱中运行，且不要在工作环境中保留不必要的凭据。

## 架构

```text
CLI
 └── DefaultAgent
      ├── DeepSeekModel
      │    └── 工具协议：bash、str_replace_editor、web_search、web_fetch
      ├── LocalEnvironment
      │    ├── subprocess 执行 Bash
      │    ├── 工作区内原子文件编辑
      │    ├── 仓库自带的零 Key 多引擎网页搜索
      │    └── 仓库自带的网页正文抓取
      └── .sessions/YYYYMMDD/<session-id>.json
```

- `src/minisweagent/agents/default.py`：维护消息、调用次数、步数限制和完成状态。
- `src/minisweagent/models/deepseek_model.py`：通过 OpenAI SDK 请求 DeepSeek，解析四类工具调用。
- `src/minisweagent/environments/local.py`：在本地工作目录执行 Bash，并捕获输出、返回码和超时。
- `src/minisweagent/environments/web_search.py`：执行零 Key 多引擎搜索，解析、去重并返回引擎诊断。
- `src/minisweagent/environments/web_fetch.py`：抓取单个网页并提取标题、发布时间和正文。
- `src/minisweagent/models/utils/actions_toolcall.py`：定义 Bash、编辑器、网页搜索和网页抓取工具及 tool 结果消息。
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

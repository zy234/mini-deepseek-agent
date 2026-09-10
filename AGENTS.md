# DeepSeek Bash Agent

本仓库有意实现一个小型软件工程 Agent：

- 模型：通过 DeepSeek 兼容 OpenAI 的 Chat Completions API 调用 `deepseek-v4-flash`。
- 工具：提供 `bash` 和 `str_replace_editor` 两个 host-owned 工具；Bash 用于命令执行，编辑器用于工作区内的文本文件操作。
- 环境：仅执行本地子进程和工作区内的文件编辑。
- Agent：一个迭代式的 `DefaultAgent` 循环，支持在同一会话中继续提问。
- CLI：一个 `mini` 入口和一个 YAML 配置文件。

除非有明确的具体需求，否则不要增加 provider 抽象、动态类加载、基准测试运行器、容器后端、多模态处理、文本动作协议或其他模型 API。

## 目录结构

```text
src/minisweagent/agents/default.py              Agent 循环和限制
src/minisweagent/models/deepseek_model.py       DeepSeek API 适配器
src/minisweagent/models/utils/actions_toolcall.py  Bash/editor 工具协议
src/minisweagent/environments/local.py          本地命令执行和工具分发
src/minisweagent/environments/editor.py         工作区内文本编辑和原子写入
src/minisweagent/run/mini.py                    CLI 和持续会话
src/minisweagent/run/inspect.py                 轨迹观测与角色配置服务：会话索引、调度树、执行路径、配置与 prompt 读写
src/minisweagent/run/inspect_ui.html            观测与配置前端单页，无构建、无外部依赖
src/minisweagent/utils/cli_display.py           CLI 分段、颜色和摘要展示
src/minisweagent/config/deepseek.yaml         角色装配（flow、tools、prompt 文件路径）和运行时默认配置
src/minisweagent/config/prompts/              各角色 prompt，按 `<role>.system.md` 和 `<role>.instance.md` 存放
tests/test_core.py                               核心功能测试
```

## 开发约定

- 目标 Python 版本为 3.10 或更高，并使用类型注解。
- 优先采用显式构造，不要增加工厂或兼容性垫片。
- 配置保存在 `deepseek.yaml`，角色 prompt 单独放在 `config/prompts/`；改 Agent 行为改 prompt，改安全和硬约束改工具层。密钥从 `DS_KEY` 读取。
- 委派关系只在 agent profile 里声明：主角色写 `delegates_to`，子角色写 `requires`。模型看到的 `agent_call` role 枚举、宿主的准入校验和交接顺序都读这一份，增删角色只改配置。宿主只支持一层委派，子角色再声明 `delegates_to` 会被显式拒绝。
- 永远不要序列化或记录 `DS_KEY`。
- GitHub API Token 由用户在 `~/.zshrc` 中导出为 `GITHUB_TOKEN`；非交互命令创建 PR 时需通过交互 `zsh` 加载，但不得打印、序列化或记录其值。
- 代码注释应用中文，清楚的解释为什么这么开发。
- 使用 `pytest` 编写测试，使用 `ruff` 做静态检查。只写大功能的端到端测试，不写零散单元测试。
- 模型请求使用 mock client 测试；只有明确的 smoke test 才允许发起真实 DeepSeek 请求。
- `LocalEnvironment` 不是 sandbox。任何扩大命令权限的改动都必须说明影响。
- `mini-inspect` 只绑定 127.0.0.1，可写范围只有 `--config` 指向的配置文件和它旁边的 `prompts/`；轨迹目录始终只读。轨迹里有账户持仓、账本和完整决策过程，扩大可写范围或改绑定地址前必须先和用户确认门禁方案。
- `mini-inspect` 的试跑用子进程起 `mini --agent`，装配逻辑不重复一份；默认 `miniqmt_mode=auto_execute`，也就是能真实下单。同一时刻只允许一个运行，服务退出会终止它，不留能继续下单的孤儿进程。
- 配置文件是纯数据，机器会整份重写它，所以不要在 `deepseek.yaml` 里放注释；说明写在本文件。
- 角色配置的硬约束集中在 `config/validate_agents`：写盘前跑一遍，配置错误不许拖到下一次启动。
- 轨迹用临时文件加原子替换落盘：观测端会边跑边读，覆盖写会让它读到半截 JSON。
- `str_replace_editor` 只能访问工作区路径；编辑必须经过路径校验、唯一匹配检查和原子写入。
- 开发时代码逻辑尽量精简，不要为了旧逻辑兼容，新的改动应直接使用最新 idea。
- 模糊/非必要改动，要先问用户，获得明确同意后再推进。

运行检查：

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```

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
src/minisweagent/utils/cli_display.py           CLI 分段、颜色和摘要展示
src/minisweagent/config/deepseek.yaml           Prompt 和运行时默认配置
tests/test_core.py                               核心功能测试
```

## 开发约定

- 目标 Python 版本为 3.10 或更高，并使用类型注解。
- 优先采用显式构造，不要增加工厂或兼容性垫片。
- 配置保存在 `deepseek.yaml`；密钥从 `DS_KEY` 读取。
- 永远不要序列化或记录 `DS_KEY`。
- 代码注释应用中文，清楚的解释为什么这么开发。
- 使用 `pytest` 编写测试，使用 `ruff` 做静态检查。
- 模型请求使用 mock client 测试；只有明确的 smoke test 才允许发起真实 DeepSeek 请求。
- `LocalEnvironment` 不是 sandbox。任何扩大命令权限的改动都必须说明影响。
- `str_replace_editor` 只能访问工作区路径；编辑必须经过路径校验、唯一匹配检查和原子写入。
- 开发时代码逻辑尽量精简，不要为了旧逻辑兼容，新的改动应直接使用最新 idea。
- 模糊/非必要改动，要先问用户，获得明确同意后再推进。

运行检查：

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```

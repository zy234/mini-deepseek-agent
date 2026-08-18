# DeepSeek Bash Agent

This repository intentionally implements one small software-engineering agent:

- Model: `deepseek-v4-flash` through DeepSeek's OpenAI-compatible Chat Completions API.
- Tool: one `bash` function call. File operations are performed through Bash.
- Environment: local subprocess execution only.
- Agent: one iterative `DefaultAgent` loop.
- CLI: one `mini` entry point and one YAML configuration file.

Do not add provider abstractions, dynamic class loading, benchmark runners, container backends, multimodal handling, text-action protocols, or alternate model APIs unless a concrete requirement demands them.

## Structure

```text
src/minisweagent/agents/default.py              Agent loop and limits
src/minisweagent/models/deepseek_model.py       DeepSeek API adapter
src/minisweagent/models/utils/actions_toolcall.py  Bash tool protocol
src/minisweagent/environments/local.py          Local command execution
src/minisweagent/run/mini.py                    CLI
src/minisweagent/config/deepseek.yaml            Prompts and runtime defaults
tests/test_core.py                               Focused core tests
```

## Development

- Target Python 3.10 or newer and use type annotations.
- Prefer explicit construction over factories or compatibility shims.
- Keep configuration in `deepseek.yaml`; secrets come from `DS_KEY`.
- Never serialize or log `DS_KEY`.
- Keep code comments short and limited to non-obvious behavior.
- Use `pytest` for tests and `ruff` for static checks.
- Test model requests with a mocked client; use a real DeepSeek request only for an explicit smoke test.
- `LocalEnvironment` is not a sandbox. Document any change that expands command permissions.

Run checks with:

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```

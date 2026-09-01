"""OpenAI-compatible DeepSeek V4 Flash adapter."""

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, ConfigDict

from minisweagent.models.utils.actions_toolcall import (
    format_toolcall_observation_messages,
    get_tool_definitions,
    parse_toolcall_actions,
)
from minisweagent.utils.cli_display import StreamRenderer, render_tool_actions
from minisweagent.utils.serialize import recursive_merge

logger = logging.getLogger("minisweagent.model")
MODEL_NAME = "deepseek-v4-flash"
BASE_URL = "https://api.deepseek.com"
DEFAULT_API_TIMEOUT_SECONDS = 60.0

DEFAULT_OBSERVATION_TEMPLATE = """
{"status": {{ output.status | tojson }}, "returncode": {{ output.returncode }}, "exit_code": {{ output.exit_code | tojson }}, "timed_out": {{ output.timed_out | tojson }}, "signal": {{ output.signal | tojson }}, "termination": {{ output.termination | tojson }}, "path": {{ output.get("path") | tojson }}, "operation": {{ output.get("operation") | tojson }}, "content_hash": {{ output.get("content_hash") | tojson }}, "error_code": {{ output.get("extra", {}).get("error_code") | tojson }}, "attempts": {{ output.get("extra", {}).get("attempts", []) | tojson }}, "page": {{ output.get("extra", {}).get("page", {}) | tojson }}, "stdout": {{ output.stdout | tojson }}, "stderr": {{ output.stderr | tojson }}, "stdout_truncated": {{ output.stdout_truncated | tojson }}, "stderr_truncated": {{ output.stderr_truncated | tojson }}{% if output.stdout_spill_path %}, "stdout_spill_path": {{ output.stdout_spill_path | tojson }}{% endif %}{% if output.stderr_spill_path %}, "stderr_spill_path": {{ output.stderr_spill_path | tojson }}{% endif %}{% if output.exception_info %}, "exception_info": {{ output.exception_info | tojson }}{% endif %}}
""".strip()

DEFAULT_FORMAT_ERROR_TEMPLATE = """
响应格式无法解析：
{{ error }}

{% if finish_reason | default("") in ["length", "max_tokens"] %}
模型响应触达了提供方的输出上限，内容可能不完整。请压缩内容后重新完整回答。
{% endif %}

如果确实需要操作，请使用 bash 工具，并传入如下 JSON 参数：
{"command": "要执行的命令", "workdir": "可选工作目录", "timeout": 30}

如果需要修改文件，请使用 str_replace_editor 工具；先用 view 查看，再用 str_replace、insert 或 create 修改。

如果需要当前网络信息，请先使用 web_search 搜索；需要查看某篇具体文章时，再使用 web_fetch 打开搜索结果中的 URL。两者都不需要 DS_KEY，并应在最终答复中引用来源 URL。

如果任务已经可以回答，请直接返回中文最终答复，不要强行调用工具。
""".strip()


class DeepSeekModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temperature: float = 0.0
    thinking: bool = False
    retry_attempts: int = 3
    api_timeout_seconds: float = DEFAULT_API_TIMEOUT_SECONDS
    stream_output: bool = True
    observation_template: str = DEFAULT_OBSERVATION_TEMPLATE
    format_error_template: str = DEFAULT_FORMAT_ERROR_TEMPLATE


class DeepSeekModel:
    """DeepSeek adapter exposing host-owned Bash and text-editor tools."""

    def __init__(self, **kwargs):
        self.config = DeepSeekModelConfig(**kwargs)
        api_key = os.getenv("DS_KEY")
        if not api_key:
            raise ValueError("DS_KEY is required to call DeepSeek")
        # This applies the 60-second limit to SDK connect/read/write/pool operations.
        self.client = OpenAI(api_key=api_key, base_url=BASE_URL, timeout=self.config.api_timeout_seconds)

    def query(self, messages: list[dict[str, Any]], **kwargs) -> dict:
        tool_names = kwargs.get("tools")
        tools = get_tool_definitions(tool_names)
        request = {
            "model": MODEL_NAME,
            "messages": self._api_messages(messages),
            "temperature": self.config.temperature,
            "stream": True,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        request["extra_body"] = {
            "thinking": {"type": "enabled" if self.config.thinking else "disabled"}
        }

        response = self._request(request)
        content, reasoning_content, tool_calls, finish_reason, usage = self._consume_stream(response)
        if finish_reason in {"length", "max_tokens"}:
            parse_toolcall_actions(
                [],
                format_error_template=self.config.format_error_template,
                template_kwargs={"finish_reason": finish_reason},
            )
        actions = []
        if tool_calls:
            actions = parse_toolcall_actions(
                tool_calls,
                format_error_template=self.config.format_error_template,
                template_kwargs={"finish_reason": finish_reason},
                allowed_tools=set(tool_names) if tool_names is not None else None,
            )
            if self.config.stream_output:
                render_tool_actions(actions)
        elif not content.strip():
            parse_toolcall_actions(
                [],
                format_error_template=self.config.format_error_template,
                template_kwargs={"finish_reason": finish_reason},
            )
        extra = {
            "actions": actions,
            "finish_reason": finish_reason,
            "usage": usage,
            "timestamp": time.time(),
        }
        if reasoning_content:
            extra["reasoning_content"] = reasoning_content
        result = {
            "role": "assistant",
            "content": content or None,
            "extra": extra,
        }
        if tool_calls:
            result["tool_calls"] = [call.model_dump() for call in tool_calls]
        if reasoning_content:
            result["reasoning_content"] = reasoning_content
        return result

    def _request(self, request: dict) -> Any:
        last_error: Exception | None = None
        for attempt in range(max(1, self.config.retry_attempts)):
            try:
                return self.client.chat.completions.create(
                    **request, timeout=self.config.api_timeout_seconds
                )
            except Exception as error:
                last_error = error
                if attempt + 1 >= max(1, self.config.retry_attempts):
                    raise
                delay = 2**attempt
                logger.warning("DeepSeek request failed; retrying in %ss: %s", delay, error)
                time.sleep(delay)
        raise RuntimeError("DeepSeek request failed") from last_error  # pragma: no cover

    def _consume_stream(self, response: Any) -> tuple[str, str, list["_ToolCall"], str | None, dict]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: dict[int, _ToolCall] = {}
        finish_reason = None
        usage: dict = {}
        output_state = _OutputState(enabled=self.config.stream_output)

        for chunk in response:
            if getattr(chunk, "usage", None):
                usage = chunk.usage.model_dump(exclude_none=True)
            for choice in getattr(chunk, "choices", []) or []:
                finish_reason = getattr(choice, "finish_reason", None) or finish_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                if reasoning:
                    reasoning_parts.append(reasoning)
                    self._stream_text(output_state, "思考", reasoning)
                content = getattr(delta, "content", None)
                if content:
                    content_parts.append(content)
                    self._stream_text(output_state, "回复", content)
                for tool_delta in getattr(delta, "tool_calls", None) or []:
                    index = getattr(tool_delta, "index", None)
                    index = 0 if index is None else index
                    call = tool_calls.setdefault(index, _ToolCall())
                    call.id = getattr(tool_delta, "id", None) or call.id
                    function = getattr(tool_delta, "function", None)
                    if function is None:
                        continue
                    name = getattr(function, "name", None)
                    arguments = getattr(function, "arguments", None)
                    if name:
                        call.function.name = name
                    if arguments:
                        call.function.arguments += arguments

        output_state.finish()
        return "".join(content_parts), "".join(reasoning_parts), list(tool_calls.values()), finish_reason, usage

    @staticmethod
    def _stream_text(state: "_OutputState", label: str, text: str) -> None:
        if not state.enabled:
            return
        state.renderer.write(label, text)

    @staticmethod
    def _api_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop local trajectory metadata before sending OpenAI-compatible messages."""
        allowed = {
            "system": {"role", "content"},
            "user": {"role", "content"},
            "assistant": {"role", "content", "tool_calls", "reasoning_content"},
            "tool": {"role", "content", "tool_call_id"},
        }
        result = []
        for message in messages:
            role = message.get("role")
            if role not in allowed:
                continue
            api_message = {
                key: value
                for key, value in message.items()
                if key in allowed[role] and value is not None
            }
            if role == "assistant" and not api_message.get("tool_calls"):
                api_message.pop("tool_calls", None)
            result.append(api_message)
        return result

    def format_message(self, **kwargs) -> dict:
        return {key: value for key, value in kwargs.items() if value is not None}

    def format_observation_messages(
        self, message: dict, outputs: list[dict], template_vars: dict | None = None
    ) -> list[dict]:
        return format_toolcall_observation_messages(
            actions=message.get("extra", {}).get("actions", []),
            outputs=outputs,
            observation_template=self.config.observation_template,
            template_vars=template_vars,
        )

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return recursive_merge(
            {"model_name": MODEL_NAME, **self.config.model_dump()}, kwargs
        )

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "model": {
                        "model_name": MODEL_NAME,
                        "base_url": BASE_URL,
                        **self.config.model_dump(mode="json"),
                    }
                },
                "model_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
            }
        }


@dataclass
class _FunctionCall:
    name: str = "bash"
    arguments: str = ""


@dataclass
class _ToolCall:
    id: str = ""
    function: _FunctionCall = field(default_factory=_FunctionCall)

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


@dataclass
class _OutputState:
    enabled: bool
    renderer: StreamRenderer = field(default_factory=StreamRenderer)

    def finish(self) -> None:
        if self.enabled:
            self.renderer.finish()

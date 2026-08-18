"""OpenAI-compatible DeepSeek V4 Flash adapter."""

import logging
import os
import time
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, ConfigDict

from minisweagent.models.utils.actions_toolcall import (
    BASH_TOOL,
    format_toolcall_observation_messages,
    parse_toolcall_actions,
)
from minisweagent.utils.serialize import recursive_merge

logger = logging.getLogger("minisweagent.model")
MODEL_NAME = "deepseek-v4-flash"
BASE_URL = "https://api.deepseek.com"

DEFAULT_OBSERVATION_TEMPLATE = """
{%- if output.output | length < 10000 -%}
{"returncode": {{ output.returncode }}, "output": {{ output.output | tojson }}{% if output.exception_info %}, "exception_info": {{ output.exception_info | tojson }}{% endif %}}
{%- else -%}
{"returncode": {{ output.returncode }}, "output_head": {{ output.output[:5000] | tojson }}, "output_tail": {{ output.output[-5000:] | tojson }}, "warning": "Output too long."}
{%- endif -%}
""".strip()

DEFAULT_FORMAT_ERROR_TEMPLATE = """
Tool call error:
{{ error }}

Every response must call the bash tool exactly as:
{"command": "your command"}

Finish with one bash call that prints the marker first and the final report afterwards:
printf '%s\n' 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT' 'your final report'
""".strip()


class DeepSeekModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tokens: int = 4096
    temperature: float = 0.0
    thinking: bool = False
    retry_attempts: int = 3
    observation_template: str = DEFAULT_OBSERVATION_TEMPLATE
    format_error_template: str = DEFAULT_FORMAT_ERROR_TEMPLATE


class DeepSeekModel:
    """One-tool model adapter; all file changes happen through Bash."""

    def __init__(self, **kwargs):
        self.config = DeepSeekModelConfig(**kwargs)
        api_key = os.getenv("DS_KEY")
        if not api_key:
            raise ValueError("DS_KEY is required to call DeepSeek")
        self.client = OpenAI(api_key=api_key, base_url=BASE_URL)

    def query(self, messages: list[dict[str, Any]], **_kwargs) -> dict:
        request = {
            "model": MODEL_NAME,
            "messages": self._api_messages(messages),
            "tools": [BASH_TOOL],
            "tool_choice": "required",
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": False,
        }
        if not self.config.thinking:
            request["extra_body"] = {"thinking": {"type": "disabled"}}

        response = self._request(request)
        choice = response.choices[0]
        tool_calls = choice.message.tool_calls or []
        usage = response.usage.model_dump(exclude_none=True) if response.usage else {}
        extra = {
            "actions": parse_toolcall_actions(
                tool_calls,
                format_error_template=self.config.format_error_template,
                template_kwargs={"finish_reason": choice.finish_reason},
            ),
            "finish_reason": choice.finish_reason,
            "usage": usage,
            "timestamp": time.time(),
        }
        return {
            "role": "assistant",
            "content": choice.message.content,
            "tool_calls": [call.model_dump(exclude_none=True) for call in tool_calls],
            "extra": extra,
        }

    def _request(self, request: dict) -> Any:
        last_error: Exception | None = None
        for attempt in range(max(1, self.config.retry_attempts)):
            try:
                return self.client.chat.completions.create(**request)
            except Exception as error:
                last_error = error
                if attempt + 1 >= max(1, self.config.retry_attempts):
                    raise
                delay = 2**attempt
                logger.warning("DeepSeek request failed; retrying in %ss: %s", delay, error)
                time.sleep(delay)
        raise RuntimeError("DeepSeek request failed") from last_error  # pragma: no cover

    @staticmethod
    def _api_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop local trajectory metadata before sending OpenAI-compatible messages."""
        allowed = {
            "system": {"role", "content"},
            "user": {"role", "content"},
            "assistant": {"role", "content", "tool_calls"},
            "tool": {"role", "content", "tool_call_id"},
        }
        result = []
        for message in messages:
            role = message.get("role")
            if role not in allowed:
                continue
            result.append(
                {
                    key: value
                    for key, value in message.items()
                    if key in allowed[role] and value is not None
                }
            )
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

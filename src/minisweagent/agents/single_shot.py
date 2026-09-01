"""一次模型调用的角色流程。"""

from .default import DefaultAgent


class SingleShotAgent(DefaultAgent):
    """适合总结、分类和方案生成等不需要工具循环的角色。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.config.tools:
            raise ValueError("single_shot flow 不支持工具；请使用 iterative flow")
        self.config.tools = []
        self.config.max_consecutive_format_errors = 1

    def step(self) -> list[dict]:
        message = self.query()
        content = (message.get("content") or "").strip()
        return self.add_messages(
            self.model.format_message(
                role="exit",
                content=content,
                extra={"exit_status": "Submitted", "submission": content},
            )
        )

#!/usr/bin/env python3
"""轨迹观测、角色配置与就地试跑服务。

轨迹文件本身就是唯一真相来源，所以这里不建数据库、不建事件流、不加缓存层之外的状态。
观测侧只做三件事：扫描目录建索引、把消息序列换算成带耗时的步骤、按需返回单条轨迹全文。
配置侧只做一件事：读写 `deepseek.yaml` 的 agents 段和 `prompts/` 下的模板，落盘前跑一遍自检。
试跑侧只做一件事：用子进程起一次 `mini --agent`，轨迹落回观测目录，用观测页看它跑。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import typer
from jinja2 import StrictUndefined, Template, TemplateError

from minisweagent.agents import AGENT_FLOWS
from minisweagent.config import (
    PROMPT_KINDS,
    get_config_path,
    load_config_file,
    prompt_path,
    save_config_file,
    validate_agents,
)
from minisweagent.models.utils.actions_toolcall import TOOL_DEFINITIONS_BY_NAME

PROMPT_DIR = "prompts"

# 子轨迹文件名规则由宿主的 agent_trace_prefix 固定：<父轨迹名>-<序号>-<角色>.json。
CHILD_NAME = re.compile(r"^(?P<parent>.+)-(?P<seq>\d{2})-(?P<role>.+)$")
UI_PATH = Path(__file__).resolve().parent / "inspect_ui.html"
# 没有 exit 消息且近期还在写盘的轨迹当作正在运行；轨迹每步覆盖写，mtime 就是心跳。
RUNNING_GRACE_SECONDS = 180

app = typer.Typer(add_completion=False)


def _epoch(value: str) -> float | None:
    """ISO-8601 转 epoch；空串或格式不符返回 None，不猜时间。"""
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _first_timestamp(messages: list[dict]) -> float | None:
    for message in messages:
        timestamp = (message.get("extra") or {}).get("timestamp")
        if timestamp:
            return float(timestamp)
    return None


def _child_trace(extra: dict) -> str:
    """agent_call 的结果里宿主写下了子轨迹路径，这是调度树唯一可靠的边。"""
    stdout = extra.get("stdout") or ""
    if '"trace_path"' not in stdout:
        return ""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return ""
    for section in ("data", "error"):
        path = (payload.get(section) or {}).get("trace_path")
        if path:
            return str(path)
    return ""


def _step(index: int, message: dict, start: float | None, end: float | None, t0: float | None) -> dict:
    """一条消息换算成一行时间轴步骤；角色决定携带哪些字段，不做统一的空壳。"""
    extra = message.get("extra") or {}
    role = message.get("role", "")
    step: dict[str, Any] = {
        "index": index,
        "role": role,
        "content": message.get("content") or "",
        "at": start,
        "offset": round(start - t0, 3) if start and t0 else None,
        "duration": round(end - start, 3) if start and end else None,
    }
    if role == "assistant":
        actions = extra.get("actions") or []
        step["actions"] = actions
        step["tools"] = [action.get("tool", "") for action in actions]
        step["reasoning"] = extra.get("reasoning_content") or ""
        step["finish_reason"] = extra.get("finish_reason") or ""
        step["usage"] = extra.get("usage") or {}
    elif extra.get("status"):
        step["tool"] = extra.get("tool") or ""
        step["status"] = extra.get("status") or ""
        step["ok"] = extra.get("status") == "success"
        # 老轨迹只有一批工具共享的完成时刻，批内谁慢分不出来，前端要把这种耗时标成不可信。
        step["timed"] = bool(extra.get("started_at"))
        step["error_code"] = extra.get("error_code") or ""
        step["exception_info"] = extra.get("exception_info") or ""
        step["stdout"] = extra.get("stdout") or ""
        step["stderr"] = extra.get("stderr") or ""
        step["truncated"] = bool(extra.get("stdout_truncated") or extra.get("stderr_truncated"))
        step["child_trace"] = _child_trace(extra)
    elif role == "exit":
        step["exit_status"] = extra.get("exit_status") or ""
    return step


def _steps(messages: list[dict], t0: float | None) -> list[dict]:
    """cursor 是时间轴当前位置：工具自带起止时间时以它为准，老轨迹只有批共享 timestamp 时自然退化。"""
    cursor = t0
    steps = []
    actions: dict[str, dict] = {}  # tool_call_id -> 调用参数，观测消息本身不带发起时的参数
    for index, message in enumerate(messages):
        extra = message.get("extra") or {}
        for action in extra.get("actions") or []:
            actions[action.get("tool_call_id", "")] = action
        start = extra.get("started_at") or cursor
        end = extra.get("ended_at") or extra.get("timestamp") or start
        if end and cursor and end < cursor:
            end = cursor  # 时钟回拨或缺时间时不画负长度的条
        step = _step(index, message, start, end, t0)
        if "tool" in step:
            action = actions.get(message.get("tool_call_id", "")) or {}
            step["args"] = {k: v for k, v in action.items() if k != "tool_call_id"}
            step["tool"] = step["tool"] or action.get("tool", "")
        steps.append(step)
        cursor = end or cursor
    return steps


def _tokens(steps: list[dict]) -> dict[str, int]:
    total = {"prompt": 0, "completion": 0, "reasoning": 0, "cached": 0}
    for step in steps:
        usage = step.get("usage") or {}
        total["prompt"] += usage.get("prompt_tokens") or 0
        total["completion"] += usage.get("completion_tokens") or 0
        total["cached"] += usage.get("prompt_cache_hit_tokens") or 0
        total["reasoning"] += (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
    return total


class NotATrace(Exception):
    """目标 JSON 不是 Agent 轨迹（账本目录下还有监控计划和板块缓存）。"""


def _read(path: Path) -> tuple[dict, list[dict], float | None]:
    """读一条轨迹并算出时间轴；t0 优先用会话开始时间，缺失时退回第一条带时间的消息。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "trajectory_format" not in data:
        raise NotATrace(str(path))
    messages = data.get("messages") or []
    session = (data.get("info") or {}).get("session") or {}
    t0 = _epoch(session.get("started_at", "")) or _first_timestamp(messages)
    return data, _steps(messages, t0), t0


def _summarize(path: Path, root: Path, parsed: tuple | None = None) -> dict:
    """一条轨迹的索引摘要；不含 stdout 全文，列表页要能秒开。已解析过的可直接传进来复用。"""
    data, steps, t0 = parsed or _read(path)
    info = data.get("info") or {}
    session = info.get("session") or {}
    config = info.get("config") or {}
    agent = config.get("agent") or {}
    environment = config.get("environment") or {}
    stat = path.stat()
    tool_steps = [step for step in steps if "status" in step]
    ends = [step["at"] + (step["duration"] or 0) for step in steps if step["at"]]
    exited = any(step["role"] == "exit" for step in steps)
    return {
        "id": path.relative_to(root).as_posix(),
        "session_id": session.get("id") or "",
        "parent": session.get("parent") or "",
        "kind": session.get("kind") or "",
        "agent_name": agent.get("agent_name") or "",
        "flow": agent.get("flow") or "",
        "tools": agent.get("tools") or [],
        "cycle_id": environment.get("account_cycle_id") or "",
        "miniqmt_mode": environment.get("miniqmt_mode") or "",
        "started_at": session.get("started_at") or "",
        "exit_status": info.get("exit_status") or "",
        "submission": info.get("submission") or "",
        "api_calls": (info.get("model_stats") or {}).get("api_calls") or 0,
        "tokens": _tokens(steps),
        "duration": round(max(ends) - t0, 1) if ends and t0 else None,
        "n_messages": len(steps),
        "n_tool_calls": len(tool_steps),
        "n_errors": sum(1 for step in tool_steps if not step["ok"]),
        "mtime": stat.st_mtime,
        "size": stat.st_size,
        "running": not exited and time.time() - stat.st_mtime < RUNNING_GRACE_SECONDS,
        "stale": False,
        "parse_error": "",
        "child_traces": [step["child_trace"] for step in tool_steps if step.get("child_trace")],
    }


class TraceIndex:
    """按 mtime+size 缓存摘要：轨迹每步覆盖写，文件没变就没必要重新解析 1MB JSON。"""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self._cache: dict[Path, tuple[float, int, dict]] = {}

    def _paths(self) -> list[Path]:
        # 点号开头的是写盘临时文件，账本目录下还有监控和缓存状态，都不是轨迹。
        return sorted(p for p in self.root.rglob("*.json") if p.is_file() and not p.name.startswith("."))

    def _scan(self) -> list[dict]:
        summaries = []
        for path in self._paths():
            stat = path.stat()
            cached = self._cache.get(path)
            if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
                if cached[2] is not None:
                    summaries.append(cached[2])
                continue
            try:
                summary = _summarize(path, self.root)
            except NotATrace:
                # 缓存"不是轨迹"这个结论，否则每轮轮询都要重新解析 1MB 的板块缓存。
                self._cache[path] = (stat.st_mtime, stat.st_size, None)
                continue
            except (ValueError, KeyError, OSError) as error:
                # 正在写盘的轨迹可能读到半截。用上一次的摘要顶住，但必须让前端看见这是过期快照。
                if cached and cached[2]:
                    summaries.append(
                        dict(cached[2], stale=True, parse_error=f"{type(error).__name__}: {error}")
                    )
                continue
            self._cache[path] = (stat.st_mtime, stat.st_size, summary)
            summaries.append(summary)
        return summaries

    @staticmethod
    def _parent_id(summary: dict, summaries: dict, by_session_id: dict) -> str:
        """优先用轨迹里写下的父 session id；老轨迹没这个字段时退回文件名前缀规则。"""
        parent = by_session_id.get(summary["parent"], "")
        if parent and parent != summary["id"]:
            return parent
        match = CHILD_NAME.match(Path(summary["id"]).stem)
        if not match:
            return ""
        sibling = Path(summary["id"]).with_name(f"{match.group('parent')}.json").as_posix()
        return sibling if sibling in summaries else ""

    def sessions(self) -> list[dict]:
        """返回调度树：根是宿主启动的会话，children 是它 agent_call 出去的子会话。"""
        summaries = {summary["id"]: dict(summary, children=[]) for summary in self._scan()}
        by_session_id = {s["session_id"]: s["id"] for s in summaries.values() if s["session_id"]}
        roots = []
        for summary in summaries.values():
            parent = self._parent_id(summary, summaries, by_session_id)
            if parent:
                summaries[parent]["children"].append(summary)
            else:
                roots.append(summary)
        for summary in summaries.values():
            summary["children"].sort(key=lambda child: child["id"])
        roots.sort(key=lambda s: (s["started_at"], s["mtime"]), reverse=True)
        return roots

    def _resolve(self, session_id: str) -> Path:
        path = (self.root / session_id).resolve()
        if self.root not in path.parents or not path.is_file():
            raise FileNotFoundError(f"轨迹不存在或不在观测目录内：{session_id}")
        return path

    def _child(self, trace_path: str) -> dict:
        """子轨迹缺失必须显式报出来：那正是子 Agent 在落盘前就死掉的现场。"""
        try:
            relative = Path(trace_path).resolve().relative_to(self.root).as_posix()
            path = self._resolve(relative)
            parsed = _read(path)
        except (NotATrace, ValueError, OSError) as error:
            return {"session": {"id": trace_path}, "steps": [], "error": str(error)}
        return {"session": _summarize(path, self.root, parsed), "steps": parsed[1], "error": ""}

    def load(self, session_id: str) -> dict:
        """单条轨迹全文 + 它直接调度出去的子轨迹全文，一次给全，本地传输不值得再切接口。"""
        path = self._resolve(session_id)
        parsed = _read(path)
        steps = parsed[1]
        children = [self._child(step["child_trace"]) for step in steps if step.get("child_trace")]
        return {"session": _summarize(path, self.root, parsed), "steps": steps, "children": children}


class ConfigStore:
    """角色配置和 prompt 的读写。可写范围只有配置文件本身和它旁边的 prompts/ 目录。"""

    def __init__(self, path: Path):
        self.path = path.resolve()

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def read(self) -> dict:
        settings = load_config_file(self.path)
        return {
            "path": str(self.path),
            "agents": settings.get("agents") or {},
            "tool_names": sorted(TOOL_DEFINITIONS_BY_NAME),
            "flows": sorted(AGENT_FLOWS),
            "prompt_kinds": list(PROMPT_KINDS),
        }

    def write_agents(self, agents: dict) -> dict:
        """先整份自检再落盘：配置写坏了要在这里挡住，不能等下一轮启动才炸。"""
        settings = load_config_file(self.path)
        settings["agents"] = agents
        validate_agents(settings, self.base_dir)
        save_config_file(self.path, settings)
        return {"ok": True, "orphan_prompts": self._orphan_prompts(agents)}

    def _orphan_prompts(self, agents: dict) -> list[str]:
        """不再被任何角色引用的 prompt 文件。宿主不擅自删文件，只报给你。"""
        referenced = {
            (profile or {}).get(f"{kind}_template_path")
            for profile in agents.values()
            for kind in PROMPT_KINDS
        }
        directory = self.base_dir / PROMPT_DIR
        if not directory.is_dir():
            return []
        return sorted(
            f"{PROMPT_DIR}/{item.name}"
            for item in directory.glob("*.md")
            if f"{PROMPT_DIR}/{item.name}" not in referenced
        )

    def read_prompt(self, role: str, kind: str) -> dict:
        path = prompt_path(self.base_dir, role, kind)
        exists = path.is_file()
        return {
            "role": role,
            "kind": kind,
            "path": f"{PROMPT_DIR}/{path.name}",
            "text": path.read_text(encoding="utf-8") if exists else "",
            "exists": exists,
        }

    def write_prompt(self, role: str, kind: str, text: str) -> dict:
        path = prompt_path(self.base_dir, role, kind)
        try:
            Template(text, undefined=StrictUndefined)
        except TemplateError as error:
            # 模板语法错会在第一次渲染时打死整轮，编译一次就能提前发现。
            raise ValueError(f"prompt 模板语法错误：{error}") from error
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return {"ok": True, "path": f"{PROMPT_DIR}/{path.name}"}


class Conflict(Exception):
    """已有运行占用，返回 409 而不是 400。"""


AGENT_MODES = ("auto_execute", "execute", "observe")


class Runner:
    """前端触发的单次运行。

    用子进程跑 `mini --agent ...`，而不是在服务里直接构造 Agent：装配逻辑一份都不重复，
    Agent 崩溃打不到观测服务，停止运行就是终止进程。
    同一时刻只允许一个，否则两轮 Agent 会抢 MiniQMT 连接和当日账本。
    """

    def __init__(self, sessions_dir: Path, store: ConfigStore):
        self.sessions_dir = sessions_dir.resolve()
        self.store = store
        self.current: dict[str, Any] | None = None
        self._process: subprocess.Popen | None = None
        self._log: Any = None

    def status(self) -> dict:
        if self.current is None:
            return {"idle": True}
        returncode = self._process.poll() if self._process else None
        return {
            **self.current,
            "idle": False,
            "running": returncode is None,
            "returncode": returncode,
            "log_tail": self._log_tail(),
        }

    def start(self, role: str, task: str, mode: str) -> dict:
        if self._process is not None and self._process.poll() is None:
            raise Conflict(f"已有运行中的 {self.current['role']}；先停止它再启动新的")
        if role not in self.store.read()["agents"]:
            raise ValueError(f"配置里没有角色 {role}")
        if not task.strip():
            raise ValueError("任务不能为空")
        if mode not in AGENT_MODES:
            raise ValueError(f"mode 只能是 {', '.join(AGENT_MODES)}")
        started = datetime.now().astimezone()
        run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        day_dir = self.sessions_dir / started.strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        trace = day_dir / f"{run_id}-inspect-{role}.json"
        self._spawn(trace, role, task, mode)
        self.current = {
            "run_id": run_id,
            "role": role,
            "task": task,
            "mode": mode,
            "started_at": started.isoformat(),
            "trace_id": trace.relative_to(self.sessions_dir).as_posix(),
            "log_path": str(trace.with_suffix(".log")),
        }
        return self.status()

    def _spawn(self, trace: Path, role: str, task: str, mode: str) -> None:
        command = [
            sys.executable, "-m", "minisweagent.run.mini",
            "--agent", role,
            "--task", task,
            "--config", str(self.store.path),
            "--output", str(trace),
        ]
        self._close_log()
        self._log = trace.with_suffix(".log").open("w", encoding="utf-8")
        # stdin 必须断开：服务是从终端启动的，子进程继承 tty 会进入交互模式然后卡在等输入。
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            cwd=str(Path.cwd()),
            env={**os.environ, "MINIQMT_AGENT_MODE": mode},
        )

    def stop(self) -> dict:
        if self._process is None or self._process.poll() is not None:
            raise ValueError("当前没有运行中的进程")
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)
        self._close_log()
        return self.status()

    def shutdown(self) -> None:
        """服务退出时不留孤儿：auto_execute 的交易 Agent 活着就还能继续下单。"""
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._close_log()

    def _close_log(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None

    def _log_tail(self, limit: int = 6000) -> str:
        """子进程的崩溃现场只在这个日志里；点了没反应时它是唯一线索。"""
        path = Path(self.current["log_path"]) if self.current else None
        if path is None or not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]


class InspectServer(ThreadingHTTPServer):
    """只监听回环地址：轨迹里有账户持仓、账本和完整决策过程，不该出本机。"""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type,
        index: TraceIndex,
        config: ConfigStore,
        runner: Runner,
    ):
        super().__init__(address, handler)
        self.index = index
        self.config = config
        self.runner = runner


class Handler(BaseHTTPRequestHandler):
    server_version = "mini-inspect"

    def do_GET(self) -> None:  # noqa: N802 - stdlib 约定的方法名
        route = urlparse(self.path)
        routes = {
            "/": self._ui,
            "/index.html": self._ui,
            "/api/sessions": self._sessions,
            "/api/session": self._session,
            "/api/config": self._config,
            "/api/prompt": self._prompt,
            "/api/run": self._run_status,
        }
        self._dispatch(routes.get(route.path), route, write=False)

    def do_PUT(self) -> None:  # noqa: N802 - stdlib 约定的方法名
        route = urlparse(self.path)
        routes = {"/api/config": self._save_config, "/api/prompt": self._save_prompt}
        self._dispatch(routes.get(route.path), route, write=True)

    def do_POST(self) -> None:  # noqa: N802 - stdlib 约定的方法名
        route = urlparse(self.path)
        routes = {"/api/run": self._start_run, "/api/run/stop": self._stop_run}
        self._dispatch(routes.get(route.path), route, write=True)

    def _dispatch(self, handler, route, *, write: bool) -> None:
        if handler is None:
            self._error(404, f"未知路径：{route.path}")
            return
        if write and not self._same_origin():
            self._error(403, "拒绝跨站写请求")
            return
        try:
            handler(self._body() if write else parse_qs(route.query))
        except Conflict as error:
            self._error(409, str(error))
        except (NotATrace, ValueError, OSError) as error:
            self._error(400, f"{type(error).__name__}: {error}")

    def _same_origin(self) -> bool:
        """写请求只接受本机页面。浏览器强制给跨站请求带 Origin，所以缺失 Origin 只可能来自
        本机的 curl 或脚本，外部网页伪造不出来。"""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return urlparse(origin).hostname in {"127.0.0.1", "localhost"}

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def _ui(self, _query: dict) -> None:
        body = UI_PATH.read_bytes()
        self._send(200, "text/html; charset=utf-8", body)

    def _sessions(self, _query: dict) -> None:
        index: TraceIndex = self.server.index  # type: ignore[attr-defined]
        self._json({"sessions": index.sessions(), "generated_at": time.time()})

    def _session(self, query: dict) -> None:
        index: TraceIndex = self.server.index  # type: ignore[attr-defined]
        session_id = (query.get("id") or [""])[0]
        if not session_id:
            raise ValueError("缺少 id 参数")
        self._json(index.load(session_id))

    def _config(self, _query: dict) -> None:
        self._json(self._store().read())

    def _prompt(self, query: dict) -> None:
        role = (query.get("role") or [""])[0]
        kind = (query.get("kind") or [""])[0]
        self._json(self._store().read_prompt(role, kind))

    def _save_config(self, body: dict) -> None:
        agents = body.get("agents")
        if not isinstance(agents, dict):
            raise ValueError("请求体缺少 agents 对象")
        self._json(self._store().write_agents(agents))

    def _save_prompt(self, body: dict) -> None:
        text = body.get("text")
        if not isinstance(text, str):
            raise ValueError("请求体缺少 text 字符串")
        self._json(self._store().write_prompt(body.get("role", ""), body.get("kind", ""), text))

    def _store(self) -> ConfigStore:
        return self.server.config  # type: ignore[attr-defined]

    def _runner(self) -> Runner:
        return self.server.runner  # type: ignore[attr-defined]

    def _run_status(self, _query: dict) -> None:
        self._json(self._runner().status())

    def _start_run(self, body: dict) -> None:
        self._json(
            self._runner().start(
                body.get("role", ""),
                body.get("task", ""),
                body.get("mode", AGENT_MODES[0]),
            )
        )

    def _stop_run(self, _body: dict) -> None:
        self._json(self._runner().stop())

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(200, "application/json; charset=utf-8", body)

    def _error(self, code: int, detail: str) -> None:
        body = json.dumps({"error": detail}, ensure_ascii=False).encode("utf-8")
        self._send(code, "application/json; charset=utf-8", body)

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # 前端每两秒轮询一次，成功请求不值得刷屏；失败必须留下来。stdlib 把状态码放在第二个参数。
        if len(args) > 1 and str(args[1]).startswith(("2", "3")):
            return
        super().log_message(fmt, *args)


@app.command()
def main(
    port: int = typer.Option(8765, "--port", help="监听端口，只绑定 127.0.0.1。"),
    sessions_dir: Path = typer.Option(Path(".sessions"), "--sessions-dir", help="轨迹目录。"),
    config: Path = typer.Option(Path("deepseek.yaml"), "-c", "--config", help="可编辑的角色配置文件。"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="启动后自动打开浏览器。"),
) -> None:
    """观测轨迹与子 Agent 调度，编辑角色和 prompt，并就地跑一轮看效果。"""
    root = sessions_dir if sessions_dir.is_absolute() else Path.cwd() / sessions_dir
    if not root.is_dir():
        raise typer.BadParameter(f"轨迹目录不存在：{root}")
    store = ConfigStore(get_config_path(config))
    validate_agents(load_config_file(store.path), store.base_dir)
    runner = Runner(root, store)
    url = f"http://127.0.0.1:{port}/"
    server = InspectServer(("127.0.0.1", port), Handler, TraceIndex(root), store, runner)
    # 被 kill 时也要走到 finally：一个 auto_execute 的交易 Agent 变成孤儿还能继续下单。
    signal.signal(signal.SIGTERM, _raise_interrupt)
    print(f"观测服务已启动：{url}")
    print(f"轨迹目录 {root}；可编辑配置 {store.path}（Ctrl+C 退出）")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        runner.shutdown()
        server.server_close()


def _raise_interrupt(*_: Any) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    app()

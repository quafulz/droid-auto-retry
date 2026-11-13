#!/usr/bin/env python3
"""Stop hook：回合没有真正做完时，有限次自动继续。

Droid 读自己的 transcript。Codex 读 rollout 里的 response_item。
两边的续跑结论一致：静默中断、压缩摘要、截断、协议泄漏、未完成待办会续跑。

Codex 还有一个 Stop 钩子接不到的出口：采样请求在 API 层失败（429 限流、5xx、
流中断）时，Codex 直接结束回合，不触发 Stop。插件为此额外注册了
PostModelRequest —— 它在失败时带 status=failed，是 Codex 唯一能报告这类失败的
可观测钩子。拿到失败后按指数退避等待，再拉一次 `codex exec resume`。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any


PLUGIN_NAME = "custom-model-stop-patch"
DEFAULT_MAX_ATTEMPTS = 4
MAX_MAX_ATTEMPTS = 10
DEFAULT_BASE_DELAY_SECONDS = 2.0
DEFAULT_MAX_DELAY_SECONDS = 15.0
DEFAULT_BACKOFF_FACTOR = 2.0
HOOK_TIMEOUT_SECONDS = 45.0
HOOK_TIMEOUT_RESERVE_SECONDS = 5.0
MAX_SLEEP_SECONDS = HOOK_TIMEOUT_SECONDS - HOOK_TIMEOUT_RESERVE_SECONDS
DEFAULT_STATE_TTL_SECONDS = 2 * 60 * 60
DEFAULT_TRANSCRIPT_TAIL_BYTES = 512 * 1024
MAX_TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024
SQLITE_BUSY_TIMEOUT_SECONDS = 5.0
SILENT_BLOCK_TYPES = {"thinking", "redacted_thinking", "reasoning"}
TEXT_BLOCK_TYPES = {"text", "output_text"}
TOOL_BLOCK_TYPES = {"tool_use", "tool_call", "server_tool_use"}
TODO_TOOL_NAMES = {"todowrite"}
PENDING_TODO_STATUSES = {"in_progress", "pending"}
SUMMARY_TAGS = ("analysis", "summary")
CODE_FENCE = "```"

# API 失败后续跑的参数。Codex 自己的重试基数是 200ms、总共只有约 3 秒，对 429 的
# 限流窗口远远不够；这里用独立的退避预算，靠 hook 进程自己等。
DEFAULT_FAILURE_BASE_DELAY_SECONDS = 5.0
DEFAULT_FAILURE_MAX_DELAY_SECONDS = 40.0
DEFAULT_FAILURE_MAX_ATTEMPTS = 4
MAX_HOOK_SLEEP_SECONDS = 40.0
# 续跑等写锁的上限。发起回合的 Codex 进程通常在失败后很快退出，
# 但 TUI 场景下会一直活着（默认空闲 60 秒才释放），所以给足窗口，
# 拿不到就放手，不无限挂着一个后台进程。
LOCK_WAIT_TIMEOUT_SECONDS = 180.0
LOCK_POLL_INTERVAL_SECONDS = 1.0
PROCESS_QUERY_TIMEOUT_SECONDS = 5.0
MAX_PROCESS_ARG_COUNT = 4096
RESUME_PROMPT = (
    "继续。上一轮在 API 层失败并且已经重试，没有输出正文。"
    "先核对已经成功的修改和命令结果，再从中断处继续当前任务，"
    "不要重复已经完成的写入、提交、推送、删除或其他有副作用的操作。"
)
# 可重试的失败指纹。Codex 把重试耗尽收敛成 "exceeded retry limit, last status: 429"，
# 所以既要认 HTTP 状态码，也要认这句收敛后的文案。
RETRYABLE_HTTP_STATUS = re.compile(r"\b(?:429|500|502|503|504)\b")
RETRYABLE_FAILURE_HINTS = (
    "exceeded retry limit",
    "too many requests",
    "rate limit",
    "rate_limit",
    "server had an error",
    "server overloaded",
    "overloaded",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "stream disconnected",
    "stream failed",
    "connection reset",
    "connection failed",
    "connection aborted",
    "timed out",
    "timeout",
    "temporarily unavailable",
)
# 明确不该重试的失败：重试只会重复失败，还会白等。
TERMINAL_FAILURE_HINTS = (
    "unauthorized",
    "invalid api key",
    "incorrect api key",
    "authentication",
    "permission denied",
    "forbidden",
    "not found",
    "model_not_found",
    "context length",
    "context window",
    "maximum context",
    "invalid request",
    "invalid_request",
    "bad request",
    "content policy",
    "content_filter",
    "quota",
    "billing",
    "insufficient",
    "usage limit",
    "budget",
    "unsupported",
)
LEAKED_MARKUP_TAIL = re.compile(
    r"<[／/]?(?:｜|\|)?\s*(?:DSML|antml)[^>]{0,120}>\s*$"
    r"|<[／/]?\s*(?:function_calls|function_results)[^>]{0,120}>\s*$",
    re.IGNORECASE,
)
QUESTION_PHRASES = (
    "请确认",
    "请提供",
    "请选择",
    "请告知",
    "是否需要",
    "是否继续",
    "要不要",
    "需要我",
    "可以吗",
    "告诉我",
    "确认一下",
)
MAX_TODO_LABEL_CHARS = 60
MAX_TODO_LABELS = 5
LIST_NUMBER_PREFIX = re.compile(r"^\s*\d+[.)、．]\s*")

ENDING_SILENT = "silent"
ENDING_SUMMARY = "summary"
ENDING_TRUNCATED = "truncated"
ENDING_LEAK = "leak"
ENDING_TODOS = "todos"
ENDING_TOOL_USE = "tool_use"
ENDING_UNKNOWN = "unknown"
ENDING_COMPLETE = "complete"

CONTINUATION_REASON = "继续。上一轮在思考阶段中断，没有输出正文，也没有发出工具调用。"
CONTINUATION_REASONS = {
    ENDING_SUMMARY: (
        "继续。上一轮只输出了上下文压缩摘要（<analysis>/<summary>），"
        "没有继续执行任务。"
    ),
    ENDING_TRUNCATED: (
        "继续。上一轮正文在输出途中被截断（代码块或标签没有闭合），"
        "不是完整答复。"
    ),
    ENDING_LEAK: (
        "继续。上一轮把工具调用协议当成了正文输出，没有真正发出工具调用，"
        "回合在输出途中结束。"
    ),
    ENDING_TODOS: "继续。上一条消息结束时待办列表仍有未完成项。",
}
CONTINUATION_TAIL = (
    "从中断处继续当前任务。先核对已经成功的修改和命令结果，"
    "不要重复已经完成的写入、提交、推送、删除或其他有副作用的操作。"
)
NORMAL_EXIT_NOTICE = "本轮任务正常完成。"
CODEX_TOOL_TYPES = {
    "function_call",
    "custom_tool_call",
    "local_shell_call",
    "tool_search_call",
    "web_search_call",
    "image_generation_call",
}
CODEX_PLAN_NAMES = {"updateplan", "todowrite"}
CODEX_TEXT_BLOCK_TYPES = {"output_text", "input_text"}
CODEX_BOUNDARY_TYPES = {
    "additional_tools",
    "configuration_update",
    "compaction_trigger",
}
UNKNOWN_MODEL_NAME = "Custom Model"
MAX_SETTINGS_BYTES = 2 * 1024 * 1024
MAX_MODEL_NAME_CHARS = 200


def env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw, 10)
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value != value or value in {float("inf"), float("-inf")}:
        return default
    return min(maximum, max(minimum, value))


def state_directory() -> Path:
    override = os.environ.get("DROID_AUTO_CONTINUE_STATE_DIR")
    if override and override.strip():
        return Path(override).expanduser()
    base = os.environ.get("XDG_STATE_HOME")
    if base and base.strip():
        return Path(base).expanduser() / PLUGIN_NAME
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if local:
            return Path(local) / "DroidAutoContinue"
    return Path.home() / ".local" / "state" / PLUGIN_NAME


def state_database() -> Path:
    return state_directory() / "streaks.sqlite3"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_payload() -> dict[str, Any] | None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


def expand_path(value: str) -> Path:
    return Path(value).expanduser()


def load_transcript_records(transcript_path: str) -> list[dict[str, Any]]:
    path = expand_path(transcript_path)
    try:
        size = path.stat().st_size
    except OSError:
        return []
    tail_limit = env_int(
        "DROID_AUTO_CONTINUE_TRANSCRIPT_TAIL_BYTES",
        DEFAULT_TRANSCRIPT_TAIL_BYTES,
        minimum=4096,
        maximum=MAX_TRANSCRIPT_TAIL_BYTES,
    )
    try:
        with path.open("rb") as handle:
            if size > tail_limit:
                handle.seek(size - tail_limit)
                blob = handle.read()
                newline = blob.find(b"\n")
                if newline >= 0:
                    blob = blob[newline + 1 :]
            else:
                blob = handle.read()
    except OSError:
        return []

    records: list[dict[str, Any]] = []
    for line in blob.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def block_text(block: dict[str, Any]) -> str:
    text = block.get("text")
    return text if isinstance(text, str) else ""


def assistant_is_silent(message: dict[str, Any]) -> bool:
    """思考块或空消息视为静默中断。正文、工具调用和未知块都不算静默。"""
    content = message.get("content")
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if not isinstance(content, list):
        return False
    if not content:
        return True

    for block in content:
        if not isinstance(block, dict):
            return False
        kind = block.get("type")
        if kind in SILENT_BLOCK_TYPES:
            continue
        if kind in TEXT_BLOCK_TYPES:
            if block_text(block).strip():
                return False
            continue
        return False
    return True


def last_assistant_record(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in reversed(records):
        if record.get("type") != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        if message.get("role") != "assistant":
            return None
        return record
    return None


def assistant_visible_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in TEXT_BLOCK_TYPES:
            continue
        parts.append(block_text(block))
    return "".join(parts)


def assistant_has_tool_use(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") in TOOL_BLOCK_TYPES for block in content
    )


def assistant_has_unknown_block(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    known = SILENT_BLOCK_TYPES | TEXT_BLOCK_TYPES | TOOL_BLOCK_TYPES
    return any(
        not isinstance(block, dict) or block.get("type") not in known for block in content
    )


def text_is_compaction_summary(text: str) -> bool:
    """压缩摘要以大模型自述的 <analysis>/<summary> 包裹，不是给用户的答复。"""
    stripped = text.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if not lowered.startswith("<analysis>") and not lowered.startswith("<summary>"):
        return False
    return any(f"<{tag}>" in lowered for tag in SUMMARY_TAGS) and "</summary>" in lowered


def text_has_open_code_fence(text: str) -> bool:
    """只认行首的围栏，正文里引用的 ``` 不算。"""
    open_marker = ""
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        if len(line) - len(stripped) > 3:
            continue
        fence = stripped[:3]
        if fence not in {CODE_FENCE, "~~~"}:
            continue
        marker = stripped[0]
        if not open_marker:
            open_marker = marker
        elif open_marker == marker:
            open_marker = ""
    return bool(open_marker)


def text_is_truncated(text: str) -> bool:
    """未闭合的代码块或摘要信封说明输出在半途被切断。"""
    if text_has_open_code_fence(text):
        return True
    lowered = text.lstrip().lower()
    if not lowered.startswith(("<analysis>", "<summary>")):
        return False
    for tag in SUMMARY_TAGS:
        if lowered.count(f"<{tag}>") > lowered.count(f"</{tag}>"):
            return True
    return False


def text_ends_with_leaked_tool_markup(text: str) -> bool:
    """正文以工具调用协议片段收尾：模型想调工具，片段却当正文发了出来。

    只认落在末尾的标记。正文中间引用这些标记，或整段写在代码块里讨论，
    最后几个字符不是标记本身，因此不会命中。
    """
    return bool(LEAKED_MARKUP_TAIL.search(text))


def text_looks_like_a_question(text: str) -> bool:
    tail = text.strip()[-400:]
    if not tail:
        return False
    stripped = tail.rstrip("`*_> \t\r\n")
    if stripped.endswith(("?", "？")):
        return True
    if stripped.endswith(("。", ".", "!", "！")):
        return False
    return any(phrase in stripped[-120:] for phrase in QUESTION_PHRASES)


def todo_label(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str) and content.strip():
        label = " ".join(content.split())
        return label[:MAX_TODO_LABEL_CHARS]
    identifier = item.get("id")
    return str(identifier) if identifier is not None else "未命名待办"


def todo_status(item: dict[str, Any]) -> str:
    status = item.get("status")
    return status.strip().lower() if isinstance(status, str) else ""


def validated_todo_items(items: Any) -> list[dict[str, Any]] | None:
    """每一项都带 `status` 时才认可整份列表。"""
    if not isinstance(items, list):
        return None
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not todo_status(item):
            return None
        result.append(item)
    return result


def todo_items_from_input(value: Any) -> list[dict[str, Any]] | None:
    """只认 TodoWrite 入参里结构化的待办项。"""
    if not isinstance(value, dict):
        return None
    return validated_todo_items(value.get("todos"))


def todo_items_from_state(state: dict[str, Any]) -> list[dict[str, Any]] | None:
    """读出 Droid 写在 transcript 里的 todo_state 待办项。"""
    if state.get("type") != "todo_state":
        return None
    todos = state.get("todos")
    if not isinstance(todos, dict):
        return None
    return validated_todo_items(todos.get("todos")) or None


def normalized_text(value: str) -> str:
    return " ".join(value.split())


def free_text_line_parsed(line: str, item: dict[str, Any]) -> bool:
    """这一行是否被 Droid 真正解析过：条目的 `content` 已经脱离了原行。"""
    content = item.get("content")
    if not isinstance(content, str):
        return False
    source = normalized_text(LIST_NUMBER_PREFIX.sub("", line))
    return bool(source) and normalized_text(content) != source


def free_text_todo_items(
    text: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """核对 Droid 是否真的解析了自由文本里的每一行。

    证据只取 todo_state。但 Droid 解析不出状态时会把整行原样保留、状态落成
    `pending`，这种 `pending` 是默认值，不是模型写的。判别不靠状态词表：
    解析成功的那一行，`content` 一定比原行短（状态标记和序号被剥掉）；
    解析失败的那一行，`content` 与原行只差行首序号。只要有任意一行两者相等，
    这份清单就整份弃权。这样模型怎么写状态都不影响判定。
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(lines) != len(items):
        return None
    for line, item in zip(lines, items):
        if not free_text_line_parsed(line, item):
            return None
    return items


def todo_evidence_for_call(
    records: list[dict[str, Any]],
    call_index: int,
    block: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """按 TodoWrite 的入参形态决定这一处调用能否作为待办证据。"""
    value = block.get("input")
    items = todo_items_from_input(value)
    if items is not None:
        return items
    if not isinstance(value, dict):
        return None
    entries = value.get("todos")
    if not isinstance(entries, str):
        return None
    for record in records[call_index + 1 :]:
        state_items = todo_items_from_state(record)
        if state_items is not None:
            return free_text_todo_items(entries, state_items)
    return None


def iter_tool_calls(
    records: list[dict[str, Any]],
) -> Iterator[tuple[int, int, dict[str, Any]]]:
    """按 transcript 顺序产出 assistant 消息里的工具调用块。"""
    for index, record in enumerate(records):
        if record.get("type") != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for position, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") in TOOL_BLOCK_TYPES:
                yield index, position, block


def is_todo_write(block: dict[str, Any]) -> bool:
    name = block.get("name")
    return isinstance(name, str) and name.replace("_", "").lower() in TODO_TOOL_NAMES


def iter_todo_write_calls(
    records: list[dict[str, Any]],
) -> Iterator[tuple[int, int, dict[str, Any]]]:
    for call in iter_tool_calls(records):
        if is_todo_write(call[2]):
            yield call


def work_after_call(
    records: list[dict[str, Any]],
    call_index: int,
    call_position: int,
) -> bool:
    """最后一次 TodoWrite 之后是否还有工具调用。

    实测里模型常常先更新计划、再干完收尾的活、然后直接写总结，计划里那条
    `in_progress` 就这么留下来了。这种计划已经过时，不能当作未完成的证据。
    """
    return any(
        (index, position) > (call_index, call_position)
        for index, position, _ in iter_tool_calls(records)
    )


def last_todo_tool_items(records: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """返回最后一次 TodoWrite 采信后的待办项；无证据时为 None。"""
    calls = list(iter_todo_write_calls(records))
    if not calls:
        return None
    call_index, call_position, block = calls[-1]
    if work_after_call(records, call_index, call_position):
        return None
    return todo_evidence_for_call(records, call_index, block)


def last_tool_call_marker(records: list[dict[str, Any]]) -> str:
    """transcript 里最后一次工具调用的身份，用来判断两次中断之间有没有干活。

    Droid 在整个自动续跑链里一直把 `stop_hook_active` 置为 true，光靠这个标志
    区分不了「连续中断」和「干了很久之后又中断一次」。模型真的跑过工具，说明
    计数该重新开始：实测一次会话里 104 次工具调用之后仍被算成连续第 3 次，
    第 4 次用完后再中断就不续跑了。
    """
    marker = ""
    for _, _, block in iter_tool_calls(records):
        call_id = block.get("id")
        if isinstance(call_id, str) and call_id.strip():
            marker = "id:" + call_id.strip()
        else:
            marker = "digest:" + digest(
                json.dumps(block, ensure_ascii=False, sort_keys=True, default=str)
            )
    return marker


def pending_todo_labels(records: list[dict[str, Any]]) -> list[str]:
    items = last_todo_tool_items(records)
    if not items:
        return []
    return [
        todo_label(item)
        for item in items
        if todo_status(item) in PENDING_TODO_STATUSES
    ]


def format_todo_detail(labels: list[str]) -> str:
    shown = labels[:MAX_TODO_LABELS]
    detail = "未完成项：" + "；".join(shown)
    if len(labels) > len(shown):
        detail += f" 等 {len(labels)} 项"
    return detail + "。"


def classify_text(
    text: str,
    labels: list[str],
    *,
    detect_unfinished: bool,
) -> tuple[str, str]:
    """可见正文上的未完成判定。空白正文视为已经完成。"""
    if not text.strip() or not detect_unfinished:
        return ENDING_COMPLETE, ""
    if text_is_compaction_summary(text):
        return ENDING_SUMMARY, ""
    if text_is_truncated(text):
        return ENDING_TRUNCATED, ""
    if text_ends_with_leaked_tool_markup(text):
        return ENDING_LEAK, ""
    if text_looks_like_a_question(text):
        return ENDING_COMPLETE, ""
    if labels:
        return ENDING_TODOS, format_todo_detail(labels)
    return ENDING_COMPLETE, ""


def classify_ending(
    records: list[dict[str, Any]],
    message: dict[str, Any],
    *,
    detect_unfinished: bool = True,
) -> tuple[str, str]:
    """判断这一轮是否真的结束。返回 (结论, 细节)。"""
    if assistant_has_tool_use(message):
        return ENDING_TOOL_USE, ""
    if assistant_is_silent(message):
        return ENDING_SILENT, ""
    if assistant_has_unknown_block(message):
        return ENDING_UNKNOWN, ""
    return classify_text(
        assistant_visible_text(message),
        pending_todo_labels(records),
        detect_unfinished=detect_unfinished,
    )


def assistant_identity(record: dict[str, Any]) -> str:
    record_id = record.get("id")
    if isinstance(record_id, str) and record_id.strip():
        return record_id.strip()
    message = record.get("message")
    message_id = message.get("id") if isinstance(message, dict) else None
    if isinstance(message_id, str) and message_id.strip():
        return message_id.strip()
    return digest(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))


def delay_seconds(attempt: int) -> float:
    base = env_float(
        "DROID_AUTO_CONTINUE_BASE_DELAY",
        DEFAULT_BASE_DELAY_SECONDS,
        minimum=0.0,
        maximum=MAX_SLEEP_SECONDS,
    )
    cap = env_float(
        "DROID_AUTO_CONTINUE_MAX_DELAY",
        DEFAULT_MAX_DELAY_SECONDS,
        minimum=0.0,
        maximum=MAX_SLEEP_SECONDS,
    )
    factor = env_float(
        "DROID_AUTO_CONTINUE_BACKOFF_FACTOR",
        DEFAULT_BACKOFF_FACTOR,
        minimum=1.0,
        maximum=4.0,
    )
    if cap <= 0 or base <= 0:
        return 0.0
    delay = base * (factor ** max(0, attempt - 1))
    return min(cap, MAX_SLEEP_SECONDS, delay)


def connect() -> sqlite3.Connection:
    path = state_database()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
    connection.execute(f"PRAGMA busy_timeout = {int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}")
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS streaks (
            scope_hash TEXT PRIMARY KEY,
            attempts INTEGER NOT NULL CHECK(
                attempts >= 0 AND attempts <= {MAX_MAX_ATTEMPTS}
            ),
            assistant_hash TEXT,
            last_seen INTEGER NOT NULL CHECK(last_seen >= 0),
            progress TEXT
        )
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(streaks)")}
    if "progress" not in columns:
        connection.execute("ALTER TABLE streaks ADD COLUMN progress TEXT")
    connection.commit()
    return connection


def claim_attempt(
    session_id: str,
    assistant_id: str,
    progress: str,
    *,
    stop_hook_active: bool,
    max_attempts: int,
    ttl_seconds: int,
) -> int | None:
    if max_attempts <= 0:
        return None

    scope_hash = digest(session_id.strip())
    assistant_hash = digest(assistant_id)
    now = int(time.time())
    try:
        connection = connect()
    except (OSError, sqlite3.Error):
        return None

    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM streaks WHERE last_seen < ?",
            (now - ttl_seconds,),
        )
        row = connection.execute(
            "SELECT attempts, assistant_hash, last_seen, progress FROM streaks WHERE scope_hash = ?",
            (scope_hash,),
        ).fetchone()
        if row is not None:
            attempts, previous_assistant, last_seen, previous_progress = row
            if (
                type(attempts) is not int
                or attempts < 0
                or attempts > MAX_MAX_ATTEMPTS
                or (previous_assistant is not None and not isinstance(previous_assistant, str))
                or type(last_seen) is not int
                or last_seen < 0
                or (previous_progress is not None and not isinstance(previous_progress, str))
            ):
                connection.commit()
                return None
            if previous_assistant == assistant_hash:
                connection.commit()
                return None
            if progress and previous_progress != progress:
                attempts = 0
        else:
            attempts = 0

        next_attempt = attempts + 1 if stop_hook_active else 1
        if next_attempt > max_attempts:
            connection.execute(
                """
                INSERT INTO streaks (scope_hash, attempts, assistant_hash, last_seen, progress)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope_hash) DO UPDATE SET
                    attempts = excluded.attempts,
                    assistant_hash = excluded.assistant_hash,
                    last_seen = excluded.last_seen,
                    progress = excluded.progress
                """,
                (scope_hash, next_attempt, assistant_hash, now, progress),
            )
            connection.commit()
            return None

        connection.execute(
            """
            INSERT INTO streaks (scope_hash, attempts, assistant_hash, last_seen, progress)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope_hash) DO UPDATE SET
                attempts = excluded.attempts,
                assistant_hash = excluded.assistant_hash,
                last_seen = excluded.last_seen,
                progress = excluded.progress
            """,
            (scope_hash, next_attempt, assistant_hash, now, progress),
        )
        connection.commit()
        return next_attempt
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        connection.close()


def run_failure_hook(payload: dict[str, Any]) -> int:
    """PostModelRequest 是观测钩子，Codex 只记日志、不解读输出。

    所以这里不能靠 decision 让 Codex 续跑，只能自己拉起续跑进程；
    systemMessage 只是让 TUI 上有一行提示。
    """
    if not handle_failure(payload):
        return 0
    attempt = env_int(
        "DROID_AUTO_CONTINUE_FAILURE_MAX_ATTEMPTS",
        DEFAULT_FAILURE_MAX_ATTEMPTS,
        minimum=0,
        maximum=MAX_MAX_ATTEMPTS,
    )
    model = codex_model_name(payload)
    message = f"{model} 因 API 失败中断，已退避后重新拉起续跑（上限 {attempt} 次）。"
    print(json.dumps({"systemMessage": message}, ensure_ascii=False))
    return 0


def build_reason(
    kind: str,
    detail: str,
    attempt: int,
    max_attempts: int,
    *,
    todo_tool: str = "TodoWrite",
) -> str:
    head = CONTINUATION_REASONS.get(kind, CONTINUATION_REASON)
    if detail:
        head = f"{head}{detail}"
    if kind == ENDING_TODOS:
        head = (
            f"{head}先对账：这几项如果其实已经做完，用 {todo_tool} 把它们标成 completed；"
            "如果确实还没做完，说清还缺什么再动手。"
        )
    return f"{head}{CONTINUATION_TAIL}这是自动续跑 {attempt}/{max_attempts}。"


def clean_model_name(value: str) -> str:
    cleaned = " ".join(value.split())
    return cleaned[:MAX_MODEL_NAME_CHARS]


def model_id_from_message(message: dict[str, Any]) -> str | None:
    for key in ("modelId", "model"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def name_from_model_id(model_id: str) -> str:
    """Droid 的自定义模型 id 是 custom:<显示名>-<序号>。"""
    body = model_id[7:] if model_id.startswith("custom:") else model_id
    dash = body.rfind("-")
    if dash > 0:
        suffix = body[dash + 1 :]
        if suffix.isdigit() and str(int(suffix)) == suffix:
            body = body[:dash]
    return clean_model_name(body)


def settings_candidates(cwd: str | None) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path in seen:
            return
        seen.add(path)
        candidates.append(path)

    if isinstance(cwd, str) and cwd.strip():
        current = Path(cwd).expanduser()
        for _ in range(12):
            factory = current / ".factory"
            add(factory / "settings.local.json")
            add(factory / "settings.json")
            if (current / ".git").exists():
                break
            parent = current.parent
            if parent == current:
                break
            current = parent
    home_factory = Path.home() / ".factory"
    add(home_factory / "settings.local.json")
    add(home_factory / "settings.json")
    return candidates


def custom_models(path: Path) -> list[dict[str, Any]]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_SETTINGS_BYTES:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return []
    if not isinstance(data, dict):
        return []

    models: list[dict[str, Any]] = []
    containers: list[Any] = [data]
    general = data.get("general")
    if isinstance(general, dict):
        containers.append(general)
    for container in containers:
        entries = container.get("customModels")
        if not isinstance(entries, list):
            continue
        models.extend(entry for entry in entries if isinstance(entry, dict))
    return models


def configured_display_name(model_id: str, cwd: str | None) -> str | None:
    for path in settings_candidates(cwd):
        for model in custom_models(path):
            if model.get("id") != model_id:
                continue
            display_name = model.get("displayName")
            if isinstance(display_name, str) and display_name.strip():
                return clean_model_name(display_name)
            provider_model = model.get("model")
            if isinstance(provider_model, str) and provider_model.strip():
                return clean_model_name(provider_model)
    return None


def model_display_name(message: dict[str, Any], cwd: str | None) -> str:
    model_id = model_id_from_message(message)
    if model_id is None:
        return UNKNOWN_MODEL_NAME
    configured = configured_display_name(model_id, cwd)
    if configured:
        return configured
    parsed = name_from_model_id(model_id)
    return parsed or UNKNOWN_MODEL_NAME


def build_system_message(model_name: str, attempt: int, max_attempts: int, kind: str) -> str:
    if kind == ENDING_SILENT:
        return f"{model_name} 发生中断，正在自动重试 {attempt}/{max_attempts}..."
    return f"{model_name} 任务尚未完成，正在自动续跑 {attempt}/{max_attempts}..."


def is_codex_rollout(records: list[dict[str, Any]]) -> bool:
    return any(record.get("type") == "response_item" for record in records)


def codex_payloads(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for record in records:
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        payloads.append(payload if isinstance(payload, dict) else {"type": "__invalid__"})
    return payloads


def is_codex_boundary(payload: dict[str, Any]) -> bool:
    """用户消息和工具结果把模型的一次采样隔开。"""
    kind = payload.get("type")
    if not isinstance(kind, str):
        return False
    if kind == "message":
        return payload.get("role") != "assistant"
    if kind.endswith("_output"):
        return True
    return kind in CODEX_BOUNDARY_TYPES


def codex_last_step(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """最后一次采样：最后一条用户消息或工具结果之后的模型输出。"""
    known = CODEX_TOOL_TYPES | {"reasoning", "message", "compaction", "context_compaction"}
    step: list[dict[str, Any]] = []
    for payload in payloads:
        if is_codex_boundary(payload):
            step = []
            continue
        kind = payload.get("type")
        if kind == "message" and payload.get("role") == "assistant":
            step.append(payload)
            continue
        if kind in known:
            step.append(payload)
            continue
        step.append({"type": "__unknown__"})
    return step


def codex_message_parts(payload: dict[str, Any]) -> tuple[str, bool]:
    content = payload.get("content")
    if content is None:
        return "", False
    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return "", True
    parts: list[str] = []
    unknown = False
    for block in content:
        if not isinstance(block, dict):
            unknown = True
            continue
        if block.get("type") not in CODEX_TEXT_BLOCK_TYPES:
            unknown = True
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts), unknown


def normalized_tool_name(value: str) -> str:
    leaf = value.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    return leaf.replace("_", "").replace("-", "").lower()


def codex_call_arguments(payload: dict[str, Any]) -> Any:
    raw = payload.get("arguments") if "arguments" in payload else payload.get("input")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw


def codex_plan_items(value: Any) -> list[dict[str, Any]] | None:
    """Codex 的 update_plan 用 plan[].step；也接受已经是 todos[].content 的入参。"""
    if not isinstance(value, dict):
        return None
    plan = value.get("plan")
    if isinstance(plan, list):
        items: list[dict[str, Any]] = []
        for entry in plan:
            if not isinstance(entry, dict):
                return None
            label = entry.get("step")
            if not isinstance(label, str) or not label.strip():
                label = entry.get("content")
            status = entry.get("status")
            if not isinstance(label, str) or not label.strip():
                return None
            if not isinstance(status, str) or not status.strip():
                return None
            items.append({"content": label, "status": status})
        return items
    if "todos" in value:
        return validated_todo_items(value.get("todos"))
    return None


def codex_pending_labels(payloads: list[dict[str, Any]]) -> list[str]:
    """最后一次结构化 update_plan。之后又有工具调用则计划过时，弃权。"""
    last_index: int | None = None
    last_items: list[dict[str, Any]] | None = None
    for index, payload in enumerate(payloads):
        if payload.get("type") not in {"function_call", "custom_tool_call"}:
            continue
        name = payload.get("name")
        if not isinstance(name, str) or normalized_tool_name(name) not in CODEX_PLAN_NAMES:
            continue
        last_index = index
        last_items = codex_plan_items(codex_call_arguments(payload))
    if last_index is None or not last_items:
        return []
    if any(payload.get("type") in CODEX_TOOL_TYPES for payload in payloads[last_index + 1 :]):
        return []
    return [
        todo_label(item)
        for item in last_items
        if todo_status(item) in PENDING_TODO_STATUSES
    ]


def classify_codex(
    payloads: list[dict[str, Any]],
    *,
    detect_unfinished: bool,
) -> tuple[str, str] | None:
    """把 Codex 最后一次采样收成和 Droid 相同的结论。没有模型输出时返回 None。"""
    step = codex_last_step(payloads)
    if not step:
        return None
    if any(item.get("type") == "__unknown__" for item in step):
        return ENDING_UNKNOWN, ""
    if any(item.get("type") in CODEX_TOOL_TYPES for item in step):
        return ENDING_TOOL_USE, ""

    texts: list[str] = []
    unknown = False
    saw_assistant = False
    saw_reasoning = False
    saw_compaction = False
    for item in step:
        kind = item.get("type")
        if kind == "reasoning":
            saw_reasoning = True
        elif kind in {"compaction", "context_compaction"}:
            saw_compaction = True
        elif kind == "message":
            saw_assistant = True
            text, bad = codex_message_parts(item)
            texts.append(text)
            unknown = unknown or bad
    if unknown:
        return ENDING_UNKNOWN, ""
    text = "".join(texts)
    if text.strip():
        return classify_text(
            text,
            codex_pending_labels(payloads),
            detect_unfinished=detect_unfinished,
        )
    if saw_compaction and not saw_reasoning and not saw_assistant:
        if not detect_unfinished:
            return ENDING_COMPLETE, ""
        return ENDING_SUMMARY, ""
    if saw_reasoning or saw_assistant or saw_compaction:
        return ENDING_SILENT, ""
    return None


def codex_step_identity(step: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in step:
        found = False
        for key in ("id", "call_id"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
                found = True
                break
        if not found:
            parts.append(
                digest(json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
            )
    return "\n".join(parts)


def codex_tool_marker(payloads: list[dict[str, Any]]) -> str:
    marker = ""
    for payload in payloads:
        if payload.get("type") not in CODEX_TOOL_TYPES:
            continue
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = payload.get("id")
        if isinstance(call_id, str) and call_id.strip():
            marker = "id:" + call_id.strip()
        else:
            marker = "digest:" + digest(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            )
    return marker


def codex_model_name(stop_payload: dict[str, Any]) -> str:
    model = stop_payload.get("model")
    if isinstance(model, str) and model.strip():
        return clean_model_name(model)
    return UNKNOWN_MODEL_NAME


def failure_is_retryable(message: str) -> bool:
    """只有限流、服务端临时故障和连接中断值得重试。

    先排除终态错误再认可重试特征：认证失败的消息里也可能带 "429" 之外的状态码，
    反过来 5xx 的正文里也可能出现 "invalid" 之类的词。终态优先，避免白等。
    """
    text = message.strip().lower()
    if not text:
        return False
    if any(hint in text for hint in TERMINAL_FAILURE_HINTS):
        return False
    if any(hint in text for hint in RETRYABLE_FAILURE_HINTS):
        return True
    return bool(RETRYABLE_HTTP_STATUS.search(text))


def failure_delay_seconds(attempt: int) -> float:
    """指数退避：第 1 次等 base，之后每次乘 factor，封顶 cap。

    上限还要压到 hook 能安全睡完的时长，否则 hook 会被 Codex 超时杀掉，
    等待就白费了。
    """
    base = env_float(
        "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY",
        DEFAULT_FAILURE_BASE_DELAY_SECONDS,
        minimum=0.0,
        maximum=MAX_HOOK_SLEEP_SECONDS,
    )
    cap = env_float(
        "DROID_AUTO_CONTINUE_FAILURE_MAX_DELAY",
        DEFAULT_FAILURE_MAX_DELAY_SECONDS,
        minimum=0.0,
        maximum=MAX_HOOK_SLEEP_SECONDS,
    )
    factor = env_float(
        "DROID_AUTO_CONTINUE_BACKOFF_FACTOR",
        DEFAULT_BACKOFF_FACTOR,
        minimum=1.0,
        maximum=4.0,
    )
    if base <= 0 or cap <= 0:
        return 0.0
    delay = base * (factor ** max(0, attempt - 1))
    return min(cap, MAX_HOOK_SLEEP_SECONDS, delay)


def resume_launch_overrides() -> list[str]:
    """还原启动 Codex 时的命令行配置。

    续跑拉起的 `codex exec resume` 是**新进程**，它只从 config.toml 和自身参数
    取值。原进程那些只存在于命令行的设置会全部丢掉，实测有两类后果：

    1. 自定义 provider（很多网关启动脚本这么写）丢失，直接报
       `model provider is no longer configured`。
    2. **沙箱静默降级**：原进程用 `-s danger-full-access` 启动时，续跑进程
       会退回默认的 read-only。模型接着要写文件、跑命令，全部失败。
       `codex exec resume` 不接受 `-s/--sandbox`，只能靠 `-c sandbox_mode=...`
       还原。

    所以把父进程的 `-c` / `--profile` / `-s` / `--sandbox` /
    `--ask-for-approval` 全部捞回来。读不到 argv 时返回空列表，
    退化成只用 config.toml，不猜。
    """
    argv = parent_process_argv()
    overrides: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ("-c", "--config", "-p", "--profile", "--sandbox", "--ask-for-approval"):
            if index + 1 < len(argv):
                if token in ("--sandbox",):
                    # resume 不认 --sandbox，换成等价的 config 覆盖。
                    overrides.extend(["-c", f'sandbox_mode="{argv[index + 1]}"'])
                elif token == "--ask-for-approval":
                    overrides.extend(["-c", f'approval_policy="{argv[index + 1]}"'])
                else:
                    overrides.extend([token, argv[index + 1]])
                index += 2
                continue
            index += 1
            continue
        if token in ("-s",):
            if index + 1 < len(argv):
                overrides.extend(["-c", f'sandbox_mode="{argv[index + 1]}"'])
                index += 2
                continue
            index += 1
            continue
        if token.startswith(("--sandbox=",)):
            overrides.extend(["-c", f'sandbox_mode="{token.split("=", 1)[1]}"'])
        elif token.startswith("--ask-for-approval="):
            overrides.extend(["-c", f'approval_policy="{token.split("=", 1)[1]}"'])
        elif token.startswith(("--config=", "-c=", "--profile=")):
            overrides.append(token)
        elif token == "--dangerously-bypass-approvals-and-sandbox":
            # 原进程完全关掉了沙箱；续跑也必须一致，否则模型写不了文件。
            overrides.append(token)
        index += 1
    return overrides


def parent_process_argv() -> list[str]:
    """往上找到最近的 codex 进程，返回它**未经改写**的 argv。

    续跑需要还原原进程的命令行配置，所以必须拿到精确的 argv。
    """
    pid = os.getppid()
    for _ in range(8):
        argv = process_argv(pid)
        if argv and looks_like_codex_argv(argv):
            return argv
        parent = parent_process_id(pid)
        if parent is None or parent <= 1:
            return []
        pid = parent
    return []


def looks_like_codex_argv(argv: list[str]) -> bool:
    if not argv:
        return False
    executable = os.path.basename(argv[0]).lower()
    return executable.startswith("codex")


def process_argv(pid: int) -> list[str]:
    if sys.platform == "darwin":
        return darwin_process_argv(pid)
    return linux_process_argv(pid)


def linux_process_argv(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def darwin_process_argv(pid: int) -> list[str]:
    """macOS 用 sysctl(KERN_PROCARGS2) 取原始 argv。

    不能用 `ps -o args=`：它把 argv 用空格拼成一行，参数内部的空格无法还原。
    实测 apus_codex.sh 传的是
    `-c 'model_providers.azure-openai={name="Azure OpenAI",...}'`，
    按空格切会拆成 `name="Azure` 和 `OpenAI",...}` 两段，续跑进程就拿到
    一个坏掉的 provider 定义。KERN_PROCARGS2 返回 NUL 分隔的原始字节，
    能精确还原每一个参数。
    """
    try:
        import ctypes
        import ctypes.util
    except ImportError:
        return []
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "/usr/lib/libSystem.B.dylib")
        # 只有 macOS 的 libc 导出 sysctl。Linux 上取这个符号会抛
        # AttributeError，Python < 3.12 的 ctypes 不会把它包成 OSError，
        # 所以必须先探一次，否则插件在非 macOS 上会直接崩掉。
        sysctl = libc.sysctl
    except (OSError, AttributeError):
        return []
    # CTL_KERN = 1, KERN_PROCARGS2 = 49
    mib = (ctypes.c_int * 3)(1, 49, pid)
    size = ctypes.c_size_t(0)
    try:
        if sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
            return []
        if size.value <= 0:
            return []
        buffer = ctypes.create_string_buffer(size.value)
        if sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            return []
    except (OSError, ValueError, AttributeError):
        return []
    return parse_procargs2(buffer.raw[: size.value])


def parse_procargs2(data: bytes) -> list[str]:
    """解析 KERN_PROCARGS2 的缓冲区。

    布局：`int32 argc | 可执行路径 NUL | 若干填充 NUL | argv[0..argc-1] NUL* |
    环境变量 NUL*`。只取前 argc 段，环境变量不参与。
    """
    if len(data) < 4:
        return []
    argc = int.from_bytes(data[:4], sys.byteorder)
    if argc <= 0 or argc > MAX_PROCESS_ARG_COUNT:
        return []
    rest = data[4:]
    end = rest.find(b"\0")
    if end < 0:
        return []
    rest = rest[end + 1 :]
    # 可执行路径与环境之间有一段填充 NUL。
    rest = rest.lstrip(b"\0")
    argv: list[str] = []
    for _ in range(argc):
        end = rest.find(b"\0")
        if end < 0:
            break
        argv.append(rest[:end].decode("utf-8", "replace"))
        rest = rest[end + 1 :]
    return argv


def parent_process_id(pid: int) -> int | None:
    if sys.platform == "darwin":
        # macOS 没有 /proc/<pid>/stat，用 ps 取父进程号（这里只取数字，
        # 不碰命令行，所以不受空格问题影响）。
        try:
            completed = subprocess.run(
                ["/bin/ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=PROCESS_QUERY_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = completed.stdout.strip()
        return int(value) if value.isdigit() else None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().split()
    except OSError:
        return None
    if len(stat) < 4:
        return None
    try:
        return int(stat[3])
    except ValueError:
        return None


def thread_writer_lock_held(session_id: str) -> bool:
    """发起回合的 Codex 进程是否还持有该会话的写锁。

    Codex 用 <CODEX_HOME>/thread-writer-locks/<thread>.lock 标记活跃写者，并且
    在会话空闲 60 秒（`thread_unload_delay_secs`）后才释放。锁还在时外部
    `codex exec resume` 一定会被拒，先探一下能省掉一次必然失败的重试，
    也不至于白等一轮退避。

    **只用共享锁探测**：Codex 的 WriterLockGuard 在 drop 时要抢独占锁来清理
    文件，探测若占独占锁会把它的清理挡回去。共享锁不会写文件，也不留下
    任何状态。读不到锁目录时按「未持有」处理，让续跑自己去试。
    """
    if session_id.startswith("."):
        return False
    codex_home = os.environ.get("CODEX_HOME")
    if not codex_home or not codex_home.strip():
        codex_home = str(Path.home() / ".codex")
    lock_path = Path(codex_home).expanduser() / "thread-writer-locks" / f"{session_id}.lock"
    try:
        handle = lock_path.open("rb")
    except OSError:
        return False
    try:
        try:
            import fcntl
        except ImportError:
            # 非 POSIX 平台没有 fcntl，交给续跑自己去判断。
            return False
        try:
            # LOCK_SH: 与写者的 LOCK_EX 互斥，但多个探测之间不互斥，也不妨碍
            # Codex 自己的清理流程。
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
        finally:
            handle.close()
    except OSError:
        return False


def spawn_resume(session_id: str, cwd: str | None, delay: float) -> bool:
    """拉起一个脱离的辅助进程：先退避，等锁释放后再 resume。

    两件事都必须离开 hook 进程：

    - **退避**：hook 有自己的超时，睡在 hook 里会挤占 Codex 的收尾时间。
    - **等锁**：插件在 `PostModelRequest` 里运行时，发起回合的 Codex 进程
      仍然持有该会话的写锁（实测锁在 PostModelRequest 执行期间是 HELD，
      进程退出才释放）。此时直接 resume 必定报 `already has an active writer`。
      辅助进程轮询等锁，拿到窗口后再续跑。

    用 DEVNULL + 独立会话组：hook 退出不会连坐杀掉它。

    启动参数必须在**这里**算好再传过去。辅助进程是 hook 的子进程，等它跑起来
    时 hook 早已退出，`/proc/<ppid>` 指到的是别的进程，还原不出原始 argv。
    实测过这个坑：续跑进程会拿不到 `-s danger-full-access`，沙箱静默降级成
    read-only，模型接着写文件全部失败。
    """
    executable = shutil.which("codex")
    if not executable:
        return False
    overrides = json.dumps(resume_launch_overrides(), ensure_ascii=False)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--resume-after-lock",
        session_id,
        cwd or "",
        f"{delay:.3f}",
        overrides,
    ]
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=dict(os.environ),
        )
    except (OSError, ValueError):
        return False
    return True


def resume_after_lock(
    session_id: str,
    cwd: str | None,
    delay: float,
    overrides: list[str],
) -> int:
    """辅助进程主体：退避 → 等写锁释放 → 续跑。

    `overrides` 由 hook 进程在启动本进程时算好传进来，不能在这里重算：
    那时 hook 已经退出，父进程换人了，还原不出原始启动参数。
    """
    if delay > 0:
        time.sleep(delay)
    deadline = time.time() + LOCK_WAIT_TIMEOUT_SECONDS
    while time.time() < deadline:
        if not thread_writer_lock_held(session_id):
            break
        time.sleep(LOCK_POLL_INTERVAL_SECONDS)
    else:
        # 等到超时仍被锁着：Codex 那边还活着，交给下一次失败或用户手工继续。
        return 0

    executable = shutil.which("codex")
    if not executable:
        return 0
    command = [
        executable,
        "exec",
        "resume",
        *overrides,
        "--skip-git-repo-check",
        session_id,
        RESUME_PROMPT,
    ]
    try:
        subprocess.run(
            command,
            cwd=cwd if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, ValueError):
        return 0
    return 0


def handle_failure(payload: dict[str, Any]) -> bool:
    """PostModelRequest 报来的 API 失败：退避后重新拉起续跑。

    返回 True 表示已经安排了续跑，调用方据此输出 systemMessage。
    """
    status = payload.get("status")
    if status != "failed":
        return False
    message = payload.get("error")
    if not isinstance(message, str) or not failure_is_retryable(message):
        return False

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return False
    cwd = payload.get("cwd")
    if not isinstance(cwd, str):
        cwd = None

    max_attempts = env_int(
        "DROID_AUTO_CONTINUE_FAILURE_MAX_ATTEMPTS",
        DEFAULT_FAILURE_MAX_ATTEMPTS,
        minimum=0,
        maximum=MAX_MAX_ATTEMPTS,
    )
    if max_attempts <= 0:
        return False

    attempt = claim_failure_attempt(session_id, message, max_attempts=max_attempts)
    if attempt is None:
        return False

    return spawn_resume(session_id.strip(), cwd, failure_delay_seconds(attempt))


def claim_failure_attempt(
    session_id: str,
    message: str,
    *,
    max_attempts: int,
) -> int | None:
    """失败续跑的计数，按「会话 + 失败指纹」累积。

    同一轮失败可能被多次报告（重复投递、并发子代理），用指纹去重，避免
    同一处失败反复退避、把次数和等待都烧光。
    """
    ttl_seconds = env_int(
        "DROID_AUTO_CONTINUE_STATE_TTL_SECONDS",
        DEFAULT_STATE_TTL_SECONDS,
        minimum=300,
        maximum=7 * 24 * 60 * 60,
    )
    now = int(time.time())
    try:
        connection = connect()
    except (OSError, sqlite3.Error):
        return None
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM streaks WHERE last_seen < ?", (now - ttl_seconds,))
        scope_hash = digest("failure:" + session_id.strip())
        row = connection.execute(
            "SELECT attempts, assistant_hash, last_seen FROM streaks WHERE scope_hash = ?",
            (scope_hash,),
        ).fetchone()
        fingerprint = digest(" ".join(message.split())[:2000])
        if row is None:
            attempts = 0
        else:
            stored_attempts, previous_hash, _ = row
            if type(stored_attempts) is not int or stored_attempts < 0:
                connection.commit()
                return None
            if previous_hash == fingerprint:
                # 同一处失败重复上报：不重复计数，也不再等一次。
                connection.commit()
                return None
            attempts = stored_attempts
        next_attempt = attempts + 1
        if next_attempt > max_attempts:
            connection.execute(
                """
                INSERT INTO streaks (scope_hash, attempts, assistant_hash, last_seen, progress)
                VALUES (?, ?, ?, ?, NULL)
                ON CONFLICT(scope_hash) DO UPDATE SET
                    attempts = excluded.attempts,
                    assistant_hash = excluded.assistant_hash,
                    last_seen = excluded.last_seen
                """,
                (scope_hash, next_attempt, fingerprint, now),
            )
            connection.commit()
            return None
        connection.execute(
            """
            INSERT INTO streaks (scope_hash, attempts, assistant_hash, last_seen, progress)
            VALUES (?, ?, ?, ?, NULL)
            ON CONFLICT(scope_hash) DO UPDATE SET
                attempts = excluded.attempts,
                assistant_hash = excluded.assistant_hash,
                last_seen = excluded.last_seen
            """,
            (scope_hash, next_attempt, fingerprint, now),
        )
        connection.commit()
        return next_attempt
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        connection.close()


def finish_classified(
    *,
    session_id: str,
    stop_hook_active: bool,
    kind: str,
    detail: str,
    assistant_id: str,
    progress: str,
    model_name: str,
    announce_complete: bool,
    todo_tool: str,
) -> int:
    if kind in {ENDING_TOOL_USE, ENDING_UNKNOWN}:
        return 0
    if kind == ENDING_COMPLETE:
        if announce_complete:
            print(NORMAL_EXIT_NOTICE)
        return 0

    max_attempts = env_int(
        "DROID_AUTO_CONTINUE_MAX_ATTEMPTS",
        DEFAULT_MAX_ATTEMPTS,
        minimum=0,
        maximum=MAX_MAX_ATTEMPTS,
    )
    ttl_seconds = env_int(
        "DROID_AUTO_CONTINUE_STATE_TTL_SECONDS",
        DEFAULT_STATE_TTL_SECONDS,
        minimum=300,
        maximum=7 * 24 * 60 * 60,
    )
    attempt = claim_attempt(
        session_id,
        assistant_id,
        progress,
        stop_hook_active=stop_hook_active,
        max_attempts=max_attempts,
        ttl_seconds=ttl_seconds,
    )
    if attempt is None:
        return 0

    delay = delay_seconds(attempt)
    if delay > 0:
        time.sleep(delay)

    print(
        json.dumps(
            {
                "decision": "block",
                "reason": build_reason(
                    kind,
                    detail,
                    attempt,
                    max_attempts,
                    todo_tool=todo_tool,
                ),
                "systemMessage": build_system_message(
                    model_name,
                    attempt,
                    max_attempts,
                    kind,
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    payload = read_payload()
    if payload is None:
        return 0
    event = payload.get("hook_event_name")
    if event == "PostModelRequest":
        return run_failure_hook(payload)
    if event != "Stop":
        return 0

    session_id = payload.get("session_id")
    stop_hook_active = payload.get("stop_hook_active")
    transcript_path = payload.get("transcript_path")
    cwd = payload.get("cwd")
    if not isinstance(cwd, str):
        cwd = None
    if not isinstance(session_id, str) or not session_id.strip():
        return 0
    if not isinstance(stop_hook_active, bool):
        return 0
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        return 0

    records = load_transcript_records(transcript_path)
    detect_unfinished = (
        env_int(
            "DROID_AUTO_CONTINUE_DETECT_UNFINISHED",
            1,
            minimum=0,
            maximum=1,
        )
        == 1
    )
    if is_codex_rollout(records):
        payloads = codex_payloads(records)
        classified = classify_codex(payloads, detect_unfinished=detect_unfinished)
        if classified is None:
            return 0
        kind, detail = classified
        return finish_classified(
            session_id=session_id,
            stop_hook_active=stop_hook_active,
            kind=kind,
            detail=detail,
            assistant_id=codex_step_identity(codex_last_step(payloads)),
            progress=codex_tool_marker(payloads),
            model_name=codex_model_name(payload),
            announce_complete=False,
            todo_tool="update_plan",
        )

    record = last_assistant_record(records)
    if record is None:
        return 0
    message = record.get("message")
    if not isinstance(message, dict):
        return 0

    kind, detail = classify_ending(
        records,
        message,
        detect_unfinished=detect_unfinished,
    )
    return finish_classified(
        session_id=session_id,
        stop_hook_active=stop_hook_active,
        kind=kind,
        detail=detail,
        assistant_id=assistant_identity(record),
        progress=last_tool_call_marker(records),
        model_name=model_display_name(message, cwd),
        announce_complete=True,
        todo_tool="TodoWrite",
    )


if __name__ == "__main__":
    # 内部辅助模式：hook 进程不能等锁，退避和续跑都在脱离的子进程里跑。
    if len(sys.argv) >= 6 and sys.argv[1] == "--resume-after-lock":
        try:
            requested_delay = float(sys.argv[4])
        except ValueError:
            requested_delay = 0.0
        try:
            parsed_overrides = json.loads(sys.argv[5])
        except json.JSONDecodeError:
            parsed_overrides = []
        overrides = [item for item in parsed_overrides if isinstance(item, str)] \
            if isinstance(parsed_overrides, list) else []
        raise SystemExit(
            resume_after_lock(
                sys.argv[2],
                sys.argv[3] or None,
                max(0.0, requested_delay),
                overrides,
            )
        )
    raise SystemExit(main())

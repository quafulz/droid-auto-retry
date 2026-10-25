#!/usr/bin/env python3
"""Droid Stop hook：回合没有真正做完时，有限次自动继续。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
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
PENDING_TODO_STATUSES = {"in_progress", "pending"}
SUMMARY_TAGS = ("analysis", "summary")
CODE_FENCE = "```"
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

ENDING_SILENT = "silent"
ENDING_SUMMARY = "summary"
ENDING_TRUNCATED = "truncated"
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
    ENDING_TODOS: "继续。上一条消息结束时待办列表仍有未完成项。",
}
CONTINUATION_TAIL = (
    "从中断处继续当前任务。先核对已经成功的修改和命令结果，"
    "不要重复已经完成的写入、提交、推送、删除或其他有副作用的操作。"
)
NORMAL_EXIT_NOTICE = "本轮任务正常完成。"
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


def text_is_truncated(text: str) -> bool:
    """未闭合的代码块或摘要标签说明输出在半途被切断。"""
    if text.count(CODE_FENCE) % 2 == 1:
        return True
    lowered = text.lower()
    for tag in SUMMARY_TAGS:
        if lowered.count(f"<{tag}>") > lowered.count(f"</{tag}>"):
            return True
    return False


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


def pending_todo_labels(records: list[dict[str, Any]]) -> list[str]:
    state: dict[str, Any] | None = None
    for record in records:
        if record.get("type") == "todo_state":
            state = record
    if state is None:
        return []
    todos = state.get("todos")
    if isinstance(todos, dict):
        todos = todos.get("todos")
    if not isinstance(todos, list):
        return []
    labels: list[str] = []
    for item in todos:
        if not isinstance(item, dict):
            continue
        if item.get("status") not in PENDING_TODO_STATUSES:
            continue
        labels.append(todo_label(item))
    return labels


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

    text = assistant_visible_text(message)
    if not text.strip():
        return ENDING_COMPLETE, ""
    if not detect_unfinished:
        return ENDING_COMPLETE, ""
    if text_is_compaction_summary(text):
        return ENDING_SUMMARY, ""
    if text_is_truncated(text):
        return ENDING_TRUNCATED, ""
    if text_looks_like_a_question(text):
        return ENDING_COMPLETE, ""
    labels = pending_todo_labels(records)
    if labels:
        shown = labels[:MAX_TODO_LABELS]
        detail = "未完成项：" + "；".join(shown)
        if len(labels) > len(shown):
            detail += f" 等 {len(labels)} 项"
        return ENDING_TODOS, detail + "。"
    return ENDING_COMPLETE, ""


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
            last_seen INTEGER NOT NULL CHECK(last_seen >= 0)
        )
        """
    )
    connection.commit()
    return connection


def claim_attempt(
    session_id: str,
    assistant_id: str,
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
            "SELECT attempts, assistant_hash, last_seen FROM streaks WHERE scope_hash = ?",
            (scope_hash,),
        ).fetchone()
        if row is not None:
            attempts, previous_assistant, last_seen = row
            if (
                type(attempts) is not int
                or attempts < 0
                or attempts > MAX_MAX_ATTEMPTS
                or (previous_assistant is not None and not isinstance(previous_assistant, str))
                or type(last_seen) is not int
                or last_seen < 0
            ):
                connection.commit()
                return None
            if previous_assistant == assistant_hash:
                connection.commit()
                return None
        else:
            attempts = 0

        next_attempt = attempts + 1 if stop_hook_active else 1
        if next_attempt > max_attempts:
            connection.execute(
                """
                INSERT INTO streaks (scope_hash, attempts, assistant_hash, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope_hash) DO UPDATE SET
                    attempts = excluded.attempts,
                    assistant_hash = excluded.assistant_hash,
                    last_seen = excluded.last_seen
                """,
                (scope_hash, next_attempt, assistant_hash, now),
            )
            connection.commit()
            return None

        connection.execute(
            """
            INSERT INTO streaks (scope_hash, attempts, assistant_hash, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(scope_hash) DO UPDATE SET
                attempts = excluded.attempts,
                assistant_hash = excluded.assistant_hash,
                last_seen = excluded.last_seen
            """,
            (scope_hash, next_attempt, assistant_hash, now),
        )
        connection.commit()
        return next_attempt
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        connection.close()


def build_reason(kind: str, detail: str, attempt: int, max_attempts: int) -> str:
    head = CONTINUATION_REASONS.get(kind, CONTINUATION_REASON)
    if detail:
        head = f"{head}{detail}"
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


def main() -> int:
    payload = read_payload()
    if payload is None or payload.get("hook_event_name") != "Stop":
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
    record = last_assistant_record(records)
    if record is None:
        return 0
    message = record.get("message")
    if not isinstance(message, dict):
        return 0

    detect_unfinished = (
        env_int(
            "DROID_AUTO_CONTINUE_DETECT_UNFINISHED",
            1,
            minimum=0,
            maximum=1,
        )
        == 1
    )
    kind, detail = classify_ending(
        records,
        message,
        detect_unfinished=detect_unfinished,
    )
    if kind in {ENDING_TOOL_USE, ENDING_UNKNOWN}:
        return 0
    if kind == ENDING_COMPLETE:
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
        assistant_identity(record),
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
                "reason": build_reason(kind, detail, attempt, max_attempts),
                "systemMessage": build_system_message(
                    model_display_name(message, cwd),
                    attempt,
                    max_attempts,
                    kind,
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

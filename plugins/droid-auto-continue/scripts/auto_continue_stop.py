#!/usr/bin/env python3
"""Droid Stop hook：Custom Model 在思考阶段静默中断时，有限次自动继续。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


PLUGIN_NAME = "droid-auto-continue"
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

CONTINUATION_REASON = (
    "继续。上一轮在思考阶段中断，没有输出正文，也没有发出工具调用。"
    "从中断处继续当前任务。先核对已经成功的修改和命令结果，"
    "不要重复已经完成的写入、提交、推送、删除或其他有副作用的操作。"
)
NORMAL_EXIT_NOTICE = "本轮任务正常完成。"


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


def assistant_has_visible_text(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in TEXT_BLOCK_TYPES:
            continue
        if block_text(block).strip():
            return True
    return False


def assistant_is_silent(message: dict[str, Any]) -> bool:
    """思考块或空消息视为静默中断。正文、工具调用和未知块都不续跑。"""
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


def build_reason(attempt: int, max_attempts: int) -> str:
    return (
        f"{CONTINUATION_REASON}"
        f"这是本轮连续静默中断的自动续跑 {attempt}/{max_attempts}。"
    )


def main() -> int:
    payload = read_payload()
    if payload is None or payload.get("hook_event_name") != "Stop":
        return 0

    session_id = payload.get("session_id")
    stop_hook_active = payload.get("stop_hook_active")
    transcript_path = payload.get("transcript_path")
    if not isinstance(session_id, str) or not session_id.strip():
        return 0
    if not isinstance(stop_hook_active, bool):
        return 0
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        return 0

    record = last_assistant_record(load_transcript_records(transcript_path))
    if record is None:
        return 0
    message = record.get("message")
    if not isinstance(message, dict) or not assistant_is_silent(message):
        if isinstance(message, dict) and assistant_has_visible_text(message):
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
            {"decision": "block", "reason": build_reason(attempt, max_attempts)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

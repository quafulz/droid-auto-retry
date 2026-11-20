from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "custom-model-stop-patch"
SCRIPT = PLUGIN / "scripts" / "auto_continue_stop.py"
HOOKS = PLUGIN / "hooks" / "hooks.json"
CODEX_HOOKS = PLUGIN / "hooks" / "codex-hooks.json"
MANIFEST = PLUGIN / ".factory-plugin" / "plugin.json"
CODEX_MANIFEST = PLUGIN / ".codex-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".factory-plugin" / "marketplace.json"
CODEX_MARKETPLACE = ROOT / ".agents" / "plugins" / "marketplace.json"

ENV_VARS = {
    "DROID_AUTO_CONTINUE_MAX_ATTEMPTS",
    "DROID_AUTO_CONTINUE_BASE_DELAY",
    "DROID_AUTO_CONTINUE_MAX_DELAY",
    "DROID_AUTO_CONTINUE_BACKOFF_FACTOR",
    "DROID_AUTO_CONTINUE_STATE_TTL_SECONDS",
    "DROID_AUTO_CONTINUE_STATE_DIR",
    "DROID_AUTO_CONTINUE_TRANSCRIPT_TAIL_BYTES",
    "DROID_AUTO_CONTINUE_DETECT_UNFINISHED",
    "DROID_AUTO_CONTINUE_FAILURE_MAX_ATTEMPTS",
    "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY",
    "DROID_AUTO_CONTINUE_FAILURE_MAX_DELAY",
    "DROID_AUTO_CONTINUE_LOCK_WAIT_SECONDS",
    "DROID_AUTO_CONTINUE_LOCK_POLL_SECONDS",
    "DROID_AUTO_CONTINUE_QUEUE_SETTLE_SECONDS",
    "DROID_AUTO_CONTINUE_QUEUE_VERIFY_SECONDS",
    "DROID_AUTO_CONTINUE_QUEUE_TIMEOUT_SECONDS",
    "DROID_AUTO_CONTINUE_RESUME_TIMEOUT_SECONDS",
}


def write_fake_codex(
    root: Path,
    *,
    queue_exit: int = 0,
    exec_exit: int = 0,
    queue_message: str = "",
    exec_message: str = "",
    queue_append_task: str | None = None,
    exec_append_task: str | None = None,
    queue_sleep: float = 0,
) -> Path:
    """写一个只记录调用的假 Codex，按子命令模拟成功或失败。"""
    calls = root / "calls.jsonl"
    fake = root / "codex"
    transcript = root / "rollout.jsonl"
    task_append = ""
    if queue_append_task:
        task_record = {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": queue_append_task},
        }
        task_append = (
            f"with open({str(transcript)!r}, 'a', encoding='utf-8') as handle:\n"
            f"    handle.write(json.dumps({task_record!r}, ensure_ascii=False) + chr(10))\n"
        )
    exec_append = ""
    if exec_append_task:
        task_record = {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": exec_append_task},
        }
        exec_append = (
            f"with open({str(transcript)!r}, 'a', encoding='utf-8') as handle:\n"
            f"    handle.write(json.dumps({task_record!r}, ensure_ascii=False) + chr(10))\n"
        )
    pause = f"time.sleep({queue_sleep!r})\n" if queue_sleep > 0 else ""
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        f"open({str(calls)!r}, 'a', encoding='utf-8').write("
        "json.dumps(sys.argv[1:], ensure_ascii=False) + chr(10))\n"
        "argv = sys.argv[1:]\n"
        f"if argv[:1] == ['queue']:\n"
        f"    sys.stderr.write({queue_message!r})\n"
        f"{textwrap.indent(task_append + pause, '    ')}"
        f"    raise SystemExit({queue_exit})\n"
        f"if argv[:2] == ['exec', 'resume']:\n"
        f"    sys.stderr.write({exec_message!r})\n"
        f"{textwrap.indent(exec_append, '    ')}"
        f"    raise SystemExit({exec_exit})\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return calls


def append_task_started(path: Path, turn_id: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": turn_id},
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def write_transcript(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


CUSTOM_DROP_MODEL = "custom:Drop Model-1"


def assistant(
    record_id: str,
    content: list[dict[str, object]] | str | None,
    *,
    model_id: str | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if model_id is not None:
        message["modelId"] = model_id
    return {"type": "message", "id": record_id, "message": message}


def provider_error(
    text: str,
    record_id: str = "err-1",
    *,
    visibility: str = "user_only",
) -> dict[str, object]:
    return {
        "type": "message",
        "id": record_id,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
            "visibility": visibility,
        },
    }


def stop_hook_record(record_id: str = "hook-1") -> dict[str, object]:
    """钩子返回之后 Droid 才把这条执行记录写进 transcript。"""
    return {
        "type": "message",
        "id": record_id,
        "message": {
            "role": "user",
            "content": [],
            "visibility": "user_only",
            "hookEventName": "Stop",
            "hookStatus": "completed",
        },
    }


def user(text: str, record_id: str = "user-1") -> dict[str, object]:
    return {
        "type": "message",
        "id": record_id,
        "message": {
            "role": "user",
            "userMessageSource": "cli_tui",
            "content": [{"type": "text", "text": text}],
        },
    }


def tool_result(
    text: str,
    record_id: str = "result-1",
    tool_use_id: str = "call-1",
) -> dict[str, object]:
    return {
        "type": "message",
        "id": record_id,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": False,
                    "content": text,
                }
            ],
        },
    }


def outcome(turn_id: str = "turn-1") -> dict[str, object]:
    return {
        "type": "agent_turn_outcome",
        "turnId": turn_id,
        "reason": "completed",
        "resultKind": "text",
    }


def todos(*items: tuple[str, str], record_id: str = "todo-1") -> dict[str, object]:
    return {
        "type": "todo_state",
        "id": record_id,
        "todos": {
            "todos": [
                {"id": str(index + 1), "status": status, "content": content}
                for index, (content, status) in enumerate(items)
            ]
        },
    }


def todo_write(*items: tuple[str, str], record_id: str = "todo-call-1") -> dict[str, object]:
    """模型按 TodoWrite 契约传结构化的待办列表。"""
    return assistant(
        record_id,
        [
            {
                "type": "tool_use",
                "id": f"{record_id}-use",
                "name": "TodoWrite",
                "input": {
                    "todos": [
                        {"id": str(index + 1), "status": status, "content": content}
                        for index, (content, status) in enumerate(items)
                    ]
                },
            }
        ],
    )


def todo_write_text(text: str, record_id: str = "todo-call-2") -> dict[str, object]:
    """模型把整段文本塞进 todos，状态由本机猜测。"""
    return assistant(
        record_id,
        [
            {
                "type": "tool_use",
                "id": f"{record_id}-use",
                "name": "TodoWrite",
                "input": {"todos": text},
            }
        ],
    )


def payload(transcript: Path, **updates: object) -> dict[str, object]:
    body: dict[str, object] = {
        "session_id": "session-1",
        "transcript_path": str(transcript),
        "cwd": str(ROOT),
        "permission_mode": "auto-medium",
        "hook_event_name": "Stop",
        "message_id": "parent-1",
        "stop_hook_active": False,
        "tool_execution_count": 4,
        "elapsed_time": 12000,
    }
    body.update(updates)
    return body


def run_hook(
    body: dict[str, object],
    state_dir: Path,
    *,
    raw_input: str | None = None,
    env_updates: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ENV_VARS:
        env.pop(name, None)
    env["DROID_AUTO_CONTINUE_STATE_DIR"] = str(state_dir)
    env["DROID_AUTO_CONTINUE_BASE_DELAY"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if env_updates:
        env.update(env_updates)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=raw_input if raw_input is not None else json.dumps(body, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=20,
        env=env,
    )


class SilentStopTests(unittest.TestCase):
    def test_continues_when_the_last_assistant_message_is_thinking_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [
                    user("把 4.20 的表填上"),
                    assistant(
                        "tool-1",
                        [
                            {"type": "thinking", "thinking": "先核对明细"},
                            {"type": "tool_use", "name": "Execute", "input": {"command": "pwd"}},
                        ],
                    ),
                    assistant(
                        "drop-1",
                        [{"type": "thinking", "thinking": "流在这里断了"}],
                        model_id=CUSTOM_DROP_MODEL,
                    ),
                    outcome(),
                ],
            )
            result = run_hook(payload(transcript), root / "state")

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("自动续跑 1/4", output["reason"])
        self.assertIn("不要重复已经完成", output["reason"])
        self.assertEqual(output["systemMessage"], "Drop Model 发生中断，正在自动重试 1/4...")

    def test_ignores_a_silent_stop_from_a_factory_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            factory = root / "factory.jsonl"
            missing = root / "missing.jsonl"
            write_transcript(
                factory,
                [
                    assistant(
                        "drop-factory",
                        [{"type": "thinking", "thinking": "流在这里断了"}],
                        model_id="claude-opus-5-5",
                    )
                ],
            )
            write_transcript(
                missing,
                [assistant("drop-missing", [{"type": "thinking", "thinking": "流在这里断了"}])],
            )
            factory_result = run_hook(payload(factory), root / "state-factory")
            missing_result = run_hook(payload(missing), root / "state-missing")

        self.assertEqual(factory_result.stdout, "")
        self.assertEqual(missing_result.stdout, "")

    def test_factory_model_still_continues_for_open_todos(self) -> None:
        stdout = UnfinishedStopTests().run_single(
            [
                todo_write(("回写 Excel", "in_progress")),
                todos(("回写 Excel", "in_progress")),
                assistant(
                    "text-factory",
                    [{"type": "text", "text": "已处理完主要部分。"}],
                    model_id="claude-opus-5-5",
                ),
            ]
        )

        output = json.loads(stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("任务尚未完成，正在自动续跑 1/4", output["systemMessage"])

    def test_ignores_a_thinking_only_reply_to_the_empty_end_reminder(self) -> None:
        reminder = (
            "<system-reminder>IMPORTANT: You may be ending the turn, but the TodoWrite plan "
            "still has open items. Once no actionable work remains, end the turn by "
            "replying with no text and no tool calls.\n"
            "Do not reference this reminder to the user.</system-reminder>"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [
                    todo_write(("等审查结论", "in_progress")),
                    todos(("等审查结论", "in_progress")),
                    {
                        "type": "message",
                        "id": "__internal__system-reminder__test",
                        "message": {
                            "role": "user",
                            "visibility": "llm_only",
                            "content": [{"type": "text", "text": reminder}],
                        },
                    },
                    assistant(
                        "yield-1",
                        [
                            {
                                "type": "thinking",
                                "thinking": "",
                                "signature": "signed-thinking",
                                "durationMs": 0,
                            }
                        ],
                        model_id=CUSTOM_DROP_MODEL,
                    ),
                ],
            )
            result = run_hook(payload(transcript), root / "state")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_user_quoting_the_empty_end_instruction_still_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [
                    user("请按这句话做：end the turn by replying with no text and no tool calls"),
                    assistant(
                        "drop",
                        [{"type": "thinking", "thinking": "流在这里断了"}],
                        model_id=CUSTOM_DROP_MODEL,
                    ),
                ],
            )
            result = run_hook(payload(transcript), root / "state")

        self.assertIn("发生中断，正在自动重试 1/4", result.stdout)

    def test_ignores_a_stop_while_a_background_task_is_still_running(self) -> None:
        running = (
            "Task ID: 41fc7afc-6992-464a-9cfd-73cc890b980a\n"
            "Status: running\n"
            "Duration: 1217.8s\n"
            "\n"
            "Latest progress: OK [Process exited with code 0]\n"
            "Task is still running in session \"41fc7afc-6992-464a-9cfd-73cc890b980a\"."
        )
        launched = (
            "Task launched in background.\n"
            "The task is running in a subagent session.\n"
            "You will be notified automatically when it completes and its report will be "
            "delivered to you: do not wait for it or poll for it with TaskOutput."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            running_path = root / "running.jsonl"
            launched_path = root / "launched.jsonl"
            write_transcript(
                running_path,
                [
                    tool_result(running),
                    assistant(
                        "yield-running",
                        [{"type": "thinking", "thinking": ""}],
                        model_id=CUSTOM_DROP_MODEL,
                    ),
                ],
            )
            write_transcript(
                launched_path,
                [
                    tool_result(launched, record_id="launch-result"),
                    assistant(
                        "yield-launched",
                        [{"type": "thinking", "thinking": "先停，等通知"}],
                        model_id=CUSTOM_DROP_MODEL,
                    ),
                ],
            )
            running_result = run_hook(payload(running_path), root / "state-running")
            launched_result = run_hook(payload(launched_path), root / "state-launched")

        self.assertEqual(running_result.stdout, "")
        self.assertEqual(launched_result.stdout, "")

    def test_continues_after_a_finished_background_task_or_a_normal_tool_result(self) -> None:
        finished = (
            "Task ID: 41fc7afc-6992-464a-9cfd-73cc890b980a\n"
            "Status: completed\n"
            "审查结论：方案可以启动。"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            finished_path = root / "finished.jsonl"
            query_path = root / "query.jsonl"
            write_transcript(
                finished_path,
                [
                    tool_result(finished),
                    assistant(
                        "drop-finished",
                        [{"type": "thinking", "thinking": ""}],
                        model_id=CUSTOM_DROP_MODEL,
                    ),
                ],
            )
            long_path = root / "long.jsonl"
            write_transcript(
                query_path,
                [
                    tool_result("2021 871 0.00\n2022 1656 0.00\n[Process exited with code 0]"),
                    assistant(
                        "drop-query",
                        [{"type": "thinking", "thinking": "我一次跑：按月统计。"}],
                        model_id=CUSTOM_DROP_MODEL,
                    ),
                ],
            )
            write_transcript(
                long_path,
                [
                    tool_result(
                        "Status: running\nTask is still running\n" + ("x" * 4000),
                        record_id="long-result",
                    ),
                    assistant(
                        "drop-long",
                        [{"type": "thinking", "thinking": "日志还没看完"}],
                        model_id=CUSTOM_DROP_MODEL,
                    ),
                ],
            )
            finished_result = run_hook(payload(finished_path), root / "state-finished")
            query_result = run_hook(payload(query_path), root / "state-query")
            long_result = run_hook(payload(long_path), root / "state-long")

        self.assertIn("发生中断，正在自动重试 1/4", finished_result.stdout)
        self.assertIn("发生中断，正在自动重试 1/4", query_result.stdout)
        self.assertIn("发生中断，正在自动重试 1/4", long_result.stdout)

    def test_ignores_a_visible_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [
                    assistant(
                        "done-1",
                        [
                            {"type": "thinking", "thinking": "总结"},
                            {"type": "text", "text": "表已经填好。"},
                        ],
                    )
                ],
            )
            result = run_hook(payload(transcript), root / "state")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "本轮任务正常完成。")

    def test_ignores_a_tool_call_without_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [
                    assistant(
                        "tool-only",
                        [{"type": "thinking", "thinking": "还要查一列"}, {"type": "tool_use", "name": "Execute"}],
                    )
                ],
            )
            result = run_hook(payload(transcript), root / "state")

        self.assertEqual(result.stdout, "")

    def test_ignores_unknown_content_and_a_trailing_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unknown = root / "unknown.jsonl"
            trailing_user = root / "user.jsonl"
            write_transcript(
                unknown,
                [assistant("odd", [{"type": "custom_blob", "text": ""}])],
            )
            write_transcript(
                trailing_user,
                [
                    assistant("drop", [{"type": "thinking", "thinking": "断了"}]),
                    user("继续", "user-2"),
                ],
            )
            unknown_result = run_hook(payload(unknown), root / "state-a")
            user_result = run_hook(payload(trailing_user), root / "state-b")

        self.assertEqual(unknown_result.stdout, "")
        self.assertEqual(user_result.stdout, "")

    def test_blank_text_and_reasoning_still_continue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [
                    assistant(
                        "blank",
                        [
                            {"type": "reasoning", "text": "hidden"},
                            {"type": "text", "text": "  \n"},
                        ],
                        model_id=CUSTOM_DROP_MODEL,
                    )
                ],
            )
            result = run_hook(payload(transcript), root / "state")

        self.assertIn("自动续跑 1/4", result.stdout)
        self.assertEqual(
            json.loads(result.stdout)["systemMessage"],
            "Drop Model 发生中断，正在自动重试 1/4...",
        )

    def test_system_message_uses_the_custom_model_display_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            record = assistant("drop-named", [{"type": "thinking", "thinking": "流在这里断了"}])
            record["message"]["modelId"] = "custom:Slug Name-4"
            write_transcript(transcript, [record])
            settings = root / ".factory"
            settings.mkdir()
            (settings / "settings.json").write_text(
                json.dumps(
                    {
                        "customModels": [
                            {
                                "id": "custom:Slug Name-4",
                                "displayName": "From settings.json",
                                "apiKey": "sk-secret-should-not-leak",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (settings / "settings.local.json").write_text(
                json.dumps(
                    {
                        "general": {
                            "customModels": [
                                {
                                    "id": "custom:Slug Name-4",
                                    "displayName": "Pretty Name",
                                    "model": "secret-provider-model",
                                    "apiKey": "sk-local-secret",
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            result = run_hook(payload(transcript, cwd=str(root)), root / "state")

        output = json.loads(result.stdout)
        self.assertEqual(output["systemMessage"], "Pretty Name 发生中断，正在自动重试 1/4...")
        self.assertNotIn("sk-secret-should-not-leak", result.stdout)
        self.assertNotIn("sk-local-secret", result.stdout)
        self.assertNotIn("secret-provider-model", result.stdout)

    def test_system_message_parses_a_custom_model_id_without_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            record = assistant("drop-parsed", [{"type": "thinking", "thinking": "断了"}])
            record["message"]["modelId"] = "custom:Parsed Only-9"
            write_transcript(transcript, [record])
            result = run_hook(payload(transcript, cwd=str(root)), root / "state")

        self.assertEqual(
            json.loads(result.stdout)["systemMessage"],
            "Parsed Only 发生中断，正在自动重试 1/4...",
        )

    def test_streak_uses_stop_hook_active_and_resets_for_a_new_user_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [assistant("drop-1", [{"type": "thinking", "thinking": "一次"}], model_id=CUSTOM_DROP_MODEL)],
            )
            first = run_hook(payload(transcript), state)
            write_transcript(
                transcript,
                [assistant("drop-2", [{"type": "thinking", "thinking": "两次"}], model_id=CUSTOM_DROP_MODEL)],
            )
            second = run_hook(payload(transcript, stop_hook_active=True), state)
            write_transcript(
                transcript,
                [assistant("drop-3", [{"type": "thinking", "thinking": "新一轮"}], model_id=CUSTOM_DROP_MODEL)],
            )
            reset = run_hook(payload(transcript, stop_hook_active=False), state)

        self.assertIn("自动续跑 1/4", first.stdout)
        self.assertIn("自动续跑 2/4", second.stdout)
        self.assertIn("自动续跑 1/4", reset.stdout)
        self.assertIn("正在自动重试 1/4...", first.stdout)
        self.assertIn("正在自动重试 2/4...", second.stdout)
        self.assertIn("正在自动重试 1/4...", reset.stdout)

    def test_duplicate_assistant_message_and_attempt_cap_do_not_continue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [assistant("same-drop", [{"type": "thinking", "thinking": "一次"}], model_id=CUSTOM_DROP_MODEL)],
            )
            env = {"DROID_AUTO_CONTINUE_MAX_ATTEMPTS": "1"}
            first = run_hook(payload(transcript), state, env_updates=env)
            duplicate = run_hook(payload(transcript), state, env_updates=env)
            write_transcript(
                transcript,
                [assistant("next-drop", [{"type": "thinking", "thinking": "两次"}], model_id=CUSTOM_DROP_MODEL)],
            )
            capped = run_hook(
                payload(transcript, stop_hook_active=True),
                state,
                env_updates=env,
            )
            self.assertIn("自动续跑 1/1", first.stdout)
            self.assertIn("正在自动重试 1/1...", first.stdout)
            self.assertEqual(duplicate.stdout, "")
            self.assertEqual(capped.stdout, "")
            database = state / "streaks.sqlite3"
            with sqlite3.connect(database) as connection:
                stored = connection.execute("SELECT attempts FROM streaks").fetchone()
            self.assertEqual(stored[0], 2)

    def test_streak_resets_after_real_tool_work_between_stops(self) -> None:
        """实测：Droid 整条续跑链里 `stop_hook_active` 一直是 true。

        会话里模型跑了 104 次工具之后又中断一次，仍被算成连续第 3 次，
        第 4 次用完后真正的压缩摘要中断就不再续跑。中间干过活就不算连续。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [assistant("drop-1", [{"type": "thinking", "thinking": "一次"}], model_id=CUSTOM_DROP_MODEL)],
            )
            first = run_hook(payload(transcript, stop_hook_active=True), state)
            write_transcript(
                transcript,
                [
                    assistant("drop-1", [{"type": "thinking", "thinking": "一次"}], model_id=CUSTOM_DROP_MODEL),
                    user("继续。自动续跑 1/4。", "user-2"),
                    assistant(
                        "work-1",
                        [
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "Execute",
                                "input": {"command": "pwd"},
                            }
                        ],
                    ),
                    assistant("drop-2", [{"type": "thinking", "thinking": "两次"}], model_id=CUSTOM_DROP_MODEL),
                ],
            )
            second = run_hook(payload(transcript, stop_hook_active=True), state)

        self.assertIn("自动续跑 1/4", first.stdout)
        self.assertIn("自动续跑 1/4", second.stdout)

    def test_streak_keeps_counting_without_new_tool_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [
                    assistant(
                        "work-1",
                        [
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "Execute",
                                "input": {"command": "pwd"},
                            }
                        ],
                    ),
                    assistant("drop-1", [{"type": "thinking", "thinking": "一次"}], model_id=CUSTOM_DROP_MODEL),
                ],
            )
            first = run_hook(payload(transcript, stop_hook_active=True), state)
            write_transcript(
                transcript,
                [
                    assistant(
                        "work-1",
                        [
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "Execute",
                                "input": {"command": "pwd"},
                            }
                        ],
                    ),
                    assistant("drop-1", [{"type": "thinking", "thinking": "一次"}], model_id=CUSTOM_DROP_MODEL),
                    user("继续。自动续跑 1/4。", "user-2"),
                    assistant("drop-2", [{"type": "thinking", "thinking": "两次"}], model_id=CUSTOM_DROP_MODEL),
                ],
            )
            second = run_hook(payload(transcript, stop_hook_active=True), state)

        self.assertIn("自动续跑 1/4", first.stdout)
        self.assertIn("自动续跑 2/4", second.stdout)

    def test_streak_reset_survives_a_state_database_without_progress_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            with sqlite3.connect(state / "streaks.sqlite3") as connection:
                connection.execute(
                    """
                    CREATE TABLE streaks (
                        scope_hash TEXT PRIMARY KEY,
                        attempts INTEGER NOT NULL,
                        assistant_hash TEXT,
                        last_seen INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO streaks VALUES (?, ?, ?, ?)",
                    (
                        hashlib.sha256("session-1".encode("utf-8")).hexdigest(),
                        5,
                        "old",
                        1,
                    ),
                )
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [
                    assistant(
                        "work-1",
                        [
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "Execute",
                                "input": {"command": "pwd"},
                            }
                        ],
                    ),
                    assistant("drop-1", [{"type": "thinking", "thinking": "一次"}], model_id=CUSTOM_DROP_MODEL),
                ],
            )
            result = run_hook(payload(transcript, stop_hook_active=True), state)

        self.assertIn("自动续跑 1/4", result.stdout)

    def test_fail_open_on_bad_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "missing.jsonl"
            cases = [
                payload(transcript),
                payload(transcript, hook_event_name="SubagentStop"),
                payload(transcript, stop_hook_active="false"),
                payload(transcript, session_id=""),
            ]
            results = [run_hook(case, root / f"state-{index}") for index, case in enumerate(cases)]
            raw = run_hook(payload(transcript), root / "state-raw", raw_input="{")

        for result in [*results, raw]:
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")


class UnfinishedStopTests(unittest.TestCase):
    def run_single(self, records: list[dict[str, object]], **extra: object) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            write_transcript(transcript, records)
            result = run_hook(payload(transcript, **extra), root / "state")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_continues_when_the_turn_only_wrote_a_compaction_summary(self) -> None:
        summary = (
            "<analysis>\nLet me chronologically analyze this conversation.\n</analysis>\n"
            "<summary>\n1. Read the workbook.\n2. Still writing rows.\n</summary>"
        )
        stdout = self.run_single(
            [
                user("把 Excel 改完"),
                todo_write(("回写 Excel", "in_progress")),
                todos(("回写 Excel", "in_progress")),
                assistant("tool-1", [{"type": "tool_use", "name": "Execute"}]),
                assistant("summary-1", [{"type": "text", "text": summary}]),
            ]
        )

        output = json.loads(stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("上下文压缩摘要", output["reason"])
        self.assertIn("自动续跑 1/4", output["reason"])
        self.assertEqual(output["systemMessage"], "Custom Model 任务尚未完成，正在自动续跑 1/4...")

    def test_continues_when_todos_are_still_open(self) -> None:
        stdout = self.run_single(
            [
                user("把 Excel 改完"),
                todo_write(
                    ("回写 Excel", "completed"),
                    ("校验修改结果并汇报", "in_progress"),
                    ("补充缺失章节", "pending"),
                ),
                todos(
                    ("回写 Excel", "completed"),
                    ("校验修改结果并汇报", "in_progress"),
                    ("补充缺失章节", "pending"),
                ),
                assistant("text-1", [{"type": "text", "text": "已处理完主要部分。"}]),
            ]
        )

        output = json.loads(stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("未完成项：校验修改结果并汇报；补充缺失章节。", output["reason"])
        self.assertIn("先对账", output["reason"])
        self.assertIn("标成 completed", output["reason"])

    def test_ignores_open_todos_while_a_background_task_is_still_running(self) -> None:
        """实测误报：进度说明写完了，待办留给后台代理，中间还夹了一次 TodoWrite。"""
        task_id = "3ec75d3f-bca9-463f-b2ca-30d10e05f4fb"
        launched = (
            "Task launched in background.\n"
            f"task_id: {task_id}\n"
            "The task is running in a subagent session.\n"
            "You will be notified automatically when it completes and its report will be "
            "delivered to you: do not wait for it or poll for it with TaskOutput."
        )
        stdout = self.run_single(
            [
                user("继续 Google 2026 第二阶段"),
                tool_result(launched, record_id="launch-result"),
                todo_write(("冻结抽取并干跑", "in_progress"), ("审查后发布", "pending")),
                todos(("冻结抽取并干跑", "in_progress"), ("审查后发布", "pending")),
                tool_result("TODO List Updated", record_id="todo-result", tool_use_id="todo-call-1-use"),
                assistant(
                    "status-1",
                    [{"type": "text", "text": "第二阶段第 1 步已交给排查代理，发布脚本这段时间我不碰。"}],
                ),
            ]
        )

        self.assertEqual(stdout.strip(), "本轮任务正常完成。")

    def test_keeps_waiting_when_another_background_task_is_still_running(self) -> None:
        running = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        done = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        stdout = self.run_single(
            [
                user("两路一起查"),
                tool_result(
                    f"task_id: {running}\ndo not wait for it or poll for it with TaskOutput.",
                    record_id="launch-running",
                ),
                tool_result(
                    f"task_id: {done}\ndo not wait for it or poll for it with TaskOutput.",
                    record_id="launch-done",
                ),
                tool_result(
                    f"Task ID: {done}\nStatus: completed\n审查结论：可以收工。",
                    record_id="done-result",
                ),
                todo_write(("等还在跑的那一路", "in_progress")),
                todos(("等还在跑的那一路", "in_progress")),
                assistant("status-2", [{"type": "text", "text": "一路已经回来了，另一路还在跑。"}]),
            ]
        )

        self.assertEqual(stdout.strip(), "本轮任务正常完成。")

    def test_continues_open_todos_after_the_background_task_completes(self) -> None:
        task_id = "3ec75d3f-bca9-463f-b2ca-30d10e05f4fb"
        stdout = self.run_single(
            [
                user("继续 Google 2026 第二阶段"),
                tool_result(
                    f"task_id: {task_id}\ndo not wait for it or poll for it with TaskOutput.",
                    record_id="launch-result",
                ),
                user(
                    "Background task completed.\n"
                    f"task_id: {task_id}\n"
                    "reason: completed\n"
                    "output: 干跑还差负样本。",
                    record_id="task-completion",
                ),
                todo_write(("补负样本", "in_progress")),
                todos(("补负样本", "in_progress")),
                assistant("status-3", [{"type": "text", "text": "代理交回来了，我先看到这里。"}]),
            ]
        )

        output = json.loads(stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("未完成项：补负样本。", output["reason"])

    def test_user_quoting_the_background_phrase_does_not_hide_open_todos(self) -> None:
        stdout = self.run_single(
            [
                user("日志里有一句 do not wait for it or poll for it with TaskOutput，请处理待办。"),
                todo_write(("回写 Excel", "in_progress")),
                todos(("回写 Excel", "in_progress")),
                assistant("status-4", [{"type": "text", "text": "已处理完主要部分。"}]),
            ]
        )

        output = json.loads(stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("未完成项：回写 Excel。", output["reason"])

    def test_still_continues_a_summary_while_a_background_task_is_running(self) -> None:
        summary = (
            "<analysis>\nLet me chronologically analyze this conversation.\n</analysis>\n"
            "<summary>\n1. Launched the subagent.\n2. Still writing the plan.\n</summary>"
        )
        stdout = self.run_single(
            [
                user("继续"),
                tool_result(
                    "task_id: 3ec75d3f-bca9-463f-b2ca-30d10e05f4fb\n"
                    "do not wait for it or poll for it with TaskOutput.",
                    record_id="launch-result",
                ),
                todo_write(("回写 Excel", "in_progress")),
                todos(("回写 Excel", "in_progress")),
                assistant("summary-2", [{"type": "text", "text": summary}]),
            ]
        )

        output = json.loads(stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("上下文压缩摘要", output["reason"])

    def test_long_running_tool_result_does_not_hide_open_todos(self) -> None:
        stdout = self.run_single(
            [
                user("看日志"),
                tool_result(
                    "Status: running\nTask is still running\n" + ("x" * 4000),
                    record_id="long-result",
                ),
                todo_write(("回写 Excel", "in_progress")),
                todos(("回写 Excel", "in_progress")),
                assistant("status-5", [{"type": "text", "text": "已处理完主要部分。"}]),
            ]
        )

        output = json.loads(stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("未完成项：回写 Excel。", output["reason"])

    def test_ignores_an_open_todo_when_work_happened_after_the_plan_update(self) -> None:
        """实测误报：计划里留了 in_progress，之后又跑了 55 次工具并提交，计划已过时。"""
        stdout = self.run_single(
            [
                todo_write(("修复图表", "completed"), ("Run script and verify output", "in_progress")),
                todos(("修复图表", "completed"), ("Run script and verify output", "in_progress")),
                assistant("work-1", [{"type": "tool_use", "name": "Execute"}]),
                user("On branch main", record_id="result-1"),
                assistant("work-2", [{"type": "tool_use", "name": "Execute"}]),
                assistant("done-3", [{"type": "text", "text": "已提交到 git，commit `b87b4cd`。"}]),
            ]
        )

        self.assertEqual(stdout.strip(), "本轮任务正常完成。")

    def test_ignores_a_plan_updated_in_the_same_message_as_another_tool_call(self) -> None:
        stdout = self.run_single(
            [
                assistant(
                    "mixed-1",
                    [
                        {
                            "type": "tool_use",
                            "name": "TodoWrite",
                            "input": {
                                "todos": [
                                    {"id": "1", "status": "in_progress", "content": "校验结果"}
                                ]
                            },
                        },
                        {"type": "tool_use", "name": "Execute"},
                    ],
                ),
                todos(("校验结果", "in_progress")),
                assistant("done-4", [{"type": "text", "text": "跑完了，结果如上。"}]),
            ]
        )

        self.assertEqual(stdout.strip(), "本轮任务正常完成。")

    def test_stops_when_the_model_asked_the_user_a_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [
                    todo_write(("回写 Excel", "in_progress")),
                    todos(("回写 Excel", "in_progress")),
                    assistant("ask-1", [{"type": "text", "text": "要覆盖原表还是另存一份？"}]),
                ],
            )
            result = run_hook(payload(transcript), root / "state")

        self.assertEqual(result.stdout.strip(), "本轮任务正常完成。")

    def test_stops_when_all_todos_are_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [
                    todo_write(("回写 Excel", "completed"), ("校验修改结果并汇报", "completed")),
                    todos(("回写 Excel", "completed"), ("校验修改结果并汇报", "completed")),
                    assistant("done-1", [{"type": "text", "text": "全部完成，共更新 13 行。"}]),
                ],
            )
            result = run_hook(payload(transcript), root / "state")

        self.assertEqual(result.stdout.strip(), "本轮任务正常完成。")

    def test_ignores_inline_code_fences_and_tag_names_in_prose(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [
                    assistant(
                        "prose-1",
                        [
                            {
                                "type": "text",
                                "text": (
                                    "触发条件：\n"
                                    "- 正文以 `<analysis>` / `<summary>` 包裹，属于摘要。\n"
                                    "- 正文截断：```` ``` ```` 个数为奇数，或标签有开无闭。\n"
                                    "- 已发出工具调用则不续跑。"
                                ),
                            }
                        ],
                    )
                ],
            )
            result = run_hook(payload(transcript), root / "state")

        self.assertEqual(result.stdout.strip(), "本轮任务正常完成。")

    def test_ignores_a_closed_code_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [
                    assistant(
                        "prose-2",
                        [
                            {
                                "type": "text",
                                "text": "改法如下：\n```python\nprint('<summary>')\n```\n已提交。",
                            }
                        ],
                    )
                ],
            )
            result = run_hook(payload(transcript), root / "state")

        self.assertEqual(result.stdout.strip(), "本轮任务正常完成。")

    def test_continues_when_the_text_was_cut_mid_code_block(self) -> None:
        stdout = self.run_single(
            [assistant("cut-1", [{"type": "text", "text": "结果如下：\n```python\nprint(1)"}])]
        )

        output = json.loads(stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("被截断", output["reason"])

    def test_continues_when_a_summary_tag_is_left_open(self) -> None:
        stdout = self.run_single(
            [assistant("cut-2", [{"type": "text", "text": "<analysis>\n还在分析"}])]
        )

        self.assertEqual(json.loads(stdout)["decision"], "block")

    def test_continues_when_the_text_ends_with_leaked_tool_markup(self) -> None:
        """实测漏判：正文写到一半，工具调用协议片段当成文字发了出来。"""
        leaked = (
            "先补一条等价性测试。\n\n"
            "Let me write the edits.\n"
            "</｜DSML｜ parameter>\n</｜DSML｜ invoke>\n</｜DSML｜ calls>"
        )
        stdout = self.run_single([assistant("leak-1", [{"type": "text", "text": leaked}])])

        output = json.loads(stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("工具调用协议", output["reason"])
        self.assertIn("自动续跑 1/4", output["reason"])

    def test_continues_when_a_function_calls_block_is_leaked(self) -> None:
        """历史会话里的另一种形态：<invoke>/<parameter> 也当成正文发了出来。"""
        leaked = (
            ' 图表区", "value": "ultra-charts"}, {"label": "API 接入区", "value": "api-section"}]'
            "</parameter>\n</invoke>\n</function_calls>"
        )
        stdout = self.run_single([assistant("leak-4", [{"type": "text", "text": leaked}])])

        self.assertEqual(json.loads(stdout)["decision"], "block")

    def test_ignores_leaked_markup_quoted_before_the_end_of_the_text(self) -> None:
        stdout = self.run_single(
            [
                assistant(
                    "leak-2",
                    [
                        {
                            "type": "text",
                            "text": "这类片段形如 `</｜DSML｜ invoke>`，以前会被漏判。已修。",
                        }
                    ],
                )
            ]
        )

        self.assertEqual(stdout.strip(), "本轮任务正常完成。")

    def test_ignores_leaked_markup_inside_a_code_block(self) -> None:
        stdout = self.run_single(
            [
                assistant(
                    "leak-3",
                    [
                        {
                            "type": "text",
                            "text": "复现步骤：\n```\n</｜DSML｜ invoke>\n```\n这就是当时的现场。",
                        }
                    ],
                )
            ]
        )

        self.assertEqual(stdout.strip(), "本轮任务正常完成。")

    def test_ignores_a_text_message_that_also_calls_a_tool(self) -> None:
        stdout = self.run_single(
            [
                todo_write(("回写 Excel", "in_progress")),
                todos(("回写 Excel", "in_progress")),
                assistant(
                    "tool-2",
                    [
                        {"type": "text", "text": "先读回工作簿。"},
                        {"type": "tool_use", "name": "Read"},
                    ],
                ),
            ]
        )

        self.assertEqual(stdout.strip(), "")

    def test_ignores_a_guessed_pending_state_when_the_tool_input_was_free_text(self) -> None:
        """实测误解：模型把整段文本塞进 todos，本机猜成 pending，其实全都做完了。"""
        raw = (
            "completed [high] N0 数据清单与身份冻结\n"
            "completed [high] N1 诊断：layout 64 层逐位一致\n"
            "completed [high] N4 正式 G0：failed（MAE 0.0407 > 0.039）"
        )
        stdout = self.run_single(
            [
                todo_write_text(raw),
                todos(
                    ("completed [high] N0 数据清单与身份冻结", "pending"),
                    ("completed [high] N1 诊断：layout 64 层逐位一致", "pending"),
                    ("completed [high] N4 正式 G0：failed（MAE 0.0407 > 0.039）", "pending"),
                ),
                assistant(
                    "done-2",
                    [{"type": "text", "text": "无未完成事项：全量测试 134 passed。"}],
                ),
            ]
        )

        self.assertEqual(stdout.strip(), "本轮任务正常完成。")

    def test_free_text_todo_input_is_never_read_as_unfinished_work(self) -> None:
        """自由文本里的 in_progress/pending 只是文字，不构成未完成的证据。"""
        stdout = self.run_single(
            [
                todo_write_text("in_progress [high] 校验修改结果\npending [high] 补充缺失章节"),
                assistant("text-3", [{"type": "text", "text": "先到这里。"}]),
            ]
        )

        self.assertEqual(stdout.strip(), "本轮任务正常完成。")

    def test_uses_parsed_todo_state_when_the_model_sent_free_text(self) -> None:
        """实测漏判：整份清单是字符串，但 Droid 已解析出每一行的状态。"""
        stdout = self.run_single(
            [
                user("继续跑完校准"),
                todo_write_text(
                    "1. [completed] 通读方案与已有产物\n"
                    "2. [completed] 修复 streaming 算术路径\n"
                    "3. [in_progress] calib4 的 S1/R_full 运行中\n"
                    "4. [pending] 填报告并给出正式判定"
                ),
                todos(
                    ("通读方案与已有产物", "completed"),
                    ("修复 streaming 算术路径", "completed"),
                    ("calib4 的 S1/R_full 运行中", "in_progress"),
                    ("填报告并给出正式判定", "pending"),
                ),
                assistant("text-open", [{"type": "text", "text": "后续动作已经挂上了自动链。"}]),
            ]
        )

        output = json.loads(stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn(
            "未完成项：calib4 的 S1/R_full 运行中；填报告并给出正式判定。",
            output["reason"],
        )

    def test_abstains_when_droid_kept_a_free_text_line_verbatim(self) -> None:
        """Droid 没认出这一行的状态时状态是默认的 pending，整份弃权。"""
        stdout = self.run_single(
            [
                todo_write_text("1. [completed] 回写 Excel\n2. 校验修改结果并汇报"),
                todos(
                    ("回写 Excel", "completed"),
                    ("校验修改结果并汇报", "pending"),
                ),
                assistant("text-verbatim", [{"type": "text", "text": "先到这里。"}]),
            ]
        )

        self.assertEqual(stdout.strip(), "本轮任务正常完成。")

    def test_abstains_when_free_text_todo_state_is_missing(self) -> None:
        stdout = self.run_single(
            [
                todo_write_text("1. [in_progress] 校验修改结果"),
                assistant("text-nostate", [{"type": "text", "text": "先到这里。"}]),
            ]
        )

        self.assertEqual(stdout.strip(), "本轮任务正常完成。")

    def test_abstains_when_free_text_line_count_does_not_match(self) -> None:
        stdout = self.run_single(
            [
                todo_write_text("1. [in_progress] 校验修改结果\n2. [pending] 补充缺失章节"),
                todos(("校验修改结果", "in_progress")),
                assistant("text-count", [{"type": "text", "text": "先到这里。"}]),
            ]
        )

        self.assertEqual(stdout.strip(), "本轮任务正常完成。")

    def test_ignores_a_todo_state_without_a_todo_write_call(self) -> None:
        stdout = self.run_single(
            [
                todos(("回写 Excel", "in_progress"), ("补充缺失章节", "pending")),
                assistant("text-4", [{"type": "text", "text": "已完成。"}]),
            ]
        )

        self.assertEqual(stdout.strip(), "本轮任务正常完成。")

    def test_uses_the_latest_todo_write_call(self) -> None:
        stdout = self.run_single(
            [
                todo_write(("回写 Excel", "in_progress")),
                todo_write(("回写 Excel", "completed"), record_id="todo-call-3"),
                todos(("回写 Excel", "in_progress")),
                assistant("text-5", [{"type": "text", "text": "已完成。"}]),
            ]
        )

        self.assertEqual(stdout.strip(), "本轮任务正常完成。")

    def test_detect_unfinished_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [
                    todos(("回写 Excel", "in_progress")),
                    assistant("text-2", [{"type": "text", "text": "主要部分已处理。"}]),
                ],
            )
            result = run_hook(
                payload(transcript),
                root / "state",
                env_updates={"DROID_AUTO_CONTINUE_DETECT_UNFINISHED": "0"},
            )

        self.assertEqual(result.stdout.strip(), "本轮任务正常完成。")


def codex_item(payload: dict[str, object]) -> dict[str, object]:
    return {"type": "response_item", "payload": payload}


def codex_user(text: str, item_id: str = "user-1") -> dict[str, object]:
    return codex_item(
        {
            "type": "message",
            "id": item_id,
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }
    )


def codex_assistant(item_id: str, text: str | None) -> dict[str, object]:
    content: list[dict[str, object]] | None
    if text is None:
        content = []
    else:
        content = [{"type": "output_text", "text": text}]
    return codex_item(
        {"type": "message", "id": item_id, "role": "assistant", "content": content}
    )


def codex_reasoning(item_id: str = "reason-1") -> dict[str, object]:
    return codex_item(
        {
            "type": "reasoning",
            "id": item_id,
            "summary": [],
            "content": [{"type": "reasoning_text", "text": "hidden"}],
        }
    )


def codex_call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return codex_item(
        {
            "type": "function_call",
            "id": f"fc_{call_id}",
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
            "call_id": call_id,
        }
    )


def codex_output(call_id: str) -> dict[str, object]:
    return codex_item(
        {"type": "function_call_output", "call_id": call_id, "output": "ok"}
    )


def codex_plan(*items: tuple[str, str], call_id: str = "plan-1") -> dict[str, object]:
    return codex_call(
        call_id,
        "update_plan",
        {
            "plan": [
                {"step": step, "status": status} for step, status in items
            ]
        },
    )


class CodexStopTests(unittest.TestCase):
    def run_records(
        self,
        records: list[dict[str, object]],
        *,
        state: Path | None = None,
        **extra: object,
    ) -> subprocess.CompletedProcess[str]:
        root = state.parent if state is not None else None
        if root is None:
            raise AssertionError("state is required")
        transcript = root / "rollout.jsonl"
        write_transcript(transcript, records)
        body = payload(transcript, model="APUS Private DS v4.1 Flash #2", **extra)
        return run_hook(body, state)

    def test_continues_a_reasoning_only_stop_and_uses_the_payload_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_records(
                [
                    {"type": "session_meta", "payload": {"id": "session-1"}},
                    codex_user("把 4.20 的表填上"),
                    codex_call("call-1", "shell", {"command": "pwd"}),
                    codex_output("call-1"),
                    codex_reasoning("reason-drop"),
                    {"type": "event_msg", "payload": {"type": "task_complete"}},
                ],
                state=root / "state",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("自动续跑 1/4", output["reason"])
        self.assertEqual(
            output["systemMessage"],
            "APUS Private DS v4.1 Flash #2 发生中断，正在自动重试 1/4...",
        )

    def test_visible_answer_prints_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_records(
                [codex_reasoning(), codex_assistant("done-1", "表已经填好。")],
                state=root / "state",
            )

        self.assertEqual(result.stdout, "")

    def test_tool_call_without_text_does_not_continue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_records(
                [codex_reasoning(), codex_call("call-2", "shell", {"command": "pwd"})],
                state=root / "state",
            )

        self.assertEqual(result.stdout, "")

    def test_blank_text_still_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_records(
                [codex_reasoning("reason-blank"), codex_assistant("blank", "  \n")],
                state=root / "state",
            )

        output = json.loads(result.stdout)
        self.assertIn("发生中断，正在自动重试 1/4", output["systemMessage"])

    def test_continues_a_compaction_summary_and_a_compaction_item(self) -> None:
        summary = (
            "<analysis>\nLet me chronologically analyze this conversation.\n</analysis>\n"
            "<summary>\n1. Read the workbook.\n2. Still writing rows.\n</summary>"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = self.run_records(
                [codex_user("把 Excel 改完"), codex_assistant("summary-1", summary)],
                state=root / "state-text",
            )
            item = self.run_records(
                [
                    codex_user("把 Excel 改完"),
                    codex_item(
                        {
                            "type": "compaction",
                            "id": "compact-1",
                            "encrypted_content": "secret",
                        }
                    ),
                ],
                state=root / "state-item",
            )

        text_output = json.loads(text.stdout)
        item_output = json.loads(item.stdout)
        self.assertIn("上下文压缩摘要", text_output["reason"])
        self.assertIn("上下文压缩摘要", item_output["reason"])
        self.assertIn("任务尚未完成，正在自动续跑 1/4", text_output["systemMessage"])
        self.assertNotIn("secret", item.stdout)

    def test_detect_unfinished_ignores_a_compaction_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "rollout.jsonl"
            write_transcript(
                transcript,
                [
                    codex_item(
                        {
                            "type": "compaction",
                            "id": "compact-2",
                            "encrypted_content": "secret",
                        }
                    )
                ],
            )
            result = run_hook(
                payload(transcript, model="custom"),
                root / "state",
                env_updates={"DROID_AUTO_CONTINUE_DETECT_UNFINISHED": "0"},
            )

        self.assertEqual(result.stdout, "")

    def test_continues_when_update_plan_still_has_open_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_records(
                [
                    codex_user("把 Excel 改完"),
                    codex_plan(
                        ("回写 Excel", "completed"),
                        ("校验修改结果并汇报", "in_progress"),
                        ("补充缺失章节", "pending"),
                    ),
                    codex_output("plan-1"),
                    codex_assistant("text-1", "主要部分已处理。"),
                ],
                state=root / "state",
            )

        output = json.loads(result.stdout)
        self.assertIn("未完成项：校验修改结果并汇报；补充缺失章节", output["reason"])
        self.assertIn("用 update_plan 把它们标成 completed", output["reason"])
        self.assertNotIn("TodoWrite", output["reason"])

    def test_ignores_an_update_plan_after_later_tool_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_records(
                [
                    codex_plan(("回写 Excel", "in_progress")),
                    codex_output("plan-1"),
                    codex_call("call-9", "shell", {"command": "pwd"}),
                    codex_output("call-9"),
                    codex_assistant("text-2", "已完成。"),
                ],
                state=root / "state",
            )

        self.assertEqual(result.stdout, "")

    def test_question_and_trailing_user_message_do_not_continue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            question = self.run_records(
                [
                    codex_plan(("回写 Excel", "in_progress")),
                    codex_assistant("ask-1", "要不要我继续？"),
                ],
                state=root / "state-q",
            )
            trailing = self.run_records(
                [codex_reasoning("reason-2"), codex_user("继续", "user-2")],
                state=root / "state-u",
            )

        self.assertEqual(question.stdout, "")
        self.assertEqual(trailing.stdout, "")

    def test_unknown_content_does_not_continue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_records(
                [
                    codex_item(
                        {
                            "type": "message",
                            "id": "image-1",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "看这张图。"},
                                {"type": "input_image", "image_url": "data:image/png;base64,aa"},
                            ],
                        }
                    )
                ],
                state=root / "state",
            )

        self.assertEqual(result.stdout, "")

    def test_truncated_text_and_leaked_markup_continue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            truncated = self.run_records(
                [codex_assistant("cut-1", "```python\nprint('hi')")],
                state=root / "state-cut",
            )
            leaked = self.run_records(
                [codex_assistant("leak-1", "接下来调用\n<｜DSML｜tool_calls>")],
                state=root / "state-leak",
            )

        self.assertIn("截断", truncated.stdout)
        self.assertIn("工具调用协议", leaked.stdout)

    def test_streak_resets_after_a_new_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            first = self.run_records(
                [codex_reasoning("reason-a")],
                state=state,
                stop_hook_active=True,
            )
            second = self.run_records(
                [
                    codex_reasoning("reason-a"),
                    codex_user("继续。自动续跑 1/4。", "user-hook"),
                    codex_call("call-new", "shell", {"command": "pwd"}),
                    codex_output("call-new"),
                    codex_reasoning("reason-b"),
                ],
                state=state,
                stop_hook_active=True,
            )

        self.assertIn("自动续跑 1/4", first.stdout)
        self.assertIn("自动续跑 1/4", second.stdout)

    def test_streak_keeps_counting_without_new_tool_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            first = self.run_records(
                [codex_reasoning("reason-a")],
                state=state,
                stop_hook_active=True,
            )
            second = self.run_records(
                [
                    codex_reasoning("reason-a"),
                    codex_user("继续。自动续跑 1/4。", "user-hook"),
                    codex_reasoning("reason-b"),
                ],
                state=state,
                stop_hook_active=True,
            )

        self.assertIn("自动续跑 1/4", first.stdout)
        self.assertIn("自动续跑 2/4", second.stdout)

    def test_unstructured_plan_arguments_do_not_continue_a_visible_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_records(
                [
                    codex_call("plan-bad", "update_plan", {}),
                    codex_output("plan-bad"),
                    codex_assistant("text-3", "已完成。"),
                ],
                state=root / "state",
            )

        self.assertEqual(result.stdout, "")


class FailureHookTests(unittest.TestCase):
    """Codex 在 API 层失败时只发 PostModelRequest，不发 Stop。"""

    def wait_for_job(
        self,
        state: Path,
        *,
        timeout: float = 8.0,
        states: set[str] | None = None,
        detail_contains: str | None = None,
    ) -> tuple[str, str, int | None, int | None]:
        database = state / "streaks.sqlite3"
        expected = states or {
            "recovered",
            "queued",
            "completed_unverified",
            "blocked_active_writer",
            "failed",
            "failed_to_launch",
            "superseded",
        }
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if database.exists():
                try:
                    connection = sqlite3.connect(str(database))
                    row = connection.execute(
                        "SELECT job_id, state, api_attempts, exit_code, detail "
                        "FROM resume_jobs ORDER BY created_at DESC LIMIT 1"
                    ).fetchone()
                    connection.close()
                except sqlite3.Error:
                    row = None
                if (
                    row is not None
                    and row[1] in expected
                    and (detail_contains is None or detail_contains in (row[4] or ""))
                ):
                    return row[0], row[1], row[2], row[3]
            time.sleep(0.05)
        self.fail("resume job did not reach an expected state")

    def streak_attempts(self, state: Path, session_id: str = "session-failure-1") -> int:
        with sqlite3.connect(str(state / "streaks.sqlite3")) as connection:
            row = connection.execute(
                "SELECT attempts FROM streaks WHERE scope_hash = ?",
                (hashlib.sha256(f"failure:{session_id}".encode("utf-8")).hexdigest(),),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def read_events(self, state: Path, job_id: str) -> list[dict[str, object]]:
        log = state / "logs" / f"{job_id}.jsonl"
        return [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def wait_for_calls(self, calls: Path, *, count: int = 1) -> list[list[str]]:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if calls.exists():
                lines = [line for line in calls.read_text(encoding="utf-8").splitlines() if line.strip()]
                if len(lines) >= count:
                    return [json.loads(line) for line in lines]
            time.sleep(0.05)
        self.fail(f"fake codex did not record {count} call(s)")

    def run_failure(
        self,
        state: Path,
        *,
        error: object = "exceeded retry limit, last status: 429 Too Many Requests",
        status: object = "failed",
        **overrides: object,
    ) -> subprocess.CompletedProcess[str]:
        body: dict[str, object] = {
            "hook_event_name": "PostModelRequest",
            "session_id": "session-failure-1",
            "turn_id": "turn-failure-1",
            "cwd": str(ROOT),
            "model": "APUS Private DS v4.1 Flash #2",
            "status": status,
            "error": error,
            "token_usage": None,
        }
        body.update(overrides)
        return run_hook(body, state)

    def test_retryable_failure_spawns_a_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = write_fake_codex(root)
            result = run_hook(
                {
                    "hook_event_name": "PostModelRequest",
                    "session_id": "session-failure-1",
                    "turn_id": "turn-failure-1",
                    "cwd": str(root),
                    "model": "test-model",
                    "status": "failed",
                    "error": "exceeded retry limit, last status: 429 Too Many Requests",
                },
                root / "state",
                env_updates={
                    "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                    "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                    "DROID_AUTO_CONTINUE_QUEUE_SETTLE_SECONDS": "0.05",
                },
            )
            job_id, job_state, api_attempts, exit_code = self.wait_for_job(
                root / "state",
                detail_contains="pending later rollout reconciliation",
            )
            recorded = self.wait_for_calls(calls)
            events = self.read_events(root / "state", job_id)
            streak = self.streak_attempts(root / "state")
            with sqlite3.connect(str(root / "state" / "streaks.sqlite3")) as connection:
                verify_until, queued_detail = connection.execute(
                    "SELECT verify_until, detail FROM resume_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(recorded), 1)
        argv = recorded[0]
        self.assertEqual(argv[:4], ["queue", "--thread", "session-failure-1", "--message"])
        self.assertEqual(argv[-1], "--resume")
        self.assertTrue(argv[4].startswith("继续。"))
        self.assertIn("session-failure-1", argv)
        output = json.loads(result.stdout)
        self.assertIn("systemMessage", output)
        self.assertIn("已安排后台续跑", output["systemMessage"])
        self.assertEqual((job_state, api_attempts, exit_code, streak), ("queued", 1, 0, 1))
        self.assertEqual(events[-1]["event"], "queue_not_observed")
        self.assertEqual(events[-1]["new_turn_seen"], False)
        self.assertGreater(verify_until, int(time.time()))
        self.assertIn("pending later rollout reconciliation", queued_detail)

    def test_non_retryable_failures_do_not_spawn(self) -> None:
        cases = {
            "unauthorized": "401 Unauthorized",
            "quota": "Your quota has been exceeded",
            "context": "This model's maximum context length is 128000 tokens",
        }
        for name, message in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                calls = write_fake_codex(root)
                result = run_hook(
                    {
                        "hook_event_name": "PostModelRequest",
                        "session_id": "session-failure-1",
                        "cwd": str(root),
                        "status": "failed",
                        "error": message,
                    },
                    root / "state",
                    env_updates={
                        "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                        "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                    },
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(calls.exists())

    def test_completed_model_request_does_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_failure(root / "state", status="completed", error=None)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_the_same_failure_is_not_retried_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = write_fake_codex(root)
            env = {
                "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                "DROID_AUTO_CONTINUE_QUEUE_SETTLE_SECONDS": "0.1",
            }
            results = [
                run_hook(
                    {
                        "hook_event_name": "PostModelRequest",
                        "session_id": "session-failure-1",
                        "turn_id": "turn-failure-1",
                        "cwd": str(root),
                        "status": "failed",
                        "error": "exceeded retry limit, last status: 429 Too Many Requests",
                    },
                    root / "state",
                    env_updates=env,
                )
                for _ in range(3)
            ]
            job_id, job_state, api_attempts, _ = self.wait_for_job(root / "state")
            self.wait_for_calls(calls)
            time.sleep(0.2)
            lines = [
                line
                for line in calls.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ] if calls.exists() else []
            streak = self.streak_attempts(root / "state")

        self.assertEqual([r.returncode for r in results], [0, 0, 0])
        self.assertEqual(len(lines), 1)
        self.assertEqual((api_attempts, streak), (1, 1))
        self.assertEqual([r.stdout for r in results[1:]], ["", ""])

    def test_different_errors_in_the_same_turn_create_only_one_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = write_fake_codex(root)
            env = {
                "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                "DROID_AUTO_CONTINUE_QUEUE_SETTLE_SECONDS": "0.05",
            }
            errors = [
                "exceeded retry limit, last status: 429 Too Many Requests",
                "stream disconnected before completion",
            ]
            for error in errors:
                run_hook(
                    {
                        "hook_event_name": "PostModelRequest",
                        "session_id": "session-failure-1",
                        "turn_id": "turn-failure-1",
                        "cwd": str(root),
                        "status": "failed",
                        "error": error,
                    },
                    root / "state",
                    env_updates=env,
                )
            job_id, job_state, api_attempts, _ = self.wait_for_job(root / "state")
            recorded = self.wait_for_calls(calls)
            time.sleep(0.2)
            with sqlite3.connect(str(root / "state" / "streaks.sqlite3")) as connection:
                jobs = connection.execute("SELECT job_id FROM resume_jobs").fetchall()
            streak = self.streak_attempts(root / "state")

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0][0], job_id)
        self.assertEqual(len(recorded), 1)
        self.assertEqual((api_attempts, streak), (1, 1))
        self.assertEqual(job_state, "queued")

    def test_later_hook_reconciles_a_queued_job_to_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "rollout.jsonl"
            write_transcript(
                transcript,
                [
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_started",
                            "turn_id": "turn-failure-1",
                        },
                    }
                ],
            )
            calls = write_fake_codex(root)
            env = {
                "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                "DROID_AUTO_CONTINUE_QUEUE_SETTLE_SECONDS": "0.05",
            }
            run_hook(
                {
                    "hook_event_name": "PostModelRequest",
                    "session_id": "session-failure-1",
                    "turn_id": "turn-failure-1",
                    "transcript_path": str(transcript),
                    "cwd": str(root),
                    "status": "failed",
                    "error": "exceeded retry limit, last status: 429 Too Many Requests",
                },
                root / "state",
                env_updates=env,
            )
            job_id, job_state, _, _ = self.wait_for_job(
                root / "state",
                states={"queued"},
                detail_contains="pending later rollout reconciliation",
            )
            self.assertEqual(job_state, "queued")
            append_task_started(transcript, "turn-after-queue")
            # 任意后续 hook 都会先做 rollout 对账；用非可重试错误触发入口，
            # 避免再建一个新 job。
            reconcile = run_hook(
                {
                    "hook_event_name": "PostModelRequest",
                    "session_id": "session-failure-1",
                    "turn_id": "turn-failure-1",
                    "transcript_path": str(transcript),
                    "cwd": str(root),
                    "status": "failed",
                    "error": "401 Unauthorized",
                },
                root / "state",
                env_updates=env,
            )
            with sqlite3.connect(str(root / "state" / "streaks.sqlite3")) as connection:
                state, new_turn_seen = connection.execute(
                    "SELECT state, new_turn_seen FROM resume_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            events = [
                event
                for event in self.read_events(root / "state", job_id)
                if event["event"] == "job_reconciled"
            ]

        self.assertEqual(reconcile.returncode, 0, reconcile.stderr)
        self.assertEqual((state, new_turn_seen), ("recovered", 1))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["state"], "recovered")

    def test_queue_nonzero_unknown_outcome_does_not_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = write_fake_codex(
                root,
                queue_exit=1,
                queue_message="network connection reset",
            )
            result = run_hook(
                {
                    "hook_event_name": "PostModelRequest",
                    "session_id": "session-failure-1",
                    "turn_id": "turn-failure-1",
                    "cwd": str(root),
                    "status": "failed",
                    "error": "exceeded retry limit, last status: 429 Too Many Requests",
                },
                root / "state",
                env_updates={
                    "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                    "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                    "DROID_AUTO_CONTINUE_QUEUE_SETTLE_SECONDS": "0.05",
                },
            )
            job_id, job_state, api_attempts, _ = self.wait_for_job(root / "state")
            recorded = self.wait_for_calls(calls)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([argv[:1] for argv in recorded], [["queue"]])
        self.assertEqual((job_state, api_attempts), ("queued", 1))

    def test_the_same_error_in_a_new_turn_creates_a_new_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = write_fake_codex(root)
            env = {
                "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                "DROID_AUTO_CONTINUE_QUEUE_SETTLE_SECONDS": "0.1",
            }
            for turn in ("turn-1", "turn-2"):
                run_hook(
                    {
                        "hook_event_name": "PostModelRequest",
                        "session_id": "session-failure-1",
                        "turn_id": turn,
                        "cwd": str(root),
                        "status": "failed",
                        "error": "exceeded retry limit, last status: 429 Too Many Requests",
                    },
                    root / "state",
                    env_updates=env,
                )
                self.wait_for_job(root / "state")
            for _ in range(100):
                if calls.exists() and len(calls.read_text(encoding="utf-8").splitlines()) >= 2:
                    break
                time.sleep(0.05)
            connection = sqlite3.connect(str(root / "state" / "streaks.sqlite3"))
            jobs = connection.execute("SELECT job_id FROM resume_jobs").fetchall()
            connection.close()
            recorded = calls.read_text(encoding="utf-8").splitlines()
            streak = self.streak_attempts(root / "state")

        self.assertEqual(len(jobs), 2)
        self.assertEqual(len(recorded), 2)
        self.assertEqual(streak, 2)

    def test_a_writer_lock_that_never_clears_is_reported(self) -> None:
        fcntl = __import__("fcntl")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = write_fake_codex(root, queue_exit=1, queue_message="unsupported queue")
            home = root / "codex-home"
            locks = home / "thread-writer-locks"
            locks.mkdir(parents=True)
            lock_path = locks / "session-failure-1.lock"
            holder = lock_path.open("wb")
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            try:
                result = run_hook(
                    {
                        "hook_event_name": "PostModelRequest",
                        "session_id": "session-failure-1",
                        "turn_id": "turn-failure-1",
                        "cwd": str(root),
                        "status": "failed",
                        "error": "exceeded retry limit, last status: 429 Too Many Requests",
                    },
                    root / "state",
                    env_updates={
                        "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                        "CODEX_HOME": str(home),
                        "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                        "DROID_AUTO_CONTINUE_LOCK_WAIT_SECONDS": "0.4",
                        "DROID_AUTO_CONTINUE_LOCK_POLL_SECONDS": "0.05",
                    },
                )
                job_id, job_state, api_attempts, _ = self.wait_for_job(
                    root / "state",
                    states={"blocked_active_writer"},
                )
                events = self.read_events(root / "state", job_id)
                recorded = self.wait_for_calls(calls)
                streak = self.streak_attempts(root / "state")
            finally:
                holder.close()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([argv[:1] for argv in recorded], [["queue"]])
        self.assertEqual(job_state, "blocked_active_writer")
        self.assertEqual((api_attempts, streak), (1, 1))
        self.assertIn("lock_wait_timed_out", [event["event"] for event in events])

    def test_resume_launch_failure_is_not_counted_as_an_api_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_hook(
                {
                    "hook_event_name": "PostModelRequest",
                    "session_id": "session-failure-1",
                    "turn_id": "turn-failure-1",
                    "cwd": str(root),
                    "status": "failed",
                    "error": "exceeded retry limit, last status: 429 Too Many Requests",
                },
                root / "state",
                env_updates={
                    "PATH": str(root / "empty-bin"),
                    "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                },
            )
            job_id, job_state, api_attempts, _ = self.wait_for_job(root / "state")
            connection = sqlite3.connect(str(root / "state" / "streaks.sqlite3"))
            connection.close()
            streak = self.streak_attempts(root / "state")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(job_state, "failed_to_launch")
        self.assertEqual((api_attempts, streak), (0, 0))
        self.assertIn("后台续跑没有启动", json.loads(result.stdout)["systemMessage"])

    def test_queue_acceptance_is_a_recovery_only_after_a_new_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "rollout.jsonl"
            write_transcript(transcript, [])
            calls = write_fake_codex(root, queue_append_task="turn-after-429")
            result = run_hook(
                {
                    "hook_event_name": "PostModelRequest",
                    "session_id": "session-failure-1",
                    "turn_id": "turn-failure-1",
                    "transcript_path": str(transcript),
                    "cwd": str(root),
                    "status": "failed",
                    "error": "exceeded retry limit, last status: 429 Too Many Requests",
                },
                root / "state",
                env_updates={
                    "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                    "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                    "DROID_AUTO_CONTINUE_QUEUE_SETTLE_SECONDS": "1",
                },
            )
            job_id, job_state, api_attempts, exit_code = self.wait_for_job(
                root / "state",
                states={"recovered"},
            )
            recorded = self.wait_for_calls(calls)
            events = self.read_events(root / "state", job_id)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([argv[:1] for argv in recorded], [["queue"]])
        self.assertEqual((job_state, api_attempts, exit_code), ("recovered", 1, 0))
        self.assertEqual(events[-1]["event"], "queue_recovered")

    def test_queue_failure_falls_back_to_exec_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = write_fake_codex(
                root,
                queue_exit=1,
                queue_message="unknown command queue",
                exec_exit=0,
            )
            result = run_hook(
                {
                    "hook_event_name": "PostModelRequest",
                    "session_id": "session-failure-1",
                    "turn_id": "turn-failure-1",
                    "cwd": str(root),
                    "status": "failed",
                    "error": "exceeded retry limit, last status: 429 Too Many Requests",
                },
                root / "state",
                env_updates={
                    "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                    "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                    "DROID_AUTO_CONTINUE_LOCK_WAIT_SECONDS": "0.1",
                    "DROID_AUTO_CONTINUE_LOCK_POLL_SECONDS": "0.05",
                },
            )
            job_id, job_state, api_attempts, exit_code = self.wait_for_job(
                root / "state",
                states={"completed_unverified"},
            )
            recorded = self.wait_for_calls(calls, count=2)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([argv[:1] for argv in recorded], [["queue"], ["exec"]])
        self.assertEqual(recorded[1][:2], ["exec", "resume"])
        self.assertEqual((job_state, api_attempts, exit_code), ("completed_unverified", 1, 0))

    def test_unknown_queue_outcome_does_not_dispatch_a_duplicate_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = write_fake_codex(root, queue_sleep=5)
            result = run_hook(
                {
                    "hook_event_name": "PostModelRequest",
                    "session_id": "session-failure-1",
                    "turn_id": "turn-failure-1",
                    "cwd": str(root),
                    "status": "failed",
                    "error": "exceeded retry limit, last status: 429 Too Many Requests",
                },
                root / "state",
                env_updates={
                    "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                    "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                    "DROID_AUTO_CONTINUE_LOCK_WAIT_SECONDS": "0.1",
                    "DROID_AUTO_CONTINUE_QUEUE_TIMEOUT_SECONDS": "1",
                },
            )
            job_id, job_state, api_attempts, _ = self.wait_for_job(root / "state")
            recorded = self.wait_for_calls(calls)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([argv[:1] for argv in recorded], [["queue"]])
        # queue 超时说明消息可能已经写入；此时不能回退再发一次造成重复 turn，
        # 也不能在验证窗口内直接判失败。
        self.assertEqual(job_state, "queued")
        self.assertEqual(api_attempts, 1)


class DroidFailureStopTests(unittest.TestCase):
    """Droid 的 429 会进入 Stop，错误已经在 transcript 里。"""

    def run_stop(
        self,
        records: list[dict[str, object]],
        state: Path,
        transcript: Path,
        **payload_updates: object,
    ) -> subprocess.CompletedProcess[str]:
        write_transcript(transcript, records)
        return run_hook(
            payload(transcript, **payload_updates),
            state,
            env_updates={
                "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                "DROID_AUTO_CONTINUE_FAILURE_MAX_DELAY": "0",
            },
        )

    def test_network_error_after_a_tool_result_continues_in_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assistant_record = assistant(
                "tool-1",
                [{"type": "tool_use", "name": "Execute", "input": {"command": "git log"}}],
            )
            assistant_record["message"]["modelId"] = "custom:Retry Model-9"
            result = self.run_stop(
                [
                    user("继续改文档"),
                    assistant_record,
                    user("deadbeef", "tool-result"),
                    provider_error(
                        "错误：Your request to the LLM failed due to network issues. "
                        "Please try again later."
                    ),
                    stop_hook_record(),
                    {
                        "type": "agent_turn_outcome",
                        "turnId": "turn-1",
                        "reason": "model_provider_unavailable",
                        "resultKind": "text",
                    },
                ],
                root / "state",
                root / "session.jsonl",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("API 层失败", output["reason"])
        self.assertIn("不要重复已经完成", output["reason"])
        self.assertIn("自动续跑 1/4", output["reason"])
        self.assertEqual(
            output["systemMessage"],
            "Retry Model 因 API 失败中断，正在自动重试 1/4...",
        )
        self.assertNotIn("codex exec", result.stdout)

    def test_retryable_droid_errors_continue(self) -> None:
        cases = {
            "429": "BYOK Error: exceeded retry limit, last status: 429 Too Many Requests",
            "503": "BYOK Error: 503 所有供应商已熔断，无可用渠道",
            "openai": (
                "BYOK Error: An error occurred while processing your request. "
                "You can retry your request, or contact us through our help center."
            ),
            "stream": "BYOK Error: stream_read_error\n\nUpstream error: stream_read_error",
            "eof": "BYOK Error: JSON Parse error: Unexpected EOF",
            "socket": "BYOK Error: 403 socket closed unexpectedly",
            "empty": "BYOK Error: LLM response contained no usable output",
            "both": "BYOK Error: 500 service is experiencing high traffic, please try again",
        }
        for name, text in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                visibility = "both" if name == "both" else "user_only"
                result = self.run_stop(
                    [provider_error(text, visibility=visibility)],
                    root / "state",
                    root / "session.jsonl",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["decision"], "block")
                self.assertIn("正在自动重试 1/4", output["systemMessage"])

    def test_terminal_and_non_turn_errors_do_not_continue(self) -> None:
        cases = {
            "401": provider_error("BYOK Error: 401 Unauthorized"),
            "quota": provider_error("BYOK Error: Your quota has been exceeded"),
            "context": provider_error(
                "BYOK Error: This model's maximum context length is 128000 tokens"
            ),
            "model": provider_error(
                'BYOK Error: 503 {"error":{"code":"model_not_found",'
                '"message":"No available channel for model claude"}}'
            ),
            "400": provider_error("BYOK Error: 400 status code (no body)"),
            "403": provider_error("BYOK Error: 403 status code (no body)"),
            "param": provider_error(
                "BYOK Error: 400 The parameter max_tokens is not valid: BadRequest"
            ),
            "background": provider_error(
                "Background task error.\nreason: model_provider_unavailable\n"
                "The model provider was temporarily unavailable.",
                visibility="llm_only",
            ),
            "cancel": provider_error("Request interrupted by user", visibility="llm_only"),
            "user-typed": user("日志里有 429 Too Many Requests，帮我看看"),
        }
        for name, record in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result = self.run_stop([record], root / "state", root / "session.jsonl")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_same_record_is_not_retried_twice_but_a_new_one_is(self) -> None:
        text = (
            "错误：Your request to the LLM failed due to network issues. "
            "Please try again later."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self.run_stop(
                [provider_error(text, "err-a")],
                root / "state",
                root / "a.jsonl",
            )
            duplicate = self.run_stop(
                [provider_error(text, "err-a")],
                root / "state",
                root / "a.jsonl",
                session_id="session-1",
            )
            second = self.run_stop(
                [provider_error(text, "err-b")],
                root / "state",
                root / "b.jsonl",
            )

        self.assertIn("正在自动重试 1/4", first.stdout)
        self.assertEqual(duplicate.stdout, "")
        self.assertIn("正在自动重试 2/4", second.stdout)

    def test_attempt_cap_stops_the_streak(self) -> None:
        text = "BYOK Error: 429 Too Many Requests"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = [
                self.run_stop(
                    [provider_error(text, f"err-{index}")],
                    root / "state",
                    root / f"{index}.jsonl",
                )
                for index in range(5)
            ]

        self.assertEqual(
            [json.loads(result.stdout)["systemMessage"].endswith(f"{index + 1}/4...")
             for index, result in enumerate(results[:4])],
            [True, True, True, True],
        )
        self.assertEqual(results[4].stdout, "")

    def test_zero_max_attempts_disables_the_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [provider_error("BYOK Error: 429 Too Many Requests")],
            )
            result = run_hook(
                payload(transcript),
                root / "state",
                env_updates={
                    "DROID_AUTO_CONTINUE_FAILURE_MAX_ATTEMPTS": "0",
                    "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                },
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_unfinished_detector_flag_does_not_disable_api_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            write_transcript(
                transcript,
                [provider_error("BYOK Error: 429 Too Many Requests")],
            )
            result = run_hook(
                payload(transcript),
                root / "state",
                env_updates={
                    "DROID_AUTO_CONTINUE_DETECT_UNFINISHED": "0",
                    "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                    "DROID_AUTO_CONTINUE_FAILURE_MAX_DELAY": "0",
                },
            )

        self.assertIn("因 API 失败中断", result.stdout)

    def test_a_later_answer_is_not_treated_as_an_api_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            answered = self.run_stop(
                [
                    provider_error("BYOK Error: 429 Too Many Requests", "old-err"),
                    assistant("done", [{"type": "text", "text": "表已经填好。"}]),
                ],
                root / "state",
                root / "answered.jsonl",
            )
            silent = self.run_stop(
                [
                    provider_error("BYOK Error: 429 Too Many Requests", "old-err"),
                    assistant(
                        "drop",
                        [{"type": "thinking", "thinking": "流在这里断了"}],
                        model_id=CUSTOM_DROP_MODEL,
                    ),
                ],
                root / "state-b",
                root / "silent.jsonl",
            )

        self.assertEqual(answered.stdout.strip(), "本轮任务正常完成。")
        silent_output = json.loads(silent.stdout)
        self.assertIn("发生中断，正在自动重试 1/4", silent_output["systemMessage"])
        self.assertNotIn("API 失败", silent_output["systemMessage"])


class ResumeHelperTests(unittest.TestCase):
    """后台辅助进程：先退避，优先 queue，失败再等写锁后 exec。"""

    def run_helper(
        self,
        *args: str,
        mode: str = "--resume-after-lock",
        env_updates: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        for name in ENV_VARS:
            env.pop(name, None)
        if not env_updates or "DROID_AUTO_CONTINUE_STATE_DIR" not in env_updates:
            with tempfile.TemporaryDirectory() as tmp:
                env["DROID_AUTO_CONTINUE_STATE_DIR"] = str(Path(tmp) / "state")
                if env_updates:
                    env.update(env_updates)
                return subprocess.run(
                    [sys.executable, str(SCRIPT), mode, *args],
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    env=env,
                    timeout=120,
                )
        env.update(env_updates)
        return subprocess.run(
            [sys.executable, str(SCRIPT), mode, *args],
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=env,
            timeout=120,
        )

    def test_resume_job_argument_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fake_codex(root)
            overrides = ["-c", "model_provider=fake"]
            result = self.run_helper(
                "job-123",
                "session-map-1",
                str(root),
                "0",
                json.dumps(overrides),
                str(root / "rollout.jsonl"),
                "turn-map-1",
                mode="--resume-job",
                env_updates={
                    "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                    "DROID_AUTO_CONTINUE_STATE_DIR": str(root / "state"),
                    "DROID_AUTO_CONTINUE_QUEUE_SETTLE_SECONDS": "0.1",
                },
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_helper_resumes_once_the_lock_is_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = write_fake_codex(
                root,
                queue_exit=1,
                queue_message="unknown subcommand: queue",
            )
            home = root / "codex-home"
            (home / "thread-writer-locks").mkdir(parents=True)
            result = self.run_helper(
                "session-helper-1",
                str(root),
                "0",
                json.dumps(
                    ["-c", "model_provider=fake", "-c", 'sandbox_mode="danger-full-access"']
                ),
                env_updates={
                    "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                    "CODEX_HOME": str(home),
                    "DROID_AUTO_CONTINUE_LOCK_WAIT_SECONDS": "0.1",
                    "DROID_AUTO_CONTINUE_LOCK_POLL_SECONDS": "0.05",
                },
            )
            recorded = [
                json.loads(line)
                for line in calls.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([argv[:1] for argv in recorded], [["queue"], ["exec"]])
        self.assertEqual(recorded[1][:2], ["exec", "resume"])
        self.assertIn("--skip-git-repo-check", recorded[1])
        self.assertIn("session-helper-1", recorded[1])
        # queue 不可用时，hook 进程算好的覆盖必须原样带到 exec resume。
        self.assertIn("model_provider=fake", recorded[1])
        self.assertIn('sandbox_mode="danger-full-access"', recorded[1])

    def test_helper_queues_without_waiting_for_the_writer_lock(self) -> None:
        fcntl = __import__("fcntl")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = write_fake_codex(root)
            home = root / "codex-home"
            locks = home / "thread-writer-locks"
            locks.mkdir(parents=True)
            lock_path = locks / "session-helper-queue.lock"
            holder = lock_path.open("wb")
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            try:
                result = self.run_helper(
                    "session-helper-queue",
                    str(root),
                    "0",
                    "[]",
                    env_updates={
                        "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                        "CODEX_HOME": str(home),
                        "DROID_AUTO_CONTINUE_QUEUE_SETTLE_SECONDS": "0.1",
                    },
                )
                recorded = [
                    json.loads(line)
                    for line in calls.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            finally:
                holder.close()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([argv[:1] for argv in recorded], [["queue"]])

    def test_helper_waits_while_the_writer_lock_is_held(self) -> None:
        """queue 不可用时，锁被占用必须等释放，不能提前 exec。"""
        fcntl = __import__("fcntl")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = write_fake_codex(
                root,
                queue_exit=1,
                queue_message="unknown subcommand: queue",
            )
            home = root / "codex-home"
            locks = home / "thread-writer-locks"
            locks.mkdir(parents=True)
            lock_path = locks / "session-helper-2.lock"
            holder = lock_path.open("wb")
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--resume-after-lock",
                    "session-helper-2",
                    str(root),
                    "0",
                    "[]",
                ],
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                    "CODEX_HOME": str(home),
                    "DROID_AUTO_CONTINUE_STATE_DIR": str(root / "state"),
                    "DROID_AUTO_CONTINUE_LOCK_WAIT_SECONDS": "30",
                    "DROID_AUTO_CONTINUE_LOCK_POLL_SECONDS": "0.05",
                },
            )
            try:
                time.sleep(1.5)
                during = [
                    json.loads(line)
                    for line in calls.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                self.assertEqual([argv[:1] for argv in during], [["queue"]])
                fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                holder.close()
                proc.communicate(timeout=60)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
                if not holder.closed:
                    holder.close()

            recorded = [
                json.loads(line)
                for line in calls.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertEqual([argv[:1] for argv in recorded], [["queue"], ["exec"]])

    def test_resume_job_records_recovery_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "rollout.jsonl"
            write_transcript(transcript, [])
            calls = write_fake_codex(
                root,
                queue_exit=1,
                queue_message="unsupported queue",
                exec_append_task="turn-exec-new",
            )
            overrides = json.dumps([])
            recovered = self.run_helper(
                "job-recovered",
                "session-helper-recovered",
                str(root),
                "0",
                overrides,
                str(transcript),
                "turn-failure",
                mode="--resume-job",
                env_updates={
                    "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                    "DROID_AUTO_CONTINUE_STATE_DIR": str(root / "state"),
                    "DROID_AUTO_CONTINUE_QUEUE_SETTLE_SECONDS": "0.1",
                    "DROID_AUTO_CONTINUE_LOCK_WAIT_SECONDS": "0.1",
                },
            )
            with sqlite3.connect(str(root / "state" / "streaks.sqlite3")) as connection:
                recovered_state = connection.execute(
                    "SELECT state, new_turn_seen FROM resume_jobs WHERE job_id = ?",
                    ("job-recovered",),
                ).fetchone()

            failed_root = root / "failed"
            failed_root.mkdir()
            write_transcript(failed_root / "rollout.jsonl", [])
            write_fake_codex(
                failed_root,
                queue_exit=1,
                queue_message="unsupported queue",
                exec_exit=7,
                exec_message="provider unavailable",
            )
            failed = self.run_helper(
                "job-failed",
                "session-helper-failed",
                str(failed_root),
                "0",
                overrides,
                str(failed_root / "rollout.jsonl"),
                "turn-failure",
                mode="--resume-job",
                env_updates={
                    "PATH": f"{failed_root}{os.pathsep}{os.environ.get('PATH', '')}",
                    "DROID_AUTO_CONTINUE_STATE_DIR": str(root / "state"),
                    "DROID_AUTO_CONTINUE_QUEUE_SETTLE_SECONDS": "0.1",
                    "DROID_AUTO_CONTINUE_LOCK_WAIT_SECONDS": "0.1",
                },
            )
            with sqlite3.connect(str(root / "state" / "streaks.sqlite3")) as connection:
                failed_state = connection.execute(
                    "SELECT state, exit_code, detail FROM resume_jobs WHERE job_id = ?",
                    ("job-failed",),
                ).fetchone()
            failed_events = [
                json.loads(line)
                for line in (root / "state" / "logs" / "job-failed.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            recovered_calls = [
                json.loads(line)
                for line in calls.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(failed.returncode, 0, failed.stderr)
        self.assertEqual(recovered_state, ("recovered", 1))
        self.assertEqual([argv[:1] for argv in recovered_calls], [["queue"], ["exec"]])
        self.assertEqual(failed_state[0:2], ("failed", 7))
        self.assertIn("provider unavailable", failed_state[2])
        self.assertIn("provider unavailable", json.dumps(failed_events))

    def test_new_turn_during_lock_wait_avoids_exec(self) -> None:
        fcntl = __import__("fcntl")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "rollout.jsonl"
            write_transcript(
                transcript,
                [{"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-failure"}}],
            )
            calls = write_fake_codex(
                root,
                queue_exit=1,
                queue_message="unsupported queue",
            )
            home = root / "codex-home"
            locks = home / "thread-writer-locks"
            locks.mkdir(parents=True)
            holder = (locks / "session-helper-superseded.lock").open("wb")
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--resume-job",
                    "job-superseded",
                    "session-helper-superseded",
                    str(root),
                    "0",
                    "[]",
                    str(transcript),
                    "turn-failure",
                ],
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                    "CODEX_HOME": str(home),
                    "DROID_AUTO_CONTINUE_STATE_DIR": str(root / "state"),
                    "DROID_AUTO_CONTINUE_LOCK_WAIT_SECONDS": "30",
                    "DROID_AUTO_CONTINUE_LOCK_POLL_SECONDS": "0.05",
                },
            )
            try:
                time.sleep(1)
                append_task_started(transcript, "turn-new-before-exec")
                proc.communicate(timeout=30)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
                holder.close()

            with sqlite3.connect(str(root / "state" / "streaks.sqlite3")) as connection:
                job_state = connection.execute(
                    "SELECT state FROM resume_jobs WHERE job_id = ?",
                    ("job-superseded",),
                ).fetchone()[0]
            recorded = [
                json.loads(line)
                for line in calls.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertEqual(job_state, "superseded")
        self.assertEqual([argv[:1] for argv in recorded], [["queue"]])

    def test_helper_records_an_active_writer_timeout(self) -> None:
        fcntl = __import__("fcntl")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = write_fake_codex(root, queue_exit=1, queue_message="unsupported queue")
            home = root / "codex-home"
            locks = home / "thread-writer-locks"
            locks.mkdir(parents=True)
            holder = (locks / "session-helper-blocked.lock").open("wb")
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            try:
                result = self.run_helper(
                    "job-blocked",
                    "session-helper-blocked",
                    str(root),
                    "0",
                    "[]",
                    "",
                    "turn-failure",
                    mode="--resume-job",
                    env_updates={
                        "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                        "CODEX_HOME": str(home),
                        "DROID_AUTO_CONTINUE_STATE_DIR": str(root / "state"),
                        "DROID_AUTO_CONTINUE_LOCK_WAIT_SECONDS": "0.1",
                        "DROID_AUTO_CONTINUE_LOCK_POLL_SECONDS": "0.05",
                    },
                )
                with sqlite3.connect(str(root / "state" / "streaks.sqlite3")) as connection:
                    job_state = connection.execute(
                        "SELECT state FROM resume_jobs WHERE job_id = ?",
                        ("job-blocked",),
                    ).fetchone()[0]
                recorded = [
                    json.loads(line)
                    for line in calls.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            finally:
                holder.close()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(job_state, "blocked_active_writer")
        self.assertEqual([argv[:1] for argv in recorded], [["queue"]])


class ResumeJobMigrationTests(unittest.TestCase):
    """升级 0.5.0 不能因为旧 CHECK 拒绝 queued 而丢失恢复闭环。"""

    @staticmethod
    def create_legacy_database(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE streaks (
                    scope_hash TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL CHECK(
                        attempts >= 0 AND attempts <= 10
                    ),
                    assistant_hash TEXT,
                    last_seen INTEGER NOT NULL CHECK(last_seen >= 0),
                    progress TEXT
                );
                CREATE TABLE resume_jobs (
                    job_id TEXT PRIMARY KEY,
                    scope_hash TEXT NOT NULL,
                    session_hash TEXT NOT NULL,
                    turn_hash TEXT NOT NULL,
                    error_hash TEXT NOT NULL,
                    transcript_offset INTEGER NOT NULL DEFAULT 0 CHECK(transcript_offset >= 0),
                    baseline_at INTEGER NOT NULL DEFAULT 0 CHECK(baseline_at >= 0),
                    state TEXT NOT NULL CHECK(
                        state IN (
                            'blocked_active_writer',
                            'completed_unverified',
                            'dispatching',
                            'failed',
                            'failed_to_launch',
                            'recovered',
                            'scheduled',
                            'superseded',
                            'waiting_lock'
                        )
                    ),
                    dispatch_attempts INTEGER NOT NULL DEFAULT 0 CHECK(dispatch_attempts >= 0),
                    resume_pid INTEGER,
                    exit_code INTEGER,
                    new_turn_seen INTEGER CHECK(new_turn_seen IN (0, 1)),
                    detail TEXT,
                    created_at INTEGER NOT NULL CHECK(created_at >= 0),
                    updated_at INTEGER NOT NULL CHECK(updated_at >= 0),
                    api_attempts INTEGER NOT NULL DEFAULT 0,
                    transcript_path TEXT,
                    turn_id TEXT,
                    verify_until INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX resume_jobs_scope_created
                    ON resume_jobs(scope_hash, created_at);
                INSERT INTO resume_jobs (
                    job_id, scope_hash, session_hash, turn_hash, error_hash,
                    state, created_at, updated_at, detail
                ) VALUES (
                    'legacy-waiting', 'scope-a', 'session-a', 'turn-a', 'error-a',
                    'waiting_lock', 100, 100, 'old helper waited for the writer lock'
                );
                INSERT INTO resume_jobs (
                    job_id, scope_hash, session_hash, turn_hash, error_hash,
                    state, created_at, updated_at, detail
                ) VALUES (
                    'legacy-scheduled', 'scope-b', 'session-b', 'turn-b', 'error-b',
                    'scheduled', 101, 101, 'old helper was scheduled'
                );
                """
            )

    def load_module(self):
        spec = importlib.util.spec_from_file_location("ac_migration", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_upgrade_rebuilds_the_old_check_and_preserves_every_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            self.create_legacy_database(state / "streaks.sqlite3")
            env = {
                "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                "DROID_AUTO_CONTINUE_STATE_DIR": str(state),
            }
            module = self.load_module()
            old_env = os.environ.copy()
            os.environ.update(env)
            try:
                connection = module.connect()
                create_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'resume_jobs'"
                ).fetchone()[0]
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(resume_jobs)")
                }
                legacy_rows = connection.execute(
                    "SELECT job_id, state, detail FROM resume_jobs ORDER BY job_id"
                ).fetchall()
                indexes = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'resume_jobs'"
                    )
                }
                connection.execute(
                    "UPDATE resume_jobs SET state = 'queued', verify_until = ? WHERE job_id = ?",
                    (int(time.time()) + 60, "legacy-waiting"),
                )
                queued_state = connection.execute(
                    "SELECT state FROM resume_jobs WHERE job_id = 'legacy-waiting'"
                ).fetchone()[0]
                connection.commit()
                connection.close()
            finally:
                os.environ.clear()
                os.environ.update(old_env)

            calls = write_fake_codex(root)
            result = run_hook(
                {
                    "hook_event_name": "PostModelRequest",
                    "session_id": "session-migration-1",
                    "turn_id": "turn-migration-1",
                    "cwd": str(root),
                    "status": "failed",
                    "error": "exceeded retry limit, last status: 429 Too Many Requests",
                },
                state,
                env_updates={
                    "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                    "DROID_AUTO_CONTINUE_FAILURE_BASE_DELAY": "0",
                    "DROID_AUTO_CONTINUE_QUEUE_SETTLE_SECONDS": "0.05",
                },
            )
            # Wait for the newly created fake-queue job to finish verification.
            deadline = time.monotonic() + 8
            new_job = None
            while time.monotonic() < deadline:
                with sqlite3.connect(str(state / "streaks.sqlite3")) as connection:
                    new_job = connection.execute(
                        "SELECT job_id, state FROM resume_jobs "
                        "WHERE job_id NOT IN ('legacy-waiting', 'legacy-scheduled') "
                        "ORDER BY created_at DESC LIMIT 1"
                    ).fetchone()
                if new_job is not None and new_job[1] == "queued":
                    break
                time.sleep(0.05)
            with sqlite3.connect(str(state / "streaks.sqlite3")) as connection:
                records = connection.execute(
                    "SELECT job_id, state FROM resume_jobs ORDER BY job_id"
                ).fetchall()
                final_index = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'resume_jobs'"
                    )
                }
            call_deadline = time.monotonic() + 8
            recorded_calls: list[list[str]] = []
            while time.monotonic() < call_deadline:
                if calls.exists():
                    recorded_calls = [
                        json.loads(line)
                        for line in calls.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    if recorded_calls:
                        break
                time.sleep(0.05)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("'queued'", create_sql)
        self.assertTrue({"verify_until", "api_attempts", "transcript_path", "turn_id"} <= columns)
        self.assertEqual(
            legacy_rows,
            [
                ("legacy-scheduled", "scheduled", "old helper was scheduled"),
                ("legacy-waiting", "waiting_lock", "old helper waited for the writer lock"),
            ],
        )
        self.assertIn("resume_jobs_scope_created", indexes)
        self.assertIn("resume_jobs_scope_created", final_index)
        self.assertEqual(queued_state, "queued")
        self.assertEqual(new_job[1], "queued")
        self.assertEqual([argv[:1] for argv in recorded_calls], [["queue"]])
        self.assertEqual(len(records), 3)

class ProcessArgvTests(unittest.TestCase):
    """续跑要还原原进程命令行，必须精确拿到 argv。"""

    def load(self):
        spec = importlib.util.spec_from_file_location("ac", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_parses_macos_procargs_buffer_without_splitting_on_spaces(self) -> None:
        module = self.load()
        argv = [
            "/usr/local/bin/codex",
            "-c",
            'model_providers.azure-openai={name="Azure OpenAI",'
            'base_url="https://coding.apuscn.com/v1",wire_api="responses"}',
            "-c",
            "model=deepseek-ai/DeepSeek-V4.1-Flash",
            "--sandbox",
            "danger-full-access",
        ]
        buffer = struct.pack("<i", len(argv))
        buffer += b"/usr/local/bin/codex\0"
        buffer += b"\0" * 12
        buffer += b"\0".join(arg.encode() for arg in argv) + b"\0"
        buffer += b"PATH=/usr/bin\0HOME=/Users/x\0"

        parsed = module.parse_procargs2(buffer)

        self.assertEqual(parsed, argv)
        # 这一条是整个 macOS 支持的关键：带空格的参数不能被拆开。
        self.assertEqual(
            parsed[2],
            'model_providers.azure-openai={name="Azure OpenAI",'
            'base_url="https://coding.apuscn.com/v1",wire_api="responses"}',
        )

    def test_rejects_malformed_procargs_buffers(self) -> None:
        module = self.load()
        self.assertEqual(module.parse_procargs2(b""), [])
        self.assertEqual(module.parse_procargs2(b"\x01"), [])
        # argc 为 0 或负数不可信
        self.assertEqual(module.parse_procargs2(struct.pack("<i", 0) + b"x\0"), [])
        self.assertEqual(module.parse_procargs2(struct.pack("<i", -1) + b"x\0"), [])
        # argc 大得离谱时不能把整块内存当 argv 读
        self.assertEqual(module.parse_procargs2(struct.pack("<i", 10**9) + b"x\0"), [])
        # 没有 NUL 分隔
        self.assertEqual(module.parse_procargs2(struct.pack("<i", 1) + b"x"), [])

    def test_recovers_apus_style_overrides_from_codex_argv(self) -> None:
        """用 apus_codex.sh 真实那行 exec 验证还原结果。"""
        module = self.load()
        argv = [
            "codex",
            "-c",
            "model_provider=azure-openai",
            "-c",
            'model_providers.azure-openai={name="Azure OpenAI",'
            'base_url="https://coding.apuscn.com/v1",wire_api="responses",'
            'env_key="OPENAI_API_KEY",supports_websockets=false}',
            "-c",
            "model=deepseek-ai/DeepSeek-V4.1-Flash",
            "-c",
            "model_context_window=320000",
            "-c",
            'model_catalog_json="/home/u/.codex/apus-models.json"',
            "--sandbox",
            "danger-full-access",
            "--ask-for-approval",
            "never",
        ]
        module.parent_process_argv = lambda: argv

        overrides = module.resume_launch_overrides()
        joined = " ".join(overrides)

        self.assertIn("model_provider=azure-openai", joined)
        self.assertIn('model_providers.azure-openai={name="Azure OpenAI"', joined)
        self.assertIn("model_context_window=320000", joined)
        self.assertIn("model_catalog_json=", joined)
        # resume 不认 -s/--sandbox，必须换成 config 覆盖，否则沙箱静默降级。
        self.assertIn('sandbox_mode="danger-full-access"', joined)
        self.assertIn('approval_policy="never"', joined)
        self.assertNotIn("--sandbox", overrides)

    def test_darwin_reader_never_crashes_off_macos(self) -> None:
        """非 macOS 上 libc 没有 sysctl 符号，取值必须被吃掉而不是崩掉。

        这是实测踩过的坑：Linux 的 libc 不导出 sysctl，Python < 3.12 的
        ctypes 抛的是 AttributeError 而不是 OSError。
        """
        module = self.load()
        self.assertEqual(module.darwin_process_argv(os.getpid()), [])
        self.assertEqual(module.darwin_process_argv(1), [])

    def test_codex_argv_detection_accepts_paths(self) -> None:
        module = self.load()
        self.assertTrue(module.looks_like_codex_argv(["/opt/homebrew/bin/codex", "exec"]))
        self.assertTrue(module.looks_like_codex_argv(["codex"]))
        self.assertFalse(module.looks_like_codex_argv(["/bin/zsh", "-c", "codex"]))
        self.assertFalse(module.looks_like_codex_argv([]))


class PluginContractTests(unittest.TestCase):
    def test_hooks_use_the_droid_stop_shape(self) -> None:
        hooks = json.loads(HOOKS.read_text(encoding="utf-8"))
        command = hooks["Stop"][0]["hooks"][0]
        self.assertNotIn("hooks", hooks)
        self.assertEqual(command["type"], "command")
        self.assertIn("${DROID_PLUGIN_ROOT}/scripts/auto_continue_stop.py", command["command"])
        # API 失败的退避睡在这条 Stop 命令里，最长 40 秒。
        self.assertGreater(command["timeout"], 40)
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "custom-model-stop-patch")
        self.assertEqual(marketplace["name"], "droid-auto-retry")
        self.assertEqual(marketplace["plugins"][0]["name"], "custom-model-stop-patch")
        self.assertEqual(marketplace["plugins"][0]["source"], "./plugins/custom-model-stop-patch")
        self.assertNotIn("PLUGIN_ROOT", command["command"].split("${DROID_PLUGIN_ROOT}")[0])

        codex_hooks = json.loads(CODEX_HOOKS.read_text(encoding="utf-8"))
        codex_command = codex_hooks["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(codex_command["type"], "command")
        self.assertIn("${PLUGIN_ROOT}/scripts/auto_continue_stop.py", codex_command["command"])
        self.assertNotIn("DROID_PLUGIN_ROOT", codex_command["command"])
        self.assertGreaterEqual(codex_command["timeout"], 45)

        # API 失败时 Codex 不发 Stop，只发 PostModelRequest；没有这条注册，
        # 429 之后插件永远不会被唤醒。
        failure_command = codex_hooks["hooks"]["PostModelRequest"][0]["hooks"][0]
        self.assertEqual(failure_command["type"], "command")
        self.assertIn("${PLUGIN_ROOT}/scripts/auto_continue_stop.py", failure_command["command"])
        self.assertNotIn("DROID_PLUGIN_ROOT", failure_command["command"])
        # 退避可能要睡满 40 秒，超时必须比它长。
        self.assertGreater(failure_command["timeout"], 40)

        codex_manifest = json.loads(CODEX_MANIFEST.read_text(encoding="utf-8"))
        codex_marketplace = json.loads(CODEX_MARKETPLACE.read_text(encoding="utf-8"))
        self.assertEqual(codex_manifest["hooks"], "./hooks/codex-hooks.json")
        self.assertEqual(codex_manifest["name"], "custom-model-stop-patch")
        self.assertEqual(codex_marketplace["name"], "droid-auto-retry")
        self.assertEqual(
            codex_marketplace["plugins"][0]["source"],
            "./plugins/custom-model-stop-patch",
        )

    def test_script_compiles(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        compile(textwrap.dedent(source), str(SCRIPT), "exec")


if __name__ == "__main__":
    unittest.main()

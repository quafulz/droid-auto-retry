from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "custom-model-stop-patch"
SCRIPT = PLUGIN / "scripts" / "auto_continue_stop.py"
HOOKS = PLUGIN / "hooks" / "hooks.json"
MANIFEST = PLUGIN / ".factory-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".factory-plugin" / "marketplace.json"

ENV_VARS = {
    "DROID_AUTO_CONTINUE_MAX_ATTEMPTS",
    "DROID_AUTO_CONTINUE_BASE_DELAY",
    "DROID_AUTO_CONTINUE_MAX_DELAY",
    "DROID_AUTO_CONTINUE_BACKOFF_FACTOR",
    "DROID_AUTO_CONTINUE_STATE_TTL_SECONDS",
    "DROID_AUTO_CONTINUE_STATE_DIR",
    "DROID_AUTO_CONTINUE_TRANSCRIPT_TAIL_BYTES",
    "DROID_AUTO_CONTINUE_DETECT_UNFINISHED",
}


def write_transcript(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def assistant(record_id: str, content: list[dict[str, object]] | str | None) -> dict[str, object]:
    message: dict[str, object] = {"role": "assistant", "content": content}
    return {"type": "message", "id": record_id, "message": message}


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
                    assistant("drop-1", [{"type": "thinking", "thinking": "流在这里断了"}]),
                    outcome(),
                ],
            )
            result = run_hook(payload(transcript), root / "state")

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("自动续跑 1/4", output["reason"])
        self.assertIn("不要重复已经完成", output["reason"])
        self.assertEqual(output["systemMessage"], "Custom Model 发生中断，正在自动重试 1/4...")

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
                    )
                ],
            )
            result = run_hook(payload(transcript), root / "state")

        self.assertIn("自动续跑 1/4", result.stdout)
        self.assertEqual(
            json.loads(result.stdout)["systemMessage"],
            "Custom Model 发生中断，正在自动重试 1/4...",
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
                [assistant("drop-1", [{"type": "thinking", "thinking": "一次"}])],
            )
            first = run_hook(payload(transcript), state)
            write_transcript(
                transcript,
                [assistant("drop-2", [{"type": "thinking", "thinking": "两次"}])],
            )
            second = run_hook(payload(transcript, stop_hook_active=True), state)
            write_transcript(
                transcript,
                [assistant("drop-3", [{"type": "thinking", "thinking": "新一轮"}])],
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
                [assistant("same-drop", [{"type": "thinking", "thinking": "一次"}])],
            )
            env = {"DROID_AUTO_CONTINUE_MAX_ATTEMPTS": "1"}
            first = run_hook(payload(transcript), state, env_updates=env)
            duplicate = run_hook(payload(transcript), state, env_updates=env)
            write_transcript(
                transcript,
                [assistant("next-drop", [{"type": "thinking", "thinking": "两次"}])],
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


class PluginContractTests(unittest.TestCase):
    def test_hooks_use_the_droid_stop_shape(self) -> None:
        hooks = json.loads(HOOKS.read_text(encoding="utf-8"))
        command = hooks["Stop"][0]["hooks"][0]
        self.assertNotIn("hooks", hooks)
        self.assertEqual(command["type"], "command")
        self.assertIn("${DROID_PLUGIN_ROOT}/scripts/auto_continue_stop.py", command["command"])
        self.assertGreaterEqual(command["timeout"], 45)
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "custom-model-stop-patch")
        self.assertEqual(marketplace["name"], "droid-auto-retry")
        self.assertEqual(marketplace["plugins"][0]["name"], "custom-model-stop-patch")
        self.assertEqual(marketplace["plugins"][0]["source"], "./plugins/custom-model-stop-patch")
        self.assertNotIn("PLUGIN_ROOT", command["command"].split("${DROID_PLUGIN_ROOT}")[0])

    def test_script_compiles(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        compile(textwrap.dedent(source), str(SCRIPT), "exec")


if __name__ == "__main__":
    unittest.main()

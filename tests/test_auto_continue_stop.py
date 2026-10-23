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
PLUGIN = ROOT / "plugins" / "droid-auto-continue"
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
        self.assertEqual(result.stdout, "")

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
        self.assertEqual(manifest["name"], "droid-auto-continue")
        self.assertEqual(marketplace["name"], "droid-auto-retry")
        self.assertEqual(marketplace["plugins"][0]["source"], "./plugins/droid-auto-continue")
        self.assertNotIn("PLUGIN_ROOT", command["command"].split("${DROID_PLUGIN_ROOT}")[0])

    def test_script_compiles(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        compile(textwrap.dedent(source), str(SCRIPT), "exec")


if __name__ == "__main__":
    unittest.main()

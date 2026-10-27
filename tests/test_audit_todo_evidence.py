from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "audit_todo_evidence.py"
HOOK_SCRIPT = ROOT / "plugins" / "custom-model-stop-patch" / "scripts" / "auto_continue_stop.py"


def load_audit_module() -> Any:
    spec = importlib.util.spec_from_file_location("audit_todo_evidence_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


AUDIT = load_audit_module()


def write_session(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def assistant(record_id: str, content: list[dict[str, object]] | str) -> dict[str, object]:
    return {"type": "message", "id": record_id, "message": {"role": "assistant", "content": content}}


def todo_write_text(text: str) -> dict[str, object]:
    return assistant(
        "todo-call",
        [{"type": "tool_use", "id": "todo-use", "name": "TodoWrite", "input": {"todos": text}}],
    )


def todo_write_items(*items: tuple[str, str]) -> dict[str, object]:
    return assistant(
        "todo-call",
        [
            {
                "type": "tool_use",
                "id": "todo-use",
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


def todo_state(*items: tuple[str, str]) -> dict[str, object]:
    return {
        "type": "todo_state",
        "id": "state-1",
        "todos": {
            "todos": [
                {"id": str(index + 1), "status": status, "content": content}
                for index, (content, status) in enumerate(items)
            ]
        },
    }


class AuditScriptTests(unittest.TestCase):
    def run_script(self, root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--sessions-root", str(root)],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_reports_an_open_plan_as_would_continue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "-Users-someone-Projects-demo"
            session.mkdir()
            write_session(
                session / "aaaa1111-0000-0000-0000-000000000000.jsonl",
                [
                    todo_write_text("1. [completed] 读方案\n2. [in_progress] 跑校准"),
                    todo_state(("读方案", "completed"), ("跑校准", "in_progress")),
                    assistant("text-1", [{"type": "text", "text": "校准还在跑。"}]),
                ],
            )
            result = self.run_script(root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("判定=todos", result.stdout)
        self.assertIn("未完成=1/2", result.stdout)
        self.assertIn("跑校准", result.stdout)
        self.assertIn("work_after_call 反事实：0 个会话的判定会变化", result.stdout)

    def test_flags_a_plan_that_work_after_call_would_have_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "-Users-someone-Projects-demo"
            session.mkdir()
            write_session(
                session / "bbbb2222-0000-0000-0000-000000000000.jsonl",
                [
                    todo_write_text("1. [in_progress] Run script and verify output"),
                    todo_state(("Run script and verify output", "in_progress")),
                    assistant("work-1", [{"type": "tool_use", "name": "Execute"}]),
                    assistant("text-2", [{"type": "text", "text": "已提交到 git。"}]),
                ],
            )
            result = self.run_script(root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("work_after_call 反事实：1 个会话的判定会变化", result.stdout)
        self.assertIn("complete -> todos", result.stdout)

    def test_json_output_is_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "-Users-someone-Projects-demo"
            session.mkdir()
            write_session(
                session / "cccc3333-0000-0000-0000-000000000000.jsonl",
                [
                    todo_write_text("1. [pending] 复核结果"),
                    todo_state(("复核结果", "pending")),
                    assistant("text-3", [{"type": "text", "text": "先到这里。"}]),
                ],
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--sessions-root", str(root), "--json"],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "todos")
        self.assertEqual(rows[0]["open"], ["复核结果"])

    def test_reports_no_sessions_when_the_root_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_script(Path(tmp))

        self.assertEqual(result.returncode, 1)
        self.assertIn("没有找到可复核的 transcript", result.stderr)

    def test_both_scripts_compile(self) -> None:
        for path in (SCRIPT, HOOK_SCRIPT):
            compile(path.read_text(encoding="utf-8"), str(path), "exec")


class AuditHookAgreementTests(unittest.TestCase):
    """审计脚本复用的是 hook 的判据，两边不能各说各话。"""

    def test_evidence_decision_matches_the_hook(self) -> None:
        tail = assistant("text-1", [{"type": "text", "text": "先到这里。"}])
        cases = {
            "structured": (
                [todo_write_items(("读方案", "completed"), ("跑校准", "in_progress")), tail],
                True,
                "ok",
            ),
            "free-parsed": (
                [
                    todo_write_text("1. [completed] 读方案\n2. [in_progress] 跑校准"),
                    todo_state(("读方案", "completed"), ("跑校准", "in_progress")),
                    tail,
                ],
                True,
                "ok",
            ),
            "free-verbatim": (
                [
                    todo_write_text("1. 读方案"),
                    todo_state(("读方案", "pending")),
                    tail,
                ],
                False,
                "line_kept_verbatim",
            ),
            "free-without-state": (
                [todo_write_text("1. [in_progress] 读方案"), tail],
                False,
                "no_parsed_state",
            ),
            "free-count-mismatch": (
                [
                    todo_write_text("1. [in_progress] 读方案\n2. [pending] 跑校准"),
                    todo_state(("读方案", "in_progress")),
                    tail,
                ],
                False,
                "line_count_mismatch",
            ),
        }
        for name, (records, accepted, reason) in cases.items():
            with self.subTest(name):
                call = AUDIT.last_todo_write_call(records)
                self.assertIsNotNone(call)
                call_index, _, block = call
                _, reported, items = AUDIT.evidence_state(records, call_index, block)
                hook_items = AUDIT.hook.todo_evidence_for_call(
                    records, call_index, block
                )
                self.assertEqual(items is not None, accepted)
                self.assertEqual(hook_items is not None, accepted)
                self.assertEqual(reported, reason)

    def test_tool_count_matches_the_hook_staleness_check(self) -> None:
        records = [
            todo_write_text("1. [in_progress] 跑校准"),
            todo_state(("跑校准", "in_progress")),
            assistant("work-1", [{"type": "tool_use", "name": "Execute"}]),
            assistant("text-2", [{"type": "text", "text": "已提交。"}]),
        ]
        call_index, call_position, _ = AUDIT.last_todo_write_call(records)

        self.assertTrue(AUDIT.hook.work_after_call(records, call_index, call_position))
        self.assertEqual(AUDIT.count_tools_after(records, call_index, call_position), 1)

    def test_ignores_tool_blocks_outside_assistant_messages(self) -> None:
        records = [
            todo_write_text("1. [in_progress] 跑校准"),
            todo_state(("跑校准", "in_progress")),
            {
                "type": "tool_progress",
                "id": "progress-1",
                "content": [{"type": "tool_use", "name": "Execute"}],
            },
            assistant("text-2", [{"type": "text", "text": "先到这里。"}]),
        ]
        call_index, call_position, _ = AUDIT.last_todo_write_call(records)

        self.assertFalse(AUDIT.hook.work_after_call(records, call_index, call_position))
        self.assertEqual(AUDIT.count_tools_after(records, call_index, call_position), 0)


if __name__ == "__main__":
    unittest.main()

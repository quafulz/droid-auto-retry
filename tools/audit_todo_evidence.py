#!/usr/bin/env python3
"""复核 Stop hook 的待办判据在真实 transcript 上的表现。

回答两个问题：哪些会话因为待办被判续跑、其中哪些其实已经做完；以及
`work_after_call` 这条过时弃权有没有漏掉真正的续跑信号。脚本只读 transcript，
不修改任何状态。

    python3 tools/audit_todo_evidence.py --since 2026-09
    python3 tools/audit_todo_evidence.py --since 2026-10 --verdict todos --tail 200
    python3 tools/audit_todo_evidence.py --json > audit.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
HOOK_DIR = ROOT / "plugins" / "custom-model-stop-patch" / "scripts"
DEFAULT_SESSIONS_ROOT = Path.home() / ".factory" / "sessions"
POSITION_CHOICES = ("todos", "complete", "tool_use", "silent", "summary", "truncated", "unknown")

sys.path.insert(0, str(HOOK_DIR))
import auto_continue_stop as hook  # noqa: E402


def load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
    except OSError:
        return []
    return records


def last_todo_write_call(
    records: list[dict[str, Any]],
) -> tuple[int, int, dict[str, Any]] | None:
    calls = list(hook.iter_todo_write_calls(records))
    return calls[-1] if calls else None


def final_assistant_message(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    trimmed = list(records)
    while trimmed:
        message = trimmed[-1].get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            return trimmed, message
        trimmed.pop()
    return None


def evidence_state(
    records: list[dict[str, Any]],
    call_index: int,
    block: dict[str, Any],
) -> tuple[str, str, list[dict[str, Any]] | None]:
    """复述 hook 的采信链，返回 (入参形态, 结论, 待办项)。

    「是否采信」直接问 hook 本身，这里只补一条人读的原因，避免两处判据分叉。
    """
    value = block.get("input")
    entries = value.get("todos") if isinstance(value, dict) else None
    if isinstance(entries, list):
        items = hook.todo_items_from_input(value)
        return "structured", "ok" if items else "item_without_status", items
    if not isinstance(entries, str):
        return "other", "input_not_string_or_list", None

    state: list[dict[str, Any]] | None = None
    for record in records[call_index + 1 :]:
        state = hook.todo_items_from_state(record)
        if state is not None:
            break
    if state is None:
        return "free_text", "no_parsed_state", None

    if hook.free_text_todo_items(entries, state) is None:
        return "free_text", free_text_rejection_reason(entries, state), None
    return "free_text", "ok", state


def free_text_rejection_reason(
    text: str,
    items: list[dict[str, Any]],
) -> str:
    """说明自由文本为什么没被采信；只用于报告，不影响判定。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(lines) != len(items):
        return "line_count_mismatch"
    for line, item in zip(lines, items):
        if not isinstance(item.get("content"), str):
            return "item_without_content"
        if not hook.free_text_line_parsed(line, item):
            return "line_kept_verbatim"
    return "rejected"


def count_tools_after(
    records: list[dict[str, Any]],
    call_index: int,
    call_position: int,
) -> int:
    return sum(
        1
        for index, position, _ in hook.iter_tool_calls(records)
        if (index, position) > (call_index, call_position)
    )


def session_month(records: list[dict[str, Any]]) -> str:
    stamps = [str(record.get("timestamp") or "")[:7] for record in records]
    stamps = [stamp for stamp in stamps if len(stamp) == 7]
    return max(stamps) if stamps else ""


def verdict_without_work_after(records: list[dict[str, Any]], message: dict[str, Any]) -> str:
    original = hook.work_after_call
    hook.work_after_call = lambda *args, **kwargs: False
    try:
        return hook.classify_ending(records, message)[0]
    finally:
        hook.work_after_call = original


def describe(path: Path) -> dict[str, Any] | None:
    records = load_records(path)
    if not records:
        return None
    call = last_todo_write_call(records)
    if call is None:
        return None
    call_index, call_position, block = call
    final = final_assistant_message(records)
    if final is None:
        return None
    trimmed, message = final
    kind, detail = hook.classify_ending(trimmed, message)
    shape, state, items = evidence_state(records, call_index, block)
    open_labels = [
        hook.todo_label(item)
        for item in items or []
        if hook.todo_status(item) in hook.PENDING_TODO_STATUSES
    ]
    text = " ".join(hook.assistant_visible_text(message).split())
    return {
        "session": path.parent.name[-24:],
        "file": path.name[:8],
        "month": session_month(records),
        "verdict": kind,
        "detail": detail,
        "input_shape": shape,
        "evidence": state,
        "open": open_labels,
        "total_items": len(items or []),
        "tools_after": count_tools_after(records, call_index, call_position),
        "work_after": hook.work_after_call(records, call_index, call_position),
        "verdict_without_work_after": verdict_without_work_after(trimmed, message),
        "tail": text[-400:],
    }


def collect(root: Path, since: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/*.jsonl")):
        row = describe(path)
        if row is None:
            continue
        if since and row["month"] < since:
            continue
        rows.append(row)
    return rows


def print_report(rows: list[dict[str, Any]], tail_chars: int, verdicts: set[str]) -> None:
    chosen = [row for row in rows if row["verdict"] in verdicts]
    chosen.sort(key=lambda row: (row["verdict"] != "todos", -len(row["open"]), row["session"]))

    print(f"会话总数（有 TodoWrite 且有收尾消息）：{len(rows)}")
    print(f"按判定分组：{summary(rows)}")
    print(f"入参/采信分布：{evidence_summary(rows)}")
    print(f"本次列出：{len(chosen)}")
    print()

    for row in chosen:
        open_text = "；".join(row["open"][:3]) or "—"
        print(
            f"{row['month']} {row['session']:26s} {row['file']}  "
            f"判定={row['verdict']:8s} 未完成={len(row['open'])}/{row['total_items']}  "
            f"入参={row['input_shape']}/{row['evidence']}  之后工具={row['tools_after']}  "
            f"过时弃权={int(row['work_after'])}"
        )
        print(f"    未完成项：{open_text}")
        if row["tail"]:
            print(f"    收尾正文：{row['tail'][-tail_chars:]}")
        else:
            print("    收尾正文：（空）")
        if row["verdict_without_work_after"] != row["verdict"]:
            print(
                "    去掉 work_after 会改判为："
                f"{row['verdict_without_work_after']}（说明这条过时弃权改变了结果）"
            )
        print()

    changed = [
        row for row in rows if row["verdict_without_work_after"] != row["verdict"]
    ]
    print(f"work_after_call 反事实：{len(changed)} 个会话的判定会变化")
    for row in changed:
        print(
            f"  {row['session']} {row['file']}: "
            f"{row['verdict']} -> {row['verdict_without_work_after']}"
        )


def summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def evidence_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = f"{row['input_shape']}/{row['evidence']}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=DEFAULT_SESSIONS_ROOT,
        help=f"transcript 根目录，默认 {DEFAULT_SESSIONS_ROOT}",
    )
    parser.add_argument("--since", default=None, help="只统计该月份及之后，形如 2026-09")
    parser.add_argument(
        "--verdict",
        action="append",
        choices=POSITION_CHOICES,
        help="只看这些判定，可重复；默认只看 todos",
    )
    parser.add_argument("--tail", type=int, default=160, help="收尾正文显示多少字，默认 160")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本报告")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    rows = collect(args.sessions_root, args.since)
    if not rows:
        print(f"没有找到可复核的 transcript：{args.sessions_root}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    verdicts = set(args.verdict or ["todos"])
    print_report(rows, max(40, args.tail), verdicts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

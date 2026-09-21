"""The flagship invariant, enforced across the WHOLE package.

`VoiceLoop._execute` is the only place in daa that may invoke a tool. Every
safety control -- the risk gate, the tier policy, the spoken readback, the
typed visual approval, the undo record, the audit trail -- hangs off that one
call site. A second call site anywhere does not weaken the model, it bypasses
it entirely.

`test_voice_loop.py::test_execute_is_the_only_caller_of_tool_run` already
asserts this, but it does `inspect.getsource(daa.voice.loop)` and counts
occurrences in THAT MODULE ONLY. A background job runner living in a new
module could host a second call site and that test would stay green -- and a
job runner is exactly what the async design adds next. This file walks every
module under `src/daa` so the invariant cannot be escaped by moving house.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "daa"

# The single legitimate call site.
BLESSED = ("voice/loop.py", "VoiceLoop._execute")

# `run` is a common verb. These are the ones that are definitively not a tool:
# a subprocess, the loop's own segment pump, a CLI entry point.
IGNORED_RECEIVERS = {"subprocess", "loop", "self", "cli", "asyncio", "app"}


def _qualname(stack: list[ast.AST]) -> str:
    parts = [n.name for n in stack if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    return ".".join(parts)


def _run_calls(path: pathlib.Path) -> list[tuple[str, str, int]]:
    """Every `<something>.run(...)` call, with the receiver and enclosing scope."""
    tree = ast.parse(path.read_text())
    found: list[tuple[str, str, int]] = []
    stack: list[ast.AST] = []

    class V(ast.NodeVisitor):
        def visit(self, node):
            push = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            if push:
                stack.append(node)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
            ):
                recv = node.func.value
                name = getattr(recv, "id", None) or getattr(recv, "attr", None) or "?"
                if name not in IGNORED_RECEIVERS:
                    found.append((name, _qualname(stack), node.lineno))
            self.generic_visit(node)
            if push:
                stack.pop()

    V().visit(tree)
    return found


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in str(p))


def test_only_one_place_in_the_whole_package_invokes_a_tool():
    offenders: list[str] = []
    blessed_seen = False

    for path in _modules():
        rel = path.relative_to(SRC).as_posix()
        for recv, scope, line in _run_calls(path):
            where = f"{rel}:{line} ({scope or '<module>'}) -> {recv}.run(...)"
            if (rel, scope) == BLESSED:
                blessed_seen = True
                continue
            offenders.append(where)

    assert blessed_seen, (
        "VoiceLoop._execute no longer contains the tool invocation. If it moved, "
        "move BLESSED with it deliberately -- do not delete this assertion."
    )
    assert not offenders, (
        "a tool is invoked outside VoiceLoop._execute, bypassing the risk gate, "
        "the tier policy, the readback, the undo record and the audit trail:\n  "
        + "\n  ".join(offenders)
    )


def test_the_chokepoint_still_carries_its_guards():
    """The call site is worthless if its preconditions are removed."""
    import inspect

    from daa.voice.loop import VoiceLoop

    body = inspect.getsource(VoiceLoop._execute)
    for needle, why in [
        ("disposition", "must receive a Disposition"),
        ("REFUSE", "must refuse the REFUSE tier"),
        ("CONFIRM_VISUAL", "must require the visual token for the visual tier"),
        ("confirmed", "must require an affirmative confirmation at CONFIRM_VOICE"),
        ("dry_run", "must honour dry run"),
    ]:
        assert needle in body, f"_execute {why}; '{needle}' has gone from its body"


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_no_module_shells_out_without_going_through_base(path: pathlib.Path):
    """`subprocess` is allowed in exactly one place, for the same reason.

    `tools/base.py::run_argv` is the only spawner: it forbids `shell=True`,
    requires an argv list, always sets a timeout, and honours dry_run. A second
    spawner elsewhere would quietly opt out of all four.
    """
    rel = path.relative_to(SRC).as_posix()
    src = path.read_text()
    if "subprocess" not in src:
        return
    assert rel in {"tools/base.py", "voice/tts.py"}, (
        f"{rel} imports subprocess; spawning belongs in tools/base.py::run_argv"
    )

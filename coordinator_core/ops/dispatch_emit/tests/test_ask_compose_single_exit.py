"""The composed M+ ask script has exactly one `return` outside function bodies: the terminal one."""

from __future__ import annotations

from pathlib import PurePath

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.session.record_homes import record_path

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _m_plus_script(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel=PurePath(record_path("", "sizings", "x.yaml")).as_posix(),
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def _top_level_returns(script: str) -> list[int]:
    """Offsets of `return` keywords outside any `function` body or arrow-function body."""
    positions: list[int] = []
    n = len(script)
    i = 0
    depth = 0
    skip_until_depth: list[int] = []  # brace depths at which a function body opened
    pending_body = False
    while i < n:
        c = script[i]
        if c in "'\"`":
            q = c
            i += 1
            while i < n and script[i] != q:
                i += 2 if script[i] == "\\" else 1
            i += 1
            continue
        if script.startswith("//", i):
            i = script.find("\n", i)
            i = n if i < 0 else i
            continue
        if script.startswith("/*", i):
            j = script.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        if c == "/" and script[:i].rstrip()[-1:] in "(!,=:{[;&|?":
            i += 1
            in_class = False
            while i < n and (in_class or script[i] != "/"):
                if script[i] == "\\":
                    i += 1
                elif script[i] == "[":
                    in_class = True
                elif script[i] == "]":
                    in_class = False
                i += 1
            i += 1
            continue
        if c == "{":
            depth += 1
            if pending_body:
                skip_until_depth.append(depth)
                pending_body = False
        elif c == "}":
            if skip_until_depth and skip_until_depth[-1] == depth:
                skip_until_depth.pop()
            depth -= 1
        elif script.startswith("=>", i):
            j = i + 2
            while script[j].isspace():
                j += 1
            pending_body = script[j] == "{"
            i += 2
            continue
        elif script.startswith("get ", i) and script[i - 1] in " ,{" and script[i + 4 :].lstrip()[:1].isalpha():
            j = script.index("{", script.index(")", i))
            pending_body = True
            i = j
            continue
        elif script.startswith("function", i) and not (script[i - 1].isalnum() or script[i - 1] == "_"):
            j = script.index("{", script.index(")", i))
            pending_body = True
            i = j
            continue
        elif (
            not skip_until_depth
            and script.startswith("return", i)
            and not (script[i - 1].isalnum() or script[i - 1] == "_")
            and not (script[i + 6].isalnum() or script[i + 6] == "_")
        ):
            positions.append(i)
        i += 1
    return positions


def test_m_plus_script_has_one_return_and_it_is_the_terminal_one(monkeypatch):
    script = _m_plus_script(monkeypatch)
    found = _top_level_returns(script)
    assert len(found) == 1, [script[p : p + 40] for p in found]
    assert script[found[0] :].startswith("return { arm:")


def test_m_plus_script_validates_with_zero_errors(monkeypatch):
    findings = run_checks(_m_plus_script(monkeypatch))
    assert [f for f in findings if f.severity is Severity.ERROR] == []

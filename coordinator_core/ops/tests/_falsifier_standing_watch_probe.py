"""Baseline falsifier for the prime exit criterion: a standing watch wakes the
Group EM on peer idle, re-arms itself without operator memory, carries the
idle peer's named durable obligations on wake, and an empty-send tick must
name the declined obligation and reason rather than reporting a clean pass.

Run: python coordinator_core/ops/tests/_falsifier_standing_watch_probe.py
(in-process source inspection only -- no subprocess spawn per peer, no
git invocation; reads a fixed, named set of source files once).
"""
from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
HOOKS_DIR = REPO_ROOT / "coordinator_core" / "hooks"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def find_idle_trigger_hooks() -> list[str]:
    """Hook source files whose content couples an idle/Stop-class trigger to
    the Group EM entry op (`groupem.enter` / `group_em_enter` / `build_send_digest`).
    An empty list means: no hook file wires a peer-idle event to a Group EM wake.
    """
    hits: list[str] = []
    idle_pat = re.compile(r"idle|Stop|SubagentStop", re.IGNORECASE)
    entry_pat = re.compile(r"groupem\.enter|group_em_enter|build_send_digest")
    for path in sorted(HOOKS_DIR.glob("*.py")):
        if path.name.startswith("test_") or path.name.startswith("_"):
            continue
        text = _read(path)
        if idle_pat.search(text) and entry_pat.search(text):
            hits.append(str(path.relative_to(REPO_ROOT)))
    return hits


def find_rearm_mechanism() -> list[str]:
    """Source files anywhere in coordinator_core containing a self-re-registration
    ('re-arm') pattern applied to a Group-EM wake/watch. Empty means none found.
    """
    hits: list[str] = []
    pat = re.compile(r"re-?arm", re.IGNORECASE)
    for path in sorted((REPO_ROOT / "coordinator_core").rglob("*.py")):
        if "__pycache__" in path.parts or "/tests/" in path.as_posix() or path.name.startswith("test_"):
            continue
        text = _read(path)
        if pat.search(text) and re.search(r"group.?em|peer.*idle|watch", text, re.IGNORECASE):
            hits.append(str(path.relative_to(REPO_ROOT)))
    return hits


def digest_empty_case_names_declined_obligation() -> tuple[bool, str]:
    """Inspect group_em/send_pass.py's build_send_digest for a field that, on
    an empty send set, names which obligation was declined and why (rather
    than a bare empty list standing as a clean pass). Returns (found, evidence).
    """
    path = REPO_ROOT / "coordinator_core" / "group_em" / "send_pass.py"
    text = _read(path)
    m = re.search(r"def build_send_digest.*?(?=\ndef |\Z)", text, re.DOTALL)
    body = m.group(0) if m else ""
    declined_field = re.search(r"declined[_-]?obligation", body, re.IGNORECASE)
    return (declined_field is not None, body[:0] + (declined_field.group(0) if declined_field else "NOT FOUND"))


def main() -> None:
    print("=== find_idle_trigger_hooks ===")
    idle_hooks = find_idle_trigger_hooks()
    print(f"examined: {sorted(p.name for p in HOOKS_DIR.glob('*.py') if not p.name.startswith(('test_', '_')))}")
    print(f"hits (hook files coupling idle-trigger to Group EM entry): {idle_hooks}")

    print("\n=== find_rearm_mechanism ===")
    rearm_hits = find_rearm_mechanism()
    print(f"hits (files with a re-arm pattern tied to group-em/watch): {rearm_hits}")

    print("\n=== digest_empty_case_names_declined_obligation ===")
    found, evidence = digest_empty_case_names_declined_obligation()
    print(f"found: {found}  evidence: {evidence}")

    verdict_true = bool(idle_hooks) and bool(rearm_hits) and found
    print(f"\n=== VERDICT ===\nprime_exit_criterion_true: {verdict_true}")


if __name__ == "__main__":
    main()

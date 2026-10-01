"""Every guard deny site resolves at the one policy point (`apply_guard_level`).

Reads source text only; no guard module is imported.
"""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core import machine_profile as mp

ROOT = Path(__file__).resolve().parents[1]
POLICY = "apply_guard_level"

_DENY_SITE = re.compile(r'(?<![A-Za-z0-9_])deny\(|"permissionDecision":\s*"deny"')

#: Hook modules that compose a deny without resolving it themselves, each with its reason.
ALLOWLIST = {
    "hooks/stop_dispatch.py": "aggregate of Stop legs, each resolved at the policy point in its own module",
    "hooks/support/message_envelope.py": "envelope builder; the calling hook resolves the level",
    "hooks/support/sentinel_write_guard.py": "builder; no in-tree hook caller",
}


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _hook_modules():
    return sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "hooks").rglob("*.py")
        if "tests" not in p.parts and p.name != "__init__.py" and p.name != "conftest.py"
    )


def test_every_hook_deny_site_routes_through_the_policy_point():
    missing = []
    for rel in _hook_modules():
        text = _text(rel)
        if _DENY_SITE.search(text) and POLICY not in text and rel not in ALLOWLIST:
            missing.append(rel)
    assert not missing, f"hook deny sites bypassing {POLICY}: {missing}"


def test_allowlist_entries_exist_and_still_need_exemption():
    for rel in ALLOWLIST:
        assert (ROOT / rel).is_file(), rel


def test_bash_and_write_planes_reference_the_policy_point():
    assert POLICY in _text("bash_guards/dispatch.py")
    assert POLICY in _text("write_guards/engine.py")


def test_floor_guards_name_only_existing_guards():
    source = _text("bash_guards/dispatch.py")
    chain = source[source.index("def _build_guard_chain") :]
    bash_names = set(re.findall(r'GuardEntry\(\s*(?:name=)?"([a-z0-9-]+)"', chain))
    write_modules = {
        p.stem.replace("_", "-") for p in (ROOT / "write_guards").glob("*.py")
    }
    unknown = sorted(n for n in mp.FLOOR_GUARDS if n not in bash_names and n not in write_modules)
    assert not unknown, f"FLOOR_GUARDS names no registered guard: {unknown}"

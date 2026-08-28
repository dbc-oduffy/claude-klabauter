"""
coordinator_core.install.test_resolve_claude_klabauter_rename_retry — covers
``_resolve_claude_klabauter.resolve_target_path`` and the one call site ``exec_cli``
makes to it.

WHAT IS BEING PROTECTED. Publish applies an identity transform to filenames:
``check-claude-klabauter-doctor-sentinel.sh`` ships as
``check-claude-klabauter-doctor-sentinel.py``. The installed forwarder body
asks for the CLAUDE-KLABAUTER spelling verbatim, so on a box diverted to the published
engine the target is absent under the only name asked for and the run dies at
C13's fail-loud 127 — naming a root that does, in fact, carry the program
under another name. C2 emits the src->dst map at publish time
(``percolate.rewrite_basename.emit_published_name_map``); C3a reads it here.

WHY THIS IS NOT ``PUBLISHER_ONLY_TARGETS``, and why one test below is a
control rather than a case. The two classes share a symptom — a forwarder
that cannot run — and take INVERSE repairs. A publisher-only target exists
nowhere but the live tree, so pinning it live-tree-only is right. A renamed
target ships, works, and is merely misaddressed, so pinning it live-tree-only
would break it on every box WITHOUT a checkout: the population that has it
working today. ``test_publisher_only_target_never_reaches_the_rename_retry``
is the artifact holding that line — if the retry ever widens to cover the
publisher-only class, it goes red.

The retry is deliberately gated on ``RESOLUTION_RESOLVED_ENGINE``. A miss
under the live working tree is a genuinely broken checkout and must keep
failing loudly per C13; there is no map in a live tree to read anyway.

Module-loading convention (importlib.util.spec_from_file_location) matches
``test_resolve_claude_klabauter_exec_cli.py`` — the module under test is installed
standalone into a bare bin/ with only the stdlib importable, and lives under a
hyphenated directory name that precludes a normal ``import``.

Spec backlink: pln-the-currency-signal-exists-and-918d50 C3a.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "coordinator"
    / "lib"
    / "resolve-claude-klabauter"
    / "_resolve_claude_klabauter.py"
)

_spec = importlib.util.spec_from_file_location(
    "_resolve_claude_klabauter_under_test_rename_retry", _MODULE_PATH
)
assert _spec is not None and _spec.loader is not None
resolve_claude_klabauter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve_claude_klabauter)


#: The four live renames as of 2026-08-28. Three look like a
#: ``claude-klabauter`` -> ``claude-klabauter`` token rewrite; the fourth is derivable
#: from nothing, which is the whole reason the map is shipped rather than
#: inferred.
RENAMED_FOUR = {
    "check-claude-klabauter-doctor-sentinel.sh": "check-claude-klabauter-doctor-sentinel.py",
    "gen-claude-klabauter-root-pointer.py": "gen-claude-klabauter-root-pointer.py",
    "probe-cwd-example-retrieval-repo-relevance.py": "probe-cwd-example-retrieval-repo-relevance.py",
    "remove-claude-klabauter-precommit-hook.py": "remove-claude-klabauter-precommit-hook.py",
}


class _OSNameProxy:
    """Gives ``exec_cli`` an ``os.name`` of ``"nt"`` without mutating the real,
    process-global ``os`` module — patching the attribute directly corrupts
    ``pathlib``'s platform dispatch for the rest of the test process. Same
    device, same reason, as ``test_resolve_claude_klabauter_exec_cli.py``'s copy; every
    other attribute forwards to the real module."""

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "name", name)

    def __getattr__(self, attr):
        return getattr(os, attr)


def _bin_with_map(tmp_path: Path, mapping, *, create_published=True) -> Path:
    """A ``coordinator/bin``-shaped dir carrying *mapping* as publish's name
    map, with each mapped destination present on disk unless suppressed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if mapping is not None:
        (bin_dir / resolve_claude_klabauter.PUBLISHED_NAME_MAP_BASENAME).write_text(
            json.dumps(mapping), encoding="utf-8"
        )
    if create_published:
        for dst in (mapping or {}).values():
            if isinstance(dst, str) and dst:
                (bin_dir / dst).write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    return bin_dir


# ---------------------------------------------------------------------------
# resolve_target_path — the pure lookup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("asked,published", sorted(RENAMED_FOUR.items()))
def test_renamed_target_resolves_to_its_published_spelling(tmp_path, asked, published):
    bin_dir = _bin_with_map(tmp_path, RENAMED_FOUR)
    assert resolve_claude_klabauter.resolve_target_path(str(bin_dir), asked) == str(bin_dir) + "/" + published


def test_bare_name_finds_the_dotted_map_entry(tmp_path):
    """Forwarders installed before the ``.py`` rename ask for the bare name;
    the map is keyed by the suffixed one. One hop, matching ``exec_cli``'s own
    ``.py``-suffix probe — never a general fuzzy match."""
    bin_dir = _bin_with_map(tmp_path, RENAMED_FOUR)
    assert resolve_claude_klabauter.resolve_target_path(
        str(bin_dir), "gen-claude-klabauter-root-pointer"
    ) == str(bin_dir) + "/" + RENAMED_FOUR["gen-claude-klabauter-root-pointer.py"]


def test_absent_map_returns_the_original_path(tmp_path):
    """An absent map means "no mapping known", never an error — most mirrors
    carry none until a round has run since C2 landed."""
    bin_dir = _bin_with_map(tmp_path, None)
    asked = "check-claude-klabauter-doctor-sentinel.sh"
    assert resolve_claude_klabauter.resolve_target_path(str(bin_dir), asked) == str(bin_dir) + "/" + asked


@pytest.mark.parametrize(
    "raw",
    ["not json at all", "[]", '"a string"', "null", "123"],
    ids=["garbage", "list", "string", "null", "number"],
)
def test_unparseable_or_non_object_map_returns_the_original_path(tmp_path, raw):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / resolve_claude_klabauter.PUBLISHED_NAME_MAP_BASENAME).write_text(raw, encoding="utf-8")
    asked = "check-claude-klabauter-doctor-sentinel.sh"
    assert resolve_claude_klabauter.resolve_target_path(str(bin_dir), asked) == str(bin_dir) + "/" + asked


def test_name_absent_from_the_map_returns_the_original_path(tmp_path):
    bin_dir = _bin_with_map(tmp_path, RENAMED_FOUR)
    asked = "a-name-no-round-ever-renamed.py"
    assert resolve_claude_klabauter.resolve_target_path(str(bin_dir), asked) == str(bin_dir) + "/" + asked


def test_mapped_name_not_on_disk_returns_the_original_path(tmp_path):
    """A map entry pointing at a target this mirror does not carry is a stale
    map, and a stale map must not manufacture a path. Returning the original
    keeps ``exec_cli``'s 127 naming the name the operator actually asked for."""
    bin_dir = _bin_with_map(tmp_path, RENAMED_FOUR, create_published=False)
    asked = "check-claude-klabauter-doctor-sentinel.sh"
    assert resolve_claude_klabauter.resolve_target_path(str(bin_dir), asked) == str(bin_dir) + "/" + asked


@pytest.mark.parametrize("bad", [{"x.py": ""}, {"x.py": None}, {"x.py": 7}, {"x.py": ["y.py"]}])
def test_non_string_or_empty_destination_returns_the_original_path(tmp_path, bad):
    bin_dir = _bin_with_map(tmp_path, bad, create_published=False)
    assert resolve_claude_klabauter.resolve_target_path(str(bin_dir), "x.py") == str(bin_dir) + "/x.py"


# ---------------------------------------------------------------------------
# exec_cli — the one call site, and what gates it
# ---------------------------------------------------------------------------


def _exec_cli_target_path(monkeypatch, bin_dir: Path, target: str, resolution_class: str):
    """Run ``exec_cli`` through the Windows in-process leg with the target
    invocation stubbed, returning the ``target_path`` it composed (or the
    ``SystemExit`` code when it never got that far)."""
    seen = {}

    monkeypatch.setattr(
        resolve_claude_klabauter,
        "resolve_claude_klabauter_root_with_class",
        lambda: (str(bin_dir), resolution_class),
    )
    monkeypatch.setattr(resolve_claude_klabauter, "_resolve_publisher_root", lambda: str(bin_dir))
    monkeypatch.setattr(resolve_claude_klabauter, "_validate_bin_dir", lambda root: root)

    def _fake_run(target_path, argv, claude_klabauter_root):
        seen["target_path"] = target_path
        return 0

    monkeypatch.setattr(resolve_claude_klabauter, "_run_target_in_process", _fake_run)
    monkeypatch.setattr(resolve_claude_klabauter, "os", _OSNameProxy("nt"))

    with pytest.raises(SystemExit) as exc:
        resolve_claude_klabauter.exec_cli(target, [])
    seen["code"] = exc.value.code
    return seen


def test_exec_cli_retries_under_the_published_name_on_a_resolved_engine(tmp_path, monkeypatch):
    bin_dir = _bin_with_map(tmp_path, RENAMED_FOUR)
    asked = "probe-cwd-example-retrieval-repo-relevance.py"
    seen = _exec_cli_target_path(
        monkeypatch, bin_dir, asked, resolve_claude_klabauter.RESOLUTION_RESOLVED_ENGINE
    )
    assert seen["code"] == 0
    assert seen["target_path"] == str(bin_dir) + "/" + RENAMED_FOUR[asked]


def test_exec_cli_does_not_retry_under_the_live_working_tree(tmp_path, monkeypatch):
    """A miss in a live checkout is a broken checkout, not a rename — C13's
    fail-loud 127 stands, even with a map sitting right there."""
    bin_dir = _bin_with_map(tmp_path, RENAMED_FOUR)
    seen = _exec_cli_target_path(
        monkeypatch,
        bin_dir,
        "probe-cwd-example-retrieval-repo-relevance.py",
        resolve_claude_klabauter.RESOLUTION_LIVE_WORKING_TREE,
    )
    assert seen["code"] == 127
    assert "target_path" not in seen


def test_publisher_only_target_never_reaches_the_rename_retry(tmp_path, monkeypatch):
    """THE CONTROL. ``percolate-push.py`` is publisher-only: it exists nowhere
    but the live tree, and a resolver that "fixed" it too would convert a name
    bug into an outage on every box without a claude-klabauter checkout. It resolves via
    ``_resolve_publisher_root`` at ``RESOLUTION_LIVE_WORKING_TREE``, so the
    retry is gated off for it by construction — a map entry claiming otherwise
    must be ignored."""
    assert resolve_claude_klabauter._is_publisher_only_target("percolate-push.py")
    bin_dir = _bin_with_map(tmp_path, {"percolate-push.py": "percolate-push.py.published"})
    seen = _exec_cli_target_path(
        monkeypatch, bin_dir, "percolate-push.py", resolve_claude_klabauter.RESOLUTION_RESOLVED_ENGINE
    )
    assert seen["code"] == 127
    assert "target_path" not in seen


def test_exec_cli_present_target_never_opens_the_map(tmp_path, monkeypatch):
    """The ordinary case pays nothing: a target resolving under its own name
    must not read the map file at all."""
    bin_dir = _bin_with_map(tmp_path, RENAMED_FOUR)
    (bin_dir / "ordinary-cli.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    def _fail(*_a, **_k):
        raise AssertionError("resolve_target_path was consulted for a present target")

    monkeypatch.setattr(resolve_claude_klabauter, "resolve_target_path", _fail)
    seen = _exec_cli_target_path(
        monkeypatch, bin_dir, "ordinary-cli.py", resolve_claude_klabauter.RESOLUTION_RESOLVED_ENGINE
    )
    assert seen["code"] == 0
    assert seen["target_path"] == str(bin_dir) + "/ordinary-cli.py"

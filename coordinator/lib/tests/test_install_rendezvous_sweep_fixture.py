"""
Fixture-sweep tests for the C1 rendezvous relocation in
coordinator/templates/handoffs/continue-onboarding-and-installation.md.

Port of: coordinator/lib/tests/test-install-rendezvous-sweep.sh (T1-T6).

Does NOT reimplement the sweep logic — extracts the three ```bash code
blocks (install-leg sweep, orient-leg second sweep, supersedes resolution
sweep) VERBATIM out of the template and executes them (via /bin/bash, since
the blocks ARE bash prose embedded in a markdown template — not a new .sh
production artifact) against a sandboxed settings-home + legacy dir pair.
Same extraction technique as
coordinator_core/tests/test_install_chain_driven_leaf_seed_sweep.py's
``_extract_bash_block``, applied here to all three fences instead of one.

Spec backlink: DoE-claude:pln-relocate-the-install-baton-ren-05982a § C1
Port backlink: docs/plans/2026-08-13-grind-the-posix-exec-baseline-to-zero.md
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The blocks under test ARE bash prose embedded in a markdown template (the
# thing being proven is that template's own shell, not a Python-portable
# mechanism) — same precedent as
# coordinator_core/tests/test_install_chain_driven_leaf_seed_sweep.py, which
# skips identically when bash is unavailable (Windows without git-bash/WSL).
# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
    pytest.mark.skipif(
            shutil.which("bash") is None, reason="no bash on PATH — Windows without git-bash/WSL"
        ),
]

_TEMPLATE_REL = "coordinator/templates/handoffs/continue-onboarding-and-installation.md"


def _resolve_template_path() -> Path | None:
    """claude-klabauter does not vendor this template — it is coordinator-claude/
    DoE content (see this repo's own CLAUDE.md § What this repo is). Resolve
    it via the DoE-claude sibling root, same as
    coordinator_core/tests/test_install_chain_driven_leaf_seed_sweep.py; the
    bash oracle this replaces used a hardcoded relative path
    (SCRIPT_DIR/../../templates/...) that has never resolved inside this repo
    (verified: running it produces "FATAL: template not found") — that
    hardcoded assumption is not reproduced here."""
    from coordinator_core.doe_root_pointer import read_doe_root_pointer

    doe_root = read_doe_root_pointer()
    if not doe_root:
        return None
    candidate = Path(doe_root) / _TEMPLATE_REL
    return candidate if candidate.is_file() else None


def _extract_block(template_text: str, marker: str) -> str:
    lines = template_text.splitlines()
    on = False
    out = []
    for line in lines:
        if marker in line:
            on = True
        if on and line.strip() == "```":
            break
        if on:
            out.append(line[3:] if line.startswith("   ") else line)
    return "\n".join(out)


@pytest.fixture(scope="module")
def template_text():
    template_path = _resolve_template_path()
    if template_path is None:
        pytest.skip(
            "DoE-claude root not resolvable via coordinator_core.doe_root_pointer "
            "on this machine (or the template is missing there) — this template is "
            "not vendored in claude-klabauter; not a defect in claude-klabauter."
        )
    return template_path.read_text(encoding="utf-8")


@pytest.fixture
def sandbox(tmp_path):
    fake_claude_home = tmp_path / "fakehome"
    fake_settings_home = tmp_path / "settings-home"
    new_handoffs_dir = fake_settings_home / "state" / "handoffs"
    legacy_handoffs_dir = fake_claude_home / ".claude" / "state" / "handoffs"
    new_handoffs_dir.mkdir(parents=True)
    legacy_handoffs_dir.mkdir(parents=True)

    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir()
    resolver = fake_bin_dir / "coordinator-settings-home"
    resolver.write_text(
        f'#!/usr/bin/env bash\necho "{fake_settings_home}"\n', encoding="utf-8"
    )
    resolver.chmod(0o755)

    return {
        "claude_home": fake_claude_home,
        "settings_home": fake_settings_home,
        "new_handoffs": new_handoffs_dir,
        "legacy_handoffs": legacy_handoffs_dir,
        "bin_dir": fake_bin_dir,
    }


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def fixtures(sandbox):
    new = sandbox["new_handoffs"]
    legacy = sandbox["legacy_handoffs"]

    _write(
        new / "install-example-retrieval-repo.md",
        "---\nkind: spinoff\nrepo: example-retrieval-repo\ninstall_chain_order: 2\n---\n",
    )
    _write(
        new / "orient-example-retrieval-repo.md",
        '---\nkind: spinoff\ntitle: "Orient example-retrieval-repo"\n---\n',
    )
    _write(
        legacy / "install-cockpit.md",
        "---\nkind: spinoff\nrepo: cockpit\ninstall_chain_order: 6\n---\n",
    )
    _write(
        legacy / "orient-cockpit.md",
        '---\nkind: spinoff\ntitle: "Orient cockpit"\n---\n',
    )
    # Local name deliberately does NOT spell the repo codename: the publish
    # depersonalize pass rewrites that token to a HYPHENATED placeholder, and
    # a hyphenated identifier on an assignment's left-hand side parses as a
    # subtraction — `SyntaxError: cannot assign to expression`. String bodies
    # below still carry the codename and are still scrubbed, which is the
    # intended half of the transform. Caught by the `python-syntax-valid`
    # post_rsync guard, which failed both lib rows of the 2026-08-13 round.
    game_repo_frontmatter = "---\nkind: spinoff\nrepo: example-game-repo\ninstall_chain_order: 4\n---\n"
    _write(new / "install-example-game-repo.md", game_repo_frontmatter)
    _write(legacy / "install-example-game-repo.md", game_repo_frontmatter)
    _write(
        new / "orient-target.md",
        '---\nkind: spinoff\ntitle: "orientation for target"\n---\n',
    )
    _write(
        new / "orient-supersedes-target.md",
        '---\nkind: spinoff\nsupersedes: orient-target\ntitle: "orientation superseding the target"\n---\n',
    )
    return sandbox


def _run_block(block: str, sandbox: dict) -> str:
    env = {
        "PATH": f"{sandbox['bin_dir']}:/usr/bin:/bin",
        "CLAUDE_HOME": str(sandbox["claude_home"]),
        "HOME": str(sandbox["claude_home"]),
    }
    result = subprocess.run(
        ["/bin/bash", "-c", block],
        env=env,
        capture_output=True,
        text=True,
        creationflags=_CREATIONFLAGS,
    )
    return result.stdout + result.stderr


@pytest.fixture
def blocks(template_text):
    install_leg = _extract_block(
        template_text,
        "# Resolve the rendezvous through the seam, with an inline fallback for a cold tool shell",
    )
    orient_leg = _extract_block(template_text, "# Orient-leg discovery")
    supersedes = _extract_block(template_text, "# Generic over supersedes:<any-id>")
    for name, body in (
        ("install_leg", install_leg),
        ("orient_leg", orient_leg),
        ("supersedes", supersedes),
    ):
        assert body.strip(), (
            f"extraction of {name} produced an empty block — marker text drifted "
            "out of sync with the template"
        )
    return {"install_leg": install_leg, "orient_leg": orient_leg, "supersedes": supersedes}


def test_t1_install_leg_baton_at_settings_home_discovered(fixtures, blocks):
    out = _run_block(blocks["install_leg"], fixtures)
    assert "install-example-retrieval-repo.md" in out, out


def test_t2_orient_leg_baton_at_settings_home_discovered(fixtures, blocks):
    out = _run_block(blocks["orient_leg"], fixtures)
    assert "orient-example-retrieval-repo.md" in out, out


def test_t3_legacy_only_baton_discovered_via_fallback(fixtures, blocks):
    install_out = _run_block(blocks["install_leg"], fixtures)
    orient_out = _run_block(blocks["orient_leg"], fixtures)
    assert "install-cockpit.md" in install_out, install_out
    assert "orient-cockpit.md" in orient_out, orient_out


def test_t4_both_located_baton_deduplicated(fixtures, blocks):
    out = _run_block(blocks["install_leg"], fixtures)
    hits = out.count("install-example-game-repo.md")
    assert hits == 1, f"expected 1 hit, got {hits} in: {out}"


def test_t5_supersedes_baton_drops_target_from_orient_set(fixtures, blocks):
    supersedes_out = _run_block(blocks["supersedes"], fixtures)
    assert "orient-supersedes-target.md" in supersedes_out, supersedes_out

    superseding_file = None
    for line in supersedes_out.splitlines():
        if "orient-supersedes-target.md" in line:
            superseding_file = line.strip()
            break
    assert superseding_file, supersedes_out
    text = Path(superseding_file).read_text(encoding="utf-8")
    match = re.search(r"^supersedes:\s*(\S+)", text, re.MULTILINE)
    assert match and match.group(1) == "orient-target", text

    orient_out = _run_block(blocks["orient_leg"], fixtures)
    target = match.group(1)
    final_orient_set = "\n".join(
        line for line in orient_out.splitlines() if f"orient-{target[len('orient-'):]}.md" not in line
    )
    assert "orient-target.md" not in final_orient_set, final_orient_set


def test_t6a_frontmatter_scope_glob_repointed(template_text):
    assert re.search(
        r"^\s*-\s*\$\(coordinator-settings-home\)/state/handoffs/\*\*",
        template_text,
        re.MULTILINE,
    ), "expected a scope entry naming $(coordinator-settings-home)/state/handoffs/**"


def test_t6b_old_bare_pickup_path_fully_retired(template_text):
    assert "/pickup state/handoffs/continue-onboarding-and-installation.md" not in template_text


def test_t6c_pickup_path_mentions_repointed(template_text):
    hits = template_text.count(
        "/pickup $(coordinator-settings-home)/state/handoffs/continue-onboarding-and-installation.md"
    )
    assert hits >= 2, f"expected >=2 occurrences, got {hits}"


def test_t6d_every_legacy_reference_is_labeled_compat(template_text):
    lines = template_text.splitlines()
    legacy_line_nums = [i for i, line in enumerate(lines) if ".claude/state/handoffs" in line]
    assert legacy_line_nums, (
        "no legacy-form references found at all — suspicious (expected the 3 "
        "LEGACY_HANDOFFS= assignments + the compat-window prose sentence)"
    )
    for ln in legacy_line_nums:
        start = max(0, ln - 3)
        context = "\n".join(lines[start : ln + 1])
        assert re.search(r"legacy|compat", context, re.IGNORECASE), (
            f"line {ln + 1} has no legacy/compat label in its context: {context}"
        )

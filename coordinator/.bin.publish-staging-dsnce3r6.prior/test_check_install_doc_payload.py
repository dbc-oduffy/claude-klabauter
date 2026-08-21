"""test_check_install_doc_payload.py — pytest suite for check-install-doc-payload.py.

Exercises the module both as a library (check_tree, extract_script_paths) and
as a subprocess CLI, against scratch fixture trees built per-test — never
against dist/ or the real repo tree, per the executor brief's "build your
tests against fixture/scratch trees" constraint.

Coverage: the original P0 shape (missing scripts/setup.py FAILS), a complete
tree PASSES, a doc referencing a newly-added file FAILS until that file is
present, and the Windows-twin strictness case (a POSIX command's target
exists but its Windows-spelled twin's target does not — FAILS independently).

Spec backlink: state/audits/2026-08-05-klabauter-scrub-and-gate-both-silent.md
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(SCRIPT_DIR, "check-install-doc-payload.py")
PYTHON = sys.executable
_REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))

sys.path.insert(0, SCRIPT_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("check_install_doc_payload", HELPER)
gate = _ilu.module_from_spec(_spec)
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)  # type: ignore[union-attr]


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def run_helper(args, cwd=None):
    r = subprocess.run(
        [PYTHON, HELPER, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    return r.returncode, r.stdout, r.stderr


INSTALL_MD_TEMPLATE = """# Installing widget

```
python3 scripts/setup.py --i-am-agent      # agent path
python3 scripts/setup.py --check           # check only
```

Windows: `python scripts\\setup.py` with the same flags.

## Doctor probe

```
python3 bin/claude-klabauter-doctor-probe.py --step-zero   # POSIX
python bin\\claude-klabauter-doctor-probe.py --step-zero     # Windows
```

## Verify

```
python3 -c "import coordinator_core; print(coordinator_core.__name__)"
python3 -m pip install '.[test]'
python3 -m pytest coordinator_core
```
"""


def _write_full_tree(root):
    write(os.path.join(root, "INSTALL.md"), INSTALL_MD_TEMPLATE)
    write(os.path.join(root, "scripts", "setup.py"), "# setup\n")
    write(os.path.join(root, "bin", "claude-klabauter-doctor-probe.py"), "# probe\n")


def test_original_p0_missing_installer_fails(tmp_path):
    root = str(tmp_path)
    write(os.path.join(root, "INSTALL.md"), INSTALL_MD_TEMPLATE)
    write(os.path.join(root, "bin", "claude-klabauter-doctor-probe.py"), "# probe\n")
    # scripts/setup.py deliberately absent — this is the original P0 shape.

    findings = gate.check_tree(tmp_path)
    missing = {f.missing_path for f in findings}
    assert "scripts/setup.py" in missing
    assert "scripts\\setup.py" in missing

    rc, out, err = run_helper(["--tree", root])
    assert rc == 1
    assert "scripts/setup.py" in err or "scripts/setup.py" in out


def test_complete_tree_passes(tmp_path):
    root = str(tmp_path)
    _write_full_tree(root)

    findings = gate.check_tree(tmp_path)
    assert findings == []

    rc, out, err = run_helper(["--tree", root])
    assert rc == 0
    assert "clean" in out


def test_new_doc_reference_fails_until_file_present(tmp_path):
    root = str(tmp_path)
    _write_full_tree(root)

    write(
        os.path.join(root, "INSTALL.md"),
        INSTALL_MD_TEMPLATE
        + "\n## New step\n\n```\npython3 bin/new-migration-step.py\n```\n",
    )

    findings = gate.check_tree(tmp_path)
    missing = {f.missing_path for f in findings}
    assert "bin/new-migration-step.py" in missing

    write(os.path.join(root, "bin", "new-migration-step.py"), "# migration\n")
    findings_after = gate.check_tree(tmp_path)
    assert findings_after == []


def test_windows_twin_checked_independently(tmp_path):
    root = str(tmp_path)
    write(
        os.path.join(root, "INSTALL.md"),
        "```\n"
        "python3 scripts/setup.py --i-am-agent\n"
        "```\n\n"
        "Windows: `python scripts\\setup.py` with the same flags.\n",
    )
    # POSIX target exists; Windows-spelled twin's target ("scripts/setup.py"
    # after normalization) is the SAME underlying file — both should PASS
    # since they resolve to the identical published path.
    write(os.path.join(root, "scripts", "setup.py"), "# setup\n")

    findings = gate.check_tree(tmp_path)
    assert findings == []


def test_windows_only_broken_twin_fails_independently(tmp_path):
    root = str(tmp_path)
    write(
        os.path.join(root, "INSTALL.md"),
        "```\n"
        "python3 bin/probe.py --step-zero   # POSIX\n"
        "python bin\\probe.cmd --step-zero    # Windows\n"
        "```\n",
    )
    # POSIX .py target exists; the distinct Windows .cmd twin does NOT —
    # the gate must fail on the .cmd twin even though the .py sibling is fine.
    write(os.path.join(root, "bin", "probe.py"), "# probe\n")

    findings = gate.check_tree(tmp_path)
    missing = {f.missing_path for f in findings}
    assert missing == {"bin\\probe.cmd"}

    rc, out, err = run_helper(["--tree", root])
    assert rc == 1


def test_extract_script_paths_skips_module_and_inline_flags():
    assert gate.extract_script_paths("python3 -m pip install .") == []
    assert gate.extract_script_paths("python3 -m pytest coordinator_core") == []
    assert (
        gate.extract_script_paths(
            "python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'"
        )
        == []
    )
    assert gate.extract_script_paths("python3 -m coordinator_core.invoke <op> '<json>'") == []


def test_extract_script_paths_finds_real_targets():
    assert gate.extract_script_paths("python3 scripts/setup.py --i-am-agent") == [
        "scripts/setup.py"
    ]
    assert gate.extract_script_paths("python scripts\\setup.py") == ["scripts\\setup.py"]
    assert gate.extract_script_paths(
        "python3 .github/scripts/run-all-checks.py"
    ) == [".github/scripts/run-all-checks.py"]


def test_inline_backtick_span_is_scanned():
    root_text = "Windows: `python scripts\\setup.py` with the same flags.\n"
    spans = list(gate._iter_code_spans(root_text))
    assert any("scripts\\setup.py" in snippet for _, snippet, _ in spans)


def test_absolute_paths_are_not_flagged(tmp_path):
    root = str(tmp_path)
    write(
        os.path.join(root, "INSTALL.md"),
        "```\n/usr/bin/env python3 --version\n```\n",
    )
    findings = gate.check_tree(tmp_path)
    assert findings == []


def test_real_klabauter_toplevel_fixture_reproduces_p0():
    """Sanity-check against the real dist/klabauter-toplevel tree (read-only) —
    at time of writing it genuinely reproduces the original P0 (INSTALL.md
    references scripts/setup.py; no scripts/ directory ships). Read-only:
    never writes to this fixture."""
    repo_root = os.path.dirname(os.path.dirname(SCRIPT_DIR))
    fixture = os.path.join(repo_root, "dist", "klabauter-toplevel")
    if not os.path.isdir(fixture):
        pytest.skip("dist/klabauter-toplevel fixture not present on this checkout")
    if os.path.isdir(os.path.join(fixture, "scripts")):
        pytest.skip("dist/klabauter-toplevel has since been fixed — P0 no longer reproduces")

    from pathlib import Path

    findings = gate.check_tree(Path(fixture))
    missing = {f.missing_path for f in findings}
    assert "scripts/setup.py" in missing


# ---------------------------------------------------------------------------
# Link check -- a Markdown link is not a code span (the gate hole that let
# `docs/reference/install-chain-walk.md` ship unpublished-but-linked;
# state/audits/2026-08-05 identity-findings task).
# ---------------------------------------------------------------------------


def test_prose_link_to_missing_file_fails(tmp_path):
    root = str(tmp_path)
    write(
        os.path.join(root, "INSTALL.md"),
        "See [the install-chain-walk doc](docs/reference/install-chain-walk.md) "
        "for details.\n",
    )
    write(os.path.join(root, "docs", "reference", "other.md"), "# other\n")

    findings = gate.check_tree(root)
    missing = {(f.missing_path, f.kind) for f in findings}
    assert ("docs/reference/install-chain-walk.md", "link") in missing

    rc, out, err = run_helper(["--tree", root])
    assert rc == 1
    assert "docs/reference/install-chain-walk.md" in err or "docs/reference/install-chain-walk.md" in out


def test_link_to_present_file_passes(tmp_path):
    root = str(tmp_path)
    write(
        os.path.join(root, "INSTALL.md"),
        "See [the reference doc](docs/reference/present.md) for details.\n",
    )
    write(os.path.join(root, "docs", "reference", "present.md"), "# present\n")

    findings = gate.check_tree(root)
    assert findings == []


def test_link_to_directory_is_not_flagged(tmp_path):
    root = str(tmp_path)
    write(os.path.join(root, "INSTALL.md"), "See [the docs dir](docs/reference/) for more.\n")
    os.makedirs(os.path.join(root, "docs", "reference"), exist_ok=True)

    findings = gate.check_tree(root)
    assert findings == []


def test_external_url_and_anchor_and_mailto_not_flagged(tmp_path):
    root = str(tmp_path)
    write(
        os.path.join(root, "INSTALL.md"),
        "[external](https://example.com/x) "
        "[anchor](#section) "
        "[email](mailto:someone@example.com)\n",
    )

    findings = gate.check_tree(root)
    assert findings == []


def test_link_resolving_outside_tree_root_not_flagged(tmp_path):
    root = str(tmp_path / "sub")
    write(os.path.join(root, "INSTALL.md"), "[escape](../outside.md)\n")
    # "../outside.md" resolves outside `root` entirely -- out of scope for
    # this gate, not a finding.

    findings = gate.check_tree(root)
    assert findings == []


def test_link_with_fragment_checks_the_file_not_the_anchor(tmp_path):
    root = str(tmp_path)
    write(
        os.path.join(root, "INSTALL.md"),
        "[jump](docs/reference/present.md#some-section)\n",
    )
    write(os.path.join(root, "docs", "reference", "present.md"), "# present\n")

    findings = gate.check_tree(root)
    assert findings == []  # the file exists; the anchor's own validity is not checked


def test_leading_slash_link_target_not_flagged(tmp_path):
    root = str(tmp_path)
    write(os.path.join(root, "INSTALL.md"), "[abs-style](/docs/reference/missing.md)\n")
    # Leading "/" is ambiguous (GitHub repo-root convention vs filesystem-
    # absolute) -- deliberately not resolved or flagged (§ module docstring).

    findings = gate.check_tree(root)
    assert findings == []


def test_extract_relative_link_targets_filters_excluded_classes():
    text = (
        "[a](docs/x.md) [b](https://example.com) [c](#anchor) "
        "[d](/abs/path.md) [e](mailto:x@example.com)\n"
    )
    targets = [t for _, t, _ in gate.extract_relative_link_targets(text)]
    assert targets == ["docs/x.md"]


# ---------------------------------------------------------------------------
# Bare-basename resolution -- a doc naming a script by filename alone
# (coordinator-claude CONTRIBUTING.md's "the `validate-references.py` script
# checks this", where the file really ships at .github/scripts/) must resolve
# against the script-home directories the doc set itself demonstrates, without
# becoming permissive enough to let a retired filename resolve anywhere.
# ---------------------------------------------------------------------------


BARE_NAME_DOC = """# Contributing

Run validation locally: `python .github/scripts/run-all-checks.py`

- Cross-references must resolve — the `validate-references.py` script checks this
"""


def _write_bare_name_tree(root):
    write(os.path.join(root, "CONTRIBUTING.md"), BARE_NAME_DOC)
    write(os.path.join(root, ".github", "scripts", "run-all-checks.py"), "# checks\n")
    write(os.path.join(root, ".github", "scripts", "validate-references.py"), "# refs\n")


def test_bare_basename_resolves_in_demonstrated_script_home(tmp_path):
    root = str(tmp_path)
    _write_bare_name_tree(root)

    findings = gate.check_tree(tmp_path)
    assert findings == [], [f.render(tmp_path) for f in findings]

    rc, out, err = run_helper(["--tree", root])
    assert rc == 0, err
    assert "clean" in out


def test_bare_basename_absent_everywhere_still_fails(tmp_path):
    """The regression that would be worse than the false positive: a
    genuinely retired filename must still be caught."""
    root = str(tmp_path)
    _write_bare_name_tree(root)
    os.remove(os.path.join(root, ".github", "scripts", "validate-references.py"))

    findings = gate.check_tree(tmp_path)
    assert [f.missing_path for f in findings] == ["validate-references.py"]
    rendered = findings[0].render(tmp_path)
    assert ".github/scripts" in rendered

    rc, _out, err = run_helper(["--tree", root])
    assert rc == 1
    assert "validate-references.py" in err


def test_bare_basename_does_not_resolve_against_undemonstrated_directory(tmp_path):
    """A directory the doc set never references is not a script home, even
    though it exists and holds a same-named file — otherwise a retired
    pointer would resolve against an unrelated vendored copy."""
    root = str(tmp_path)
    _write_bare_name_tree(root)
    os.remove(os.path.join(root, ".github", "scripts", "validate-references.py"))
    write(os.path.join(root, "vendor", "third-party", "validate-references.py"), "# not ours\n")

    findings = gate.check_tree(tmp_path)
    assert [f.missing_path for f in findings] == ["validate-references.py"]


def test_path_qualified_reference_is_not_searched_across_script_homes(tmp_path):
    """Bare-name leniency must not leak into path-qualified references: a doc
    promising `scripts/setup.py` fails even though `setup.py` exists in a
    demonstrated home. This is the original P0's shape."""
    root = str(tmp_path)
    write(
        os.path.join(root, "INSTALL.md"),
        "```\npython3 bin/probe.py\npython3 scripts/setup.py\n```\n",
    )
    write(os.path.join(root, "bin", "probe.py"), "# probe\n")
    write(os.path.join(root, "bin", "setup.py"), "# decoy, wrong location\n")

    findings = gate.check_tree(tmp_path)
    assert [f.missing_path for f in findings] == ["scripts/setup.py"]


def test_broken_path_qualified_reference_does_not_seed_a_script_home(tmp_path):
    """A stale pointer cannot vouch for a directory it does not resolve in --
    otherwise a doc set could bootstrap a bogus home out of its own rot."""
    from pathlib import Path

    root = str(tmp_path)
    write(os.path.join(root, "INSTALL.md"), "```\npython3 tools/missing.py\n```\n")
    os.makedirs(os.path.join(root, "tools"), exist_ok=True)
    write(os.path.join(root, "tools", "other.py"), "# other\n")

    homes = gate.derive_script_home_dirs(Path(root), [Path(root, "INSTALL.md").read_text()])
    assert homes == [Path(root)]


def test_script_home_derived_from_one_doc_serves_a_bare_name_in_another(tmp_path):
    root = str(tmp_path)
    write(os.path.join(root, "INSTALL.md"), "```\npython3 .github/scripts/run-all-checks.py\n```\n")
    write(os.path.join(root, "CONTRIBUTING.md"), "See the `validate-references.py` script.\n")
    write(os.path.join(root, ".github", "scripts", "run-all-checks.py"), "# checks\n")
    write(os.path.join(root, ".github", "scripts", "validate-references.py"), "# refs\n")

    assert gate.check_tree(tmp_path) == []

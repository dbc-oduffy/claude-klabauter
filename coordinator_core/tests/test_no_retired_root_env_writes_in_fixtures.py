"""AST ratchet: no test fixture builds a child-process environment that PINS
the retired `CLAUDE_KLABAUTER_ROOT` engine-root var.

WHY A SECOND RATCHET, NOT A LEG ON THE FIRST. Its sibling
`test_no_direct_retired_root_env_reads.py` governs modules that READ the
retired name, and deliberately skips every test module. That is correct for
what it guards, and it is exactly the blind spot this file closes: a fixture
never reads the var — it writes it into a dict it then hands to
`subprocess.run(env=...)`. C14 (`fb1421af2`) stopped the engine-root gate
honouring the old name, so every such fixture silently stopped pinning
anything. The child fell back to ambient resolution, and the test either kept
passing while asserting nothing about its own stub, or failed with a message
("no coordinator source found") that reads as unrelated to the rename that
caused it. Twelve-plus files sat in that state for days, filed as
assumed-pre-existing red rather than as one cause with one fix —
state/bug-backlog/2026-08-25-fixture-env-dicts-still-pin-the-retired-claude-klabauter-live-root.yaml.

THE SHAPE, NOT THE SUBSTRING. Three write forms are matched, each requiring a
string constant EQUAL to a governed name (never a substring — the same
exact-equality property the sibling's docstring establishes, so prose,
docstrings and assertion messages that merely mention the name never trip):
  Form 1 -- a dict literal key: `env = {"CLAUDE_KLABAUTER_ROOT": root}`.
  Form 2 -- a subscript assignment: `env["CLAUDE_KLABAUTER_ROOT"] = root`.
  Form 3 -- a setenv call: `monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", root)`.

Form 3 was NOT matched when this ratchet first landed, on the theory that an
in-process env mutation is a different population from a dispatch env handed
to a child. That theory was wrong, and measurably so: renaming the form-3
pins turned 32 failures green across six files
(`test_first_run` 12, `test_forwarder_trust_guard` 7, `test_bin_family_refresh`
6, `test_backlog_grind_assemble` 5, `test_detect_onboarding_offer` 4 — none of
which the form-1/2 census had even surfaced). An in-process pin is just as
dead as a subprocess one; the process boundary was never the discriminator.

`delenv` is deliberately NOT matched, and never should be. Clearing the
retired name is HYGIENE — a fixture making sure an ambiently-inherited stale
value cannot reach the subject — and it stays correct for as long as any box
might still export the old name.

WHAT `EXCLUDED_PATHS` HOLDS is the real discriminator, and it is not a shape:
a module whose SUBJECT IS the retirement must keep saying the retired name.
Those tests assert that the old name is read only to report itself retired,
that it does not resolve, that a current name outranks it, or that a labelled
second rung still consults it. Renaming their pins would delete the coverage,
not fix it. Every entry below carries the one-line reason, the way the
sibling ratchet's own `EXCLUDED_PATHS` does.

Spec backlink: state/bug-backlog/2026-08-25-fixture-env-dicts-still-pin-the-retired-claude-klabauter-live-root.yaml § fix (2)
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from coordinator_core.tests.test_no_direct_retired_root_env_reads import ROOT_ENV_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Repo-relative, forward-slash paths this ratchet does not govern, each with
#: the one-line reason it must keep naming the retired var — see the module
#: docstring's "WHAT EXCLUDED_PATHS HOLDS".
EXCLUDED_PATHS: dict[str, str] = {
    "coordinator_core/tests/test_engine_root_census.py": (
        "asserts the retirement ADVISORY itself fires (and fires once) when "
        "the old name is set — the pin is the subject, not a resolution attempt."
    ),
    "coordinator_core/tests/test_engine_root_two_tier.py": (
        "the two-tier gate's own test: it sets the old name to prove "
        "coordinator_engine_root_with_class() does NOT answer with it."
    ),
    "coordinator_core/roadmap/tests/test_audit.py": (
        "pins both halves of the retirement contract — a set-but-retired old "
        "name must not resolve, and a fresh current name must outrank a stale "
        "old one inherited from an ancestor process."
    ),
    "scripts/test_setup.py": (
        "the installer's LABELLED second rung: setup.py runs against "
        "un-migrated boxes, precisely the population still exporting the old "
        "spelling, and is carved out in the sibling reads-ratchet for the same "
        "reason."
    ),
    "coordinator_core/bash_guards/tests/test_override_doc_display_portable.py": (
        "asserts the override-doc render is INVARIANT to this env var — the "
        "value is set in order to prove it changes nothing."
    ),
}

#: Directory names never walked: caches, and the scratch tree sessions write
#: throwaway copies of real modules into.
_SKIP_DIR_PARTS = frozenset({".git", "__pycache__", "subagent-share", "node_modules"})


def _test_modules() -> list[Path]:
    out: list[Path] = []
    for path in REPO_ROOT.rglob("test_*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if _SKIP_DIR_PARTS & set(path.relative_to(REPO_ROOT).parts):
            continue
        if rel in EXCLUDED_PATHS:
            continue
        out.append(path)
    return sorted(out)


def _env_dict_writes(path: Path, names: frozenset) -> list[tuple]:
    """(lineno, env_name, form) for every governed-name write in `path`.

    Unparseable files are skipped, not failed: this ratchet governs a source
    SHAPE, and a file that does not parse has a louder problem that its own
    collection will report.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    # Cheap substring prefilter before the expensive parse: this ratchet walks
    # every test module in the tree (~2300 files), and parsing all of them
    # costs seconds where reading them costs milliseconds. The substring is a
    # necessary condition for either matched form, never a sufficient one —
    # exact-equality on an ast.Constant below is still what decides.
    if not any(name in source for name in names):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    hits: list[tuple] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value in names:
                    hits.append((key.lineno, key.value, "dict-literal key"))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value in names
                ):
                    hits.append((target.lineno, target.slice.value, "subscript assignment"))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setenv"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in names
        ):
            # `setenv` only — `delenv` of a retired name is hygiene and stays.
            hits.append((node.lineno, node.args[0].value, "setenv call"))
        if isinstance(node, ast.Call):
            # Fourth shape: `dict(os.environ, CLAUDE_KLABAUTER_ROOT=root)`. A keyword
            # argument is neither a dict literal key nor a subscript, so the
            # three shapes above walked straight past it — and this is a
            # perfectly ordinary way to build a child env, not an exotic one.
            #
            # Found 2026-08-26 by the failure it was supposed to prevent:
            # `coordinator_core/ops/tests/test_doctor.py`'s `_run_doctor`
            # pinned the retired name this way, the pin stopped landing at C14,
            # and four tests in that file went red reporting a broken
            # sibling-resolution layer — a symptom that names neither the
            # variable nor the fixture. A ratchet is only worth its runtime if
            # it sees the shapes people actually write.
            for kw in node.keywords:
                if kw.arg in names:
                    hits.append((kw.value.lineno, kw.arg, "call keyword argument"))
    return sorted(hits)


def test_no_fixture_pins_a_retired_engine_root_var():
    """A fixture that sets a retired name into a child's environment pins
    nothing: the gate stopped reading it at C14. Set COORDINATOR_ENGINE_ROOT
    instead — and if the pinned root is a synthetic stub, give it a gate entry
    point too (coordinator_core.testing.fake_engine_root), or the candidate is
    rejected as "not a valid claude-klabauter checkout" rather than honoured.
    """
    offenders: list[str] = []
    for path in _test_modules():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, name, form in _env_dict_writes(path, ROOT_ENV_NAMES):
            offenders.append(f"{rel}:{lineno} — {form} sets retired {name!r}")

    assert not offenders, (
        "test fixture(s) build a child environment pinning a RETIRED engine-root "
        "var, which the engine-root gate no longer reads (C14, fb1421af2) — the "
        "pin is silently dead and the child resolves ambiently instead:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse COORDINATOR_ENGINE_ROOT. A synthetic stub root also needs a "
        "gate entry point — see coordinator_core.testing.fake_engine_root."
    )


def test_the_detector_actually_fires(tmp_path):
    """Liveness pin: an empty offender list is only meaningful if the detector
    can produce a non-empty one. All three matched forms, plus the two shapes
    that must NOT match (a `delenv` — hygiene — and the name inside prose)."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        textwrap.dedent(
            """\
            env = {"CLAUDE_KLABAUTER_ROOT": root}
            env["CLAUDE_KLABAUTER_ROOT"] = root
            monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", root)
            monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
            msg = "verify CLAUDE_KLABAUTER_ROOT is unset"
            """
        ),
        encoding="utf-8",
    )

    forms = [form for _, _, form in _env_dict_writes(probe, ROOT_ENV_NAMES)]

    assert forms == [
        "dict-literal key",
        "subscript assignment",
        "setenv call",
    ], forms


def test_the_detector_ignores_the_current_name(tmp_path):
    """The governed set is the RETIRED names only — a fixture pinning
    COORDINATOR_ENGINE_ROOT is the fix, not a new offence."""
    probe = tmp_path / "test_probe.py"
    probe.write_text('env = {"COORDINATOR_ENGINE_ROOT": root}\n', encoding="utf-8")

    assert _env_dict_writes(probe, ROOT_ENV_NAMES) == []

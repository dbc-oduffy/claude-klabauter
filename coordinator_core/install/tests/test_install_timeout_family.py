"""The `install` timeout family is a policy, not a pile of literals.

DR-349 § Carve-outs grants the install chain one exemption from this repo's
timeout budget and states the membership rule in the same breath: a carve-out
is named in the record or it does not exist. `install/timeouts.py` is where
the naming happens; these tests are what stop it from decaying back into the
scattered literals the hitlist census found (547 dials in `coordinator_core/`,
48% of them carrying one of three copied house numbers).

Three properties, each with a way to break it that this file forbids:

- The table self-describes — a constant cannot be added without appearing in
  `MEMBERS`, so a reader auditing the carve-out sees all of it.
- No member exceeds `FAMILY_CEILING_SECS` — the carve-out has an edge.
- No install-surface module carries its own over-a-minute literal — the
  failure mode is not one bad number, it is a number typed locally so nobody
  has to argue with the table.

Negative spec: this file does NOT check that the values are *right*. They are
carried over from the sites they replaced, and for the package-manager members
the right value is not knowable from here — `uv sync` is not ours. What it
checks is that they are *visible*, *bounded*, and *singular*.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

from coordinator_core.install import timeouts

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INSTALL_PKG = _REPO_ROOT / "coordinator_core" / "install"

#: Any bound above this, on the install surface, must derive from the family
#: rather than sit in the file as a number. Sub-minute bounds stay local: they
#: are wedged-child guards on fast checks we own, and pulling them into the
#: family would blur what the carve-out means.
_BARE_LITERAL_CEILING_SECS = 60

#: Modules that may import the family. Everything else in the tree is on the
#: session/commit/op paths DR-349 governs, and reaching in here would be a
#: carve-out taken rather than granted.
_QUARANTINE_PREFIXES = ("coordinator_core/install/", "scripts/")


def _install_surface_sources() -> Iterator[Path]:
    """Every production module of the install surface — the package's own
    modules and migrations, plus the three standalone scripts. Test modules
    are excluded: a test asserting on a timeout is describing behaviour, not
    setting policy."""
    for path in sorted(_INSTALL_PKG.rglob("*.py")):
        rel = path.relative_to(_INSTALL_PKG).as_posix()
        if rel.startswith("tests/") or Path(rel).name.startswith("test_"):
            continue
        yield path
    for name in ("setup.py", "windows_install_acceptance.py", "windows_install_probe.py"):
        candidate = _REPO_ROOT / "scripts" / name
        if candidate.is_file():
            yield candidate


def _numeric(node: ast.AST) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            return None
        return float(node.value)
    return None


def _over_ceiling_literals(path: Path) -> List[Tuple[int, str, float]]:
    """Every place *path* names a bound above `_BARE_LITERAL_CEILING_SECS` as
    a literal: a `timeout=<number>` keyword, or an assignment to a name that
    reads as a timeout. Returns (lineno, what, value)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: List[Tuple[int, str, float]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg != "timeout":
                    continue
                value = _numeric(kw.value)
                if value is not None and value > _BARE_LITERAL_CEILING_SECS:
                    found.append((kw.value.lineno, "timeout=", value))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name) or "TIMEOUT" not in target.id.upper():
                    continue
                value = _numeric(node.value)
                if value is not None and value > _BARE_LITERAL_CEILING_SECS:
                    found.append((node.lineno, target.id, value))
    return found


def test_every_family_member_appears_in_the_audit_table() -> None:
    """A constant that is not in `MEMBERS` is a carve-out nobody can audit."""
    declared = {
        name
        for name, value in vars(timeouts).items()
        if name.isupper() and isinstance(value, int) and not isinstance(value, bool)
    }
    declared -= {"FAMILY_CEILING_SECS"}
    assert declared == set(timeouts.MEMBERS), (
        "install/timeouts.py constants and MEMBERS have drifted. Missing from MEMBERS: "
        f"{sorted(declared - set(timeouts.MEMBERS))}; stale in MEMBERS: "
        f"{sorted(set(timeouts.MEMBERS) - declared)}."
    )
    for name, value in timeouts.MEMBERS.items():
        assert getattr(timeouts, name) == value, f"MEMBERS[{name!r}] disagrees with the constant."


def test_no_member_exceeds_the_family_ceiling() -> None:
    """The carve-out has an edge. A candidate above it argues against a
    number instead of into an open field."""
    over = {n: v for n, v in timeouts.MEMBERS.items() if v > timeouts.FAMILY_CEILING_SECS}
    assert not over, (
        f"install timeout family members above FAMILY_CEILING_SECS "
        f"({timeouts.FAMILY_CEILING_SECS}s): {over}. Raising the ceiling raises the whole "
        "carve-out — DR-349 § 2's deterrent, working as designed."
    )


@pytest.mark.parametrize("path", list(_install_surface_sources()), ids=lambda p: p.name)
def test_install_surface_carries_no_bare_over_a_minute_timeout(path: Path) -> None:
    """A local literal is how the family decays: the next author copies the
    number instead of joining the table. Derive from `install/timeouts.py`, or
    — if the bound is on code we own, which the family does not admit — name a
    constant under a minute and say what measurement backs it."""
    if path.name == "timeouts.py":
        pytest.skip("the family's own definitions are the table, not a copy of it")
    offenders = _over_ceiling_literals(path)
    assert not offenders, (
        f"{path.relative_to(_REPO_ROOT).as_posix()} carries bound(s) over "
        f"{_BARE_LITERAL_CEILING_SECS}s as literals: "
        + "; ".join(f"line {ln}: {what}{val:g}" for ln, what, val in offenders)
        + ". Import the matching member from coordinator_core.install.timeouts."
    )


def test_the_family_stays_quarantined_to_the_install_chain() -> None:
    """`install/timeouts.py` is not a timeout vocabulary for the tree. An
    importer outside the install chain is a carve-out taken rather than
    granted — DR-349's exact failure mode."""
    leaks: List[str] = []
    for path in sorted(_REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel.startswith((".git/", "state/", "archive/")):
            continue
        if rel.startswith(_QUARANTINE_PREFIXES):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "install.timeouts" in text or "install import timeouts" in text:
            leaks.append(rel)
    assert not leaks, (
        "the install timeout family is imported outside the install chain: "
        f"{leaks}. Those numbers exist because install-chain provisioning runs once per "
        "machine off every hot path; a caller elsewhere does not inherit that."
    )


def test_the_publish_round_is_not_sheltered_by_the_carve_out() -> None:
    """`first_run.provision_stamped_engine` bounds a `publish.py` round —
    claude-klabauter's OWN compute, measured at 80.8s of process time for a --dry-run
    preview alone. The hitlist § G11 Exception 1 names it as the thing the
    carve-out must not shelter, so it may not become a family member and may
    not drift back to a quarter-hour."""
    from coordinator_core.install import first_run

    budget = first_run._PUBLISH_ROUND_ADVISORY_BUDGET_SECS
    assert budget <= _BARE_LITERAL_CEILING_SECS, (
        f"the advisory publish-round budget is {budget}s. It bounds an ADVISORY install step, "
        "not publish.py's runtime — raising it re-buries the 80.8s instead of fixing it."
    )
    assert "_PUBLISH_ROUND_ADVISORY_BUDGET_SECS" not in timeouts.MEMBERS, (
        "the publish round is not install-chain provisioning: it bounds code this repo owns."
    )

"""
coordinator_core.install.tests.test_fleet_env_pin_advance — verifies AND
reports how far ``docs/install/fleet-env.lock``'s resolved pins have moved
past what each contributing repo's own manifest declares as its floor.

Purpose: the spike (docs/research/spike-verdicts/2026-08-16-one-resolved-
dependency-set-for-the-fleet.md) proved the fleet union RESOLVES; it never
claimed a repo's code runs against the resolved set
(docs/plans/2026-08-16-one-environment-for-the-fleet.md § Anti-scope:
"Do not treat 'it resolves on this box' as proof it installs or runs.").
This module is both the AC9 regression test and the AC12 per-repo
pin-advance report — the report is not a second, hand-maintained artifact;
it is emitted by ``render_pin_advance_report(build_pin_advance_report())``,
regenerable any time by re-running this file (``pytest -q -s
coordinator_core/install/tests/test_fleet_env_pin_advance.py::test_print_pin_advance_report``),
so it cannot silently drift from the lock the way a hand-copied doc would.

Spec backlink: docs/plans/2026-08-16-one-environment-for-the-fleet.md § C7, AC9, AC12
Contract: docs/reference/fleet-shared-environment-contract.md
Inputs read (read-only, never written): docs/install/fleet-env-requirements.in
(per-line ``  # <repo>:<manifest>`` provenance comments already carried by
``fleet_env_lock.render_requirements_in``), docs/install/fleet-env.lock.

Negative-spec:
    - Does NOT install anything and does NOT provision an environment —
      this is a pure parse-and-compare over two already-generated files.
    - Does NOT fix a breaking advance in any contributing repo — a
      major-version crossing or a declared-cap violation is reported and
      routed to its owning repo, never resolved here (plan Anti-scope).
    - Does NOT re-derive version comparison via ad-hoc regex arithmetic —
      uses ``packaging.requirements.Requirement`` /
      ``packaging.version.Version``, the same semantics ``uv lock`` itself
      resolved against.
    - Does NOT seed a new ``fleet-env-overrides.toml`` row and does NOT
      edit the lock, the requirements input, or the sources file — this
      module only reads them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import tomllib

from packaging.requirements import Requirement
from packaging.version import InvalidVersion, Version

from coordinator_core.install import fleet_env_lock

_REQUIREMENTS_IN_PATH: Path = fleet_env_lock._REQUIREMENTS_IN_PATH
_LOCK_PATH: Path = fleet_env_lock._LOCK_PATH

# Matches a rendered requirements-input line's trailing provenance comment,
# the exact shape `fleet_env_lock.render_requirements_in` emits
# (`"<spec>  # <repo>:<manifest>"`). Lines without this comment (the
# generated header, blank lines, the unattributed first-class-floors
# banner block) are not a single repo's declaration and are skipped.
_PROVENANCE_RE = re.compile(r"^(?P<spec>.+?)  # (?P<repo>[^:]+):(?P<manifest>.+)$")


@dataclass(frozen=True)
class PinAdvance:
    """One contributing repo's declared spec for one package, compared
    against the lock's resolved version for that package.

    ``crosses_major``/``declared_floor`` are the floor-vs-lock comparison
    (kept — sound machinery, demoted to secondary detail in the render).
    ``installed_version``/``movement``/``installed_crosses_major`` are the
    genuine-risk signal (state/audits/2026-08-16-fleet-venv-survey.md §
    "Declared-vs-installed divergences" and its pin-spread tables): what a
    repo actually runs today, compared against the same lock. A repo whose
    floor is merely an old permissive `>=` and which already runs the
    locked major shows ``movement="NO CHANGE"`` even though
    ``crosses_major`` (floor-based) is True — that gap is the whole point
    of C7's fix (docs/plans/2026-08-16-one-environment-for-the-fleet.md §
    C7: "the report currently overstates risk").
    """

    repo: str
    manifest: str
    package: str
    declared_spec: str
    declared_floor: Optional[str]
    locked_version: Optional[str]
    crosses_major: Optional[bool]
    violates_declared_cap: Optional[bool]
    installed_version: Optional[str]
    movement: str
    installed_crosses_major: Optional[bool]


def _normalize(name: str) -> str:
    """PEP 503 name normalization — case-insensitive, `.`/`_`/`-` folded
    to a single `-` — so `huggingface_hub` (requirements-input spelling)
    matches `huggingface-hub` (the lock's `[[package]] name` spelling)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _iter_declared_lines(requirements_in_path: Path = _REQUIREMENTS_IN_PATH):
    """Yield ``(spec, repo, manifest)`` for every provenance-carrying line
    in the generated requirements input."""
    for raw_line in requirements_in_path.read_text(encoding="utf-8").splitlines():
        match = _PROVENANCE_RE.match(raw_line)
        if not match:
            continue
        yield (
            match.group("spec").strip(),
            match.group("repo"),
            match.group("manifest"),
        )


def _load_locked_versions(lock_path: Path = _LOCK_PATH) -> Dict[str, str]:
    """Normalized package name -> first locked version found. torch and
    torchvision are the fleet's only platform-divergent double-pins (plan
    C3 disposition_detail) and their two locked entries share the same
    release, differing only in the `+cu130` local segment, so "first
    found" is stable for the major-version comparison this module makes."""
    with open(lock_path, "rb") as fh:
        data = tomllib.load(fh)
    versions: Dict[str, str] = {}
    for pkg in data.get("package", []):
        name = _normalize(str(pkg.get("name", "")))
        version = pkg.get("version")
        if name and version and name not in versions:
            versions[name] = str(version)
    return versions


# Hand-curated from state/audits/2026-08-16-fleet-venv-survey.md — the
# survey is prose, not a machine-readable per-repo/per-package table, so
# this is every (repo, package) pair the survey states an installed
# version for UNAMBIGUOUSLY (a single named tree per value, or a value
# the survey names as shared across explicitly listed trees). Pin
# spreads the survey gives as an unattributed list (e.g. "numpy 2.4.3 /
# 2.4.4 / 2.5.0" with no per-tree mapping) are deliberately NOT guessed
# into here — those packages report as UNKNOWN movement rather than a
# fabricated attribution. Extend only from new survey prose, never by
# inference.
#
# Coverage is intentionally thin, and deliberately not hand-counted here
# (a static count beside the dict it describes drifts from it — see the
# review that caught this comment stating "3 of ~90" against a dict that
# had grown past that). The authoritative, always-correct count is the
# report's own runtime-derived "Installed-version coverage: N/total" line
# in render_pin_advance_report, computed from this dict rather than
# maintained alongside it.
_INSTALLED_VERSIONS: Dict[tuple, str] = {
    # Keyed (repo, PEP-503-normalized package name) to match the
    # `name = _normalize(req.name)` lookup key in build_pin_advance_report.
    #
    # survey line 84 ("transformers 5.9.0 vs 5.3.0"), attributed via
    # line 32 (example-game-repo's transformers 5.3 uses is_offline_mode) and the
    # C7 dispatch brief naming example-retrieval-repo as the 5.9.0 side.
    ("example_retrieval_repo", "transformers"): "5.9.0",
    ("example_game_workbench_repo", "transformers"): "5.3.0",
    # survey line 272: "fifa has transformers 5.10.2 installed".
    ("example_league_data_repo", "transformers"): "5.10.2",
    # survey headline table (§ "Torch — one build, triplicated" /
    # Python-version-spread table): example-retrieval-repo and example-game-repo's main
    # .venv both carry 2.12.0+cu130.
    ("example_retrieval_repo", "torch"): "2.12.0+cu130",
    ("example_game_workbench_repo", "torch"): "2.12.0+cu130",
    # survey line 44 / bug-backlog 2026-08-16-the-fleet-lock-forces-
    # huggingface-hub-pa-0261f109b57b.yaml — already-tracked major
    # crossing + cap violation; reported here, not reopened.
    ("example_game_workbench_repo", "huggingface-hub"): "0.36.2",
    # survey § "Declared-vs-installed divergences" — example-retrieval-repo's own
    # stale declaration; already-tracked cap violation.
    ("example_retrieval_repo", "chromadb"): "1.4.1",
    # survey line 270: "scipy 1.17.1 (fifa, review-exp) vs 1.18.0
    # (market-intel)".
    ("experiments", "scipy"): "1.17.1",
    ("example_league_data_repo", "scipy"): "1.17.1",
    ("example_market_data_repo", "scipy"): "1.18.0",
}


def _classify_movement(
    installed: Optional[str], locked: Optional[str]
) -> str:
    """MOVES/NO CHANGE/UNKNOWN per plan C7's fix: compares what a repo
    actually runs (survey) against the lock, not the repo's (often
    years-old, permissive) declared floor against the lock — the
    floor-vs-lock comparison is what over-flagged in the first place."""
    if installed is None or locked is None:
        return "UNKNOWN"
    try:
        return "NO CHANGE" if Version(installed) == Version(locked) else "MOVES"
    except InvalidVersion:
        return "UNKNOWN"


def _declared_floor(specifier) -> Optional[str]:
    """The lowest bound a repo declared (`>=`, `==`, or `~=`), or ``None``
    for a bare unversioned spec (e.g. `pytest`, `ruff`) — nothing to
    compare a locked version against."""
    floors: List[Version] = []
    for spec in specifier:
        if spec.operator in (">=", "==", "~="):
            try:
                floors.append(Version(spec.version))
            except InvalidVersion:
                continue
    return str(min(floors)) if floors else None


def build_pin_advance_report(
    requirements_in_path: Path = _REQUIREMENTS_IN_PATH,
    lock_path: Path = _LOCK_PATH,
    installed_versions: Optional[Dict[tuple, str]] = None,
) -> List[PinAdvance]:
    """The ONLY generator for the pin-advance report's data — regenerate
    by calling this (or re-running this module) rather than hand-writing
    the report anywhere. Reads only; writes nothing.

    ``installed_versions`` defaults to the survey-sourced
    ``_INSTALLED_VERSIONS``; tests inject a synthetic mapping keyed
    ``(repo, normalized_package_name)``."""
    locked_versions = _load_locked_versions(lock_path)
    if installed_versions is None:
        installed_versions = _INSTALLED_VERSIONS
    entries: List[PinAdvance] = []
    for spec, repo, manifest in _iter_declared_lines(requirements_in_path):
        try:
            req = Requirement(spec)
        except Exception:
            continue
        name = _normalize(req.name)
        locked = locked_versions.get(name)
        floor = _declared_floor(req.specifier)

        crosses_major: Optional[bool] = None
        if floor is not None and locked is not None:
            try:
                crosses_major = Version(floor).major != Version(locked).major
            except InvalidVersion:
                crosses_major = None

        violates_cap: Optional[bool] = None
        if locked is not None:
            try:
                violates_cap = not req.specifier.contains(
                    Version(locked), prereleases=True
                )
            except InvalidVersion:
                violates_cap = None

        installed = installed_versions.get((repo, name))
        movement = _classify_movement(installed, locked)
        installed_crosses_major: Optional[bool] = None
        if installed is not None and locked is not None:
            try:
                installed_crosses_major = (
                    Version(installed).major != Version(locked).major
                )
            except InvalidVersion:
                installed_crosses_major = None

        entries.append(
            PinAdvance(
                repo=repo,
                manifest=manifest,
                package=req.name,
                declared_spec=spec,
                declared_floor=floor,
                locked_version=locked,
                crosses_major=crosses_major,
                violates_declared_cap=violates_cap,
                installed_version=installed,
                movement=movement,
                installed_crosses_major=installed_crosses_major,
            )
        )
    return entries


def render_pin_advance_report(entries: List[PinAdvance]) -> str:
    """Group by repo. Leads with the genuine risk set — packages that
    actually MOVE (installed != locked, per the venv survey) and
    declared-cap violations — because a floor-vs-lock major crossing
    alone is often just an old permissive `>=` and not a real change
    (plan C7: "the report currently overstates risk"). Floor-vs-lock
    detail is kept as a secondary section, not the lead.

    A repo with nothing flagged still gets a header, so "checked,
    clean" reads differently from "never checked"."""
    by_repo: Dict[str, List[PinAdvance]] = {}
    for entry in entries:
        by_repo.setdefault(entry.repo, []).append(entry)

    total = len(entries)
    unknown_count = sum(1 for e in entries if e.movement == "UNKNOWN")

    lines = [
        "# Fleet lock pin-advance report (AC12)",
        "",
        "Compares each contributing repo's declared floor "
        "(docs/install/fleet-env-requirements.in) against the resolved "
        "pin (docs/install/fleet-env.lock), and — where "
        "state/audits/2026-08-16-fleet-venv-survey.md records what a "
        "repo actually has installed — compares installed against "
        "locked too. That second comparison is the genuine-risk signal: "
        "a repo whose declared floor is merely an old permissive `>=` "
        "already running the locked major is NO CHANGE, not a flag, "
        "even though its floor and the lock differ by a major version.",
        "",
        f"Installed-version coverage: {total - unknown_count}/{total} "
        f"declared pins have survey-attributed installed data; "
        f"{unknown_count}/{total} are UNKNOWN (the survey has no "
        "installed-version record for that repo/package — not "
        "assumed safe or breaking).",
        "",
        "## Genuine risk — installed-version moves and declared-cap violations",
        "",
    ]
    for repo in sorted(by_repo):
        lines.append(f"### {repo}")
        repo_entries = by_repo[repo]
        moves = [e for e in repo_entries if e.movement == "MOVES"]
        caps = [e for e in repo_entries if e.violates_declared_cap]
        flagged_names = set()
        flagged_lines = []
        for entry in sorted(moves + caps, key=lambda e: e.package):
            if entry.package in flagged_names:
                continue
            flagged_names.add(entry.package)
            tags = []
            if entry.movement == "MOVES":
                tags.append(
                    "MOVES (major)" if entry.installed_crosses_major else "MOVES"
                )
            if entry.violates_declared_cap:
                tags.append("EXCEEDS DECLARED CAP")
            flagged_lines.append(
                f"- {entry.package}: installed {entry.installed_version} -> "
                f"locked {entry.locked_version} (declared {entry.declared_spec!r}) "
                f"[{', '.join(tags)}] ({entry.manifest})"
            )
        repo_unknown = sum(1 for e in repo_entries if e.movement == "UNKNOWN")
        if flagged_lines:
            lines.extend(flagged_lines)
        elif repo_entries and repo_unknown == len(repo_entries):
            # Wholly unverified must not read like "checked, clean" — the
            # leading bullet is the scannable part of the report, and
            # UNKNOWN is never assumed safe (see the report's own preamble).
            lines.append(
                "- no survey-attributed installed data for any declared "
                "pin in this repo (see floor-vs-lock section below)"
            )
        else:
            lines.append("- no installed-version moves, no declared-cap violations")
        repo_no_change = sum(1 for e in repo_entries if e.movement == "NO CHANGE")
        lines.append(
            f"  ({repo_no_change} no change, {repo_unknown} unknown, "
            f"{len(repo_entries)} declared pins total)"
        )
        lines.append("")

    lines.append(
        "## Secondary detail — declared floor vs locked pin (informational)"
    )
    lines.append(
        "A major crossing here does NOT imply the repo's runtime code "
        "moves — see the genuine-risk section above for that."
    )
    lines.append("")
    for repo in sorted(by_repo):
        lines.append(f"### {repo}")
        floor_flagged = [
            e for e in by_repo[repo] if e.crosses_major or e.violates_declared_cap
        ]
        if not floor_flagged:
            lines.append("- no major-version crossings, no declared-cap violations")
        for entry in sorted(floor_flagged, key=lambda e: e.package):
            tags = []
            if entry.crosses_major:
                tags.append("MAJOR VERSION ADVANCE (floor vs lock)")
            if entry.violates_declared_cap:
                tags.append("EXCEEDS DECLARED CAP")
            lines.append(
                f"- {entry.package}: declared {entry.declared_spec!r} "
                f"(floor {entry.declared_floor}) -> locked "
                f"{entry.locked_version} [{', '.join(tags)}] ({entry.manifest})"
            )
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Regression coverage (AC9) — a synthetic fixture proves the comparison
# logic itself detects a major-version crossing and a cap violation,
# independent of today's exact lock contents (which will change on every
# legitimate lock refresh and must not make this test flaky/deleted).
# --------------------------------------------------------------------------


def test_major_version_crossing_detected_via_synthetic_fixture(tmp_path):
    req_in = tmp_path / "fleet-env-requirements.in"
    req_in.write_text(
        "foo>=0.5,<2  # some_repo:pyproject.toml\n"
        "bar>=1.0,<2  # some_repo:pyproject.toml\n",
        encoding="utf-8",
    )
    lock = tmp_path / "fleet-env.lock"
    lock.write_text(
        '[[package]]\nname = "foo"\nversion = "1.2.0"\n\n'
        '[[package]]\nname = "bar"\nversion = "1.9.0"\n',
        encoding="utf-8",
    )
    entries = build_pin_advance_report(requirements_in_path=req_in, lock_path=lock)
    by_name = {e.package: e for e in entries}
    assert by_name["foo"].crosses_major is True
    assert by_name["bar"].crosses_major is False


def test_declared_cap_violation_detected_via_synthetic_fixture(tmp_path):
    req_in = tmp_path / "fleet-env-requirements.in"
    req_in.write_text(
        "capped>=0.20,<1.0  # some_repo:pyproject.toml\n"
        "uncapped>=0.20  # some_repo:pyproject.toml\n",
        encoding="utf-8",
    )
    lock = tmp_path / "fleet-env.lock"
    lock.write_text(
        '[[package]]\nname = "capped"\nversion = "1.5.0"\n\n'
        '[[package]]\nname = "uncapped"\nversion = "1.5.0"\n',
        encoding="utf-8",
    )
    entries = build_pin_advance_report(requirements_in_path=req_in, lock_path=lock)
    by_name = {e.package: e for e in entries}
    assert by_name["capped"].violates_declared_cap is True
    assert by_name["uncapped"].violates_declared_cap is False


def test_bare_unversioned_spec_yields_no_floor_and_no_crossing_verdict(tmp_path):
    req_in = tmp_path / "fleet-env-requirements.in"
    req_in.write_text("pytest  # some_repo:pyproject.toml\n", encoding="utf-8")
    lock = tmp_path / "fleet-env.lock"
    lock.write_text('[[package]]\nname = "pytest"\nversion = "9.1.0"\n', encoding="utf-8")

    entries = build_pin_advance_report(requirements_in_path=req_in, lock_path=lock)
    assert entries[0].declared_floor is None
    assert entries[0].crosses_major is None


def _synthetic_movement_fixture(tmp_path):
    """Shared three-package fixture for the MOVES/NO CHANGE/UNKNOWN
    classification tests: same declared floor (permissive `>=1.0`,
    unattributed by major-crossing alone) but three different installed
    states, so the assertions below isolate movement classification
    from the floor-vs-lock comparison."""
    req_in = tmp_path / "fleet-env-requirements.in"
    req_in.write_text(
        "moved>=1.0  # some_repo:pyproject.toml\n"
        "steady>=1.0  # some_repo:pyproject.toml\n"
        "unsurveyed>=1.0  # some_repo:pyproject.toml\n",
        encoding="utf-8",
    )
    lock = tmp_path / "fleet-env.lock"
    lock.write_text(
        '[[package]]\nname = "moved"\nversion = "2.5.2"\n\n'
        '[[package]]\nname = "steady"\nversion = "2.5.2"\n\n'
        '[[package]]\nname = "unsurveyed"\nversion = "2.5.2"\n',
        encoding="utf-8",
    )
    installed_versions = {
        ("some_repo", "moved"): "2.4.4",
        ("some_repo", "steady"): "2.5.2",
        # "unsurveyed" deliberately absent -> UNKNOWN.
    }
    return req_in, lock, installed_versions


def test_movement_classification_via_synthetic_fixture(tmp_path):
    """AC9 for the new MOVES/NO CHANGE/UNKNOWN classification (plan C7
    Change 1): an installed version that differs from the lock is MOVES,
    one that already matches the lock is NO CHANGE, and a package the
    survey never covers is UNKNOWN — never silently assumed safe."""
    req_in, lock, installed_versions = _synthetic_movement_fixture(tmp_path)
    entries = build_pin_advance_report(
        requirements_in_path=req_in,
        lock_path=lock,
        installed_versions=installed_versions,
    )
    by_name = {e.package: e for e in entries}
    assert by_name["moved"].movement == "MOVES"
    assert by_name["moved"].installed_crosses_major is False
    assert by_name["steady"].movement == "NO CHANGE"
    assert by_name["unsurveyed"].movement == "UNKNOWN"
    assert by_name["unsurveyed"].installed_version is None


def test_movement_classification_fails_if_no_change_treated_as_moves(tmp_path):
    """Negative control proving AC9's synthetic fixtures actually
    exercise the classifier: asserting the wrong verdict for "steady"
    (installed == locked) must fail, or the fixture above would pass
    vacuously regardless of what ``_classify_movement`` does."""
    req_in, lock, installed_versions = _synthetic_movement_fixture(tmp_path)
    entries = build_pin_advance_report(
        requirements_in_path=req_in,
        lock_path=lock,
        installed_versions=installed_versions,
    )
    by_name = {e.package: e for e in entries}
    try:
        assert by_name["steady"].movement == "MOVES"
    except AssertionError:
        pass
    else:
        raise AssertionError(
            "expected the wrong verdict to fail — classifier is not "
            "distinguishing NO CHANGE from MOVES"
        )


def test_movement_installed_major_crossing_detected_via_synthetic_fixture(tmp_path):
    req_in = tmp_path / "fleet-env-requirements.in"
    req_in.write_text("pkg>=1.0  # some_repo:pyproject.toml\n", encoding="utf-8")
    lock = tmp_path / "fleet-env.lock"
    lock.write_text('[[package]]\nname = "pkg"\nversion = "2.0.0"\n', encoding="utf-8")
    entries = build_pin_advance_report(
        requirements_in_path=req_in,
        lock_path=lock,
        installed_versions={("some_repo", "pkg"): "1.9.0"},
    )
    assert entries[0].movement == "MOVES"
    assert entries[0].installed_crosses_major is True


def test_render_report_leads_with_genuine_risk_section():
    """AC12: the render must put the installed-version MOVES/cap section
    ahead of the floor-vs-lock secondary section, so a reader sees the
    genuine risk first (plan C7 Change 1)."""
    entries = build_pin_advance_report()
    report = render_pin_advance_report(entries)
    genuine_idx = report.index("## Genuine risk")
    secondary_idx = report.index("## Secondary detail")
    assert genuine_idx < secondary_idx


# --------------------------------------------------------------------------
# AC12: the actual fleet report, over the real lock and requirements input.
# --------------------------------------------------------------------------


def test_pin_advance_report_covers_every_contributing_repo():
    entries = build_pin_advance_report()
    repos = {e.repo for e in entries}
    expected_repos = {
        "example_retrieval_repo",
        "claude_klabauter",
        "example_game_workbench_repo",
        "example_league_data_repo",
        "example_market_data_repo",
        "experiments",
        "example_retrieval_repo_ue_addon",
    }
    assert expected_repos <= repos


def test_huggingface_hub_flagged_as_major_advance_against_example_game_repo():
    """The one advance the C7 dispatch brief names explicitly as already
    known and contested: example-game-repo's `gpu_sidecar/requirements.txt` caps
    `huggingface_hub` at `<1.0`; the PM-ruled fleet floor
    (`FIRST_CLASS_FLOORS = ("huggingface_hub>=1.0",)`) is forced past that
    cap via C3's `override-dependencies`. AC12 requires this surfaced as a
    major-version advance against example-game-repo, not silently assumed."""
    entries = build_pin_advance_report()
    matches = [
        e
        for e in entries
        if _normalize(e.package) == "huggingface-hub"
        and e.repo == "example_game_workbench_repo"
    ]
    assert matches, "expected example-game-repo's own huggingface_hub declaration in the report"
    entry = matches[0]
    assert entry.crosses_major is True
    assert entry.violates_declared_cap is True
    # Also survey-attributed installed data (state/audits/2026-08-16-
    # fleet-venv-survey.md line 44): must MOVE, not merely differ by floor.
    assert entry.movement == "MOVES"
    assert entry.installed_crosses_major is True


def test_print_pin_advance_report(capsys):
    """Not an assertion-bearing regression test — running this node-id
    with `pytest -s` is how the AC12 report is regenerated/read. Kept as
    a real test (not a script) so it runs, and stays current, inside the
    same fast-tier collection as the rest of this file."""
    entries = build_pin_advance_report()
    report = render_pin_advance_report(entries)
    print(report)
    assert "example_game_workbench_repo" in report


# --------------------------------------------------------------------------
# AC12: durable report artifact — readable without running pytest. Written
# under state/audits/ (not docs/research/) because it is a derivative of
# the survey already living there (state/audits/2026-08-16-fleet-venv-
# survey.md), not session-authored research prose; keeping report and
# source survey in the same directory is the natural place to look for
# either. Regenerable ONLY via build_pin_advance_report()/
# render_pin_advance_report() — this function never composes the text by
# hand, it writes exactly what those two already-tested functions produce.
# --------------------------------------------------------------------------

_REPORT_ARTIFACT_PATH: Path = (
    Path(__file__).resolve().parents[3] / "state" / "audits" / "2026-08-16-fleet-env-pin-advance-report.md"
)

_REGENERATE_COMMAND = (
    "python -c \"from coordinator_core.install.tests."
    "test_fleet_env_pin_advance import _write_report_artifact; "
    "_write_report_artifact()\""
)


def _render_report_artifact() -> str:
    """The full committed-artifact text: a generated-file header (naming
    the regenerate command and a date) followed by the same report
    ``render_pin_advance_report`` produces — never hand-composed."""
    entries = build_pin_advance_report()
    body = render_pin_advance_report(entries)
    header = (
        "<!-- GENERATED by coordinator_core.install.tests."
        "test_fleet_env_pin_advance — do not hand-edit.\n"
        f"     Regenerate: {_REGENERATE_COMMAND}\n"
        "     Dated: 2026-08-16 -->\n\n"
    )
    return header + body


def _write_report_artifact() -> None:
    """Writes the durable AC12 artifact to disk. Not run automatically by
    the fast-tier collection (a test importing pytest must not mutate a
    committed file on every run) — invoke via `_REGENERATE_COMMAND` any
    time the lock, the requirements input, or `_INSTALLED_VERSIONS`
    changes. `test_pin_advance_report_artifact_is_current` below is the
    always-runs half: it fails, rather than silently drifts, if the
    committed artifact and the live render disagree."""
    _REPORT_ARTIFACT_PATH.write_text(
        _render_report_artifact(), encoding="utf-8", newline="\n"
    )


def test_pin_advance_report_artifact_is_current():
    """AC12: the committed report artifact must always match what
    `render_pin_advance_report` produces right now — never a second,
    hand-maintained copy that can drift from the lock. If this fails,
    regenerate via `_REGENERATE_COMMAND`."""
    assert _REPORT_ARTIFACT_PATH.exists(), (
        f"missing report artifact — regenerate via: {_REGENERATE_COMMAND}"
    )
    on_disk = _REPORT_ARTIFACT_PATH.read_text(encoding="utf-8")
    assert on_disk == _render_report_artifact()

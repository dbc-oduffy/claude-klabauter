"""Parity of the three own-sid session-shape scan legs against frozen pre-rewrite bodies.

The `_oracle_*` functions are copied from `coordinator/bin/wsc-session-disposition.py` at
385afa747b, renamed only by their `_oracle_` prefix (and the one internal call between them).
The matrix runs every case reachable under the session-start-floor invariant, each with a
`started_at` seeded and with none, and compares oracle to live on return value and diagnostics.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import re
import subprocess
import unittest.mock as mock
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "wsc_session_disposition_parity", str(_BIN_DIR / "wsc-session-disposition.py")
    )
    spec = importlib.util.spec_from_loader("wsc_session_disposition_parity", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


wsc = _load_cli_module()
wsc._bootstrap_engine_imports()

PrimaryScanResult = wsc.PrimaryScanResult
_bootstrap_engine_imports = wsc._bootstrap_engine_imports
resolve_claim_state = wsc.resolve_claim_state
rel_id = wsc.rel_id
_read_fm_field_local = wsc._read_fm_field_local
_resolve_memo_session_start_time = wsc._resolve_memo_session_start_time


# ---------------------------------------------------------------------------
# Frozen oracles: wsc-session-disposition.py @ 385afa747b
# ---------------------------------------------------------------------------


def _oracle_claim_states_naming(directory: Path, repo_root: Path, sid: str):
    """Yield `(file, ClaimState)` for each file under `directory` that the ledger or the 4 KiB
    frontmatter head could attribute to `sid`; every other file's resolved holder cannot be `sid`
    and is never read through `resolve_claim_state`."""
    import os

    from coordinator_core.claim_state import MIRROR_READ_CAP, handoff_claim_dir
    from coordinator_core.lifecycle import git_common_dir

    try:
        ledger_names = set(os.listdir(handoff_claim_dir(git_common_dir(repo_root), Path("x")).parent))
    except Exception:
        ledger_names = set()
    for dirpath, _dirs, names in os.walk(directory):
        for name in names:
            f = Path(dirpath, name)
            if name not in ledger_names:
                try:
                    with open(f, "r", encoding="utf-8", errors="replace") as fh:
                        if sid not in fh.read(MIRROR_READ_CAP):
                            continue
                except OSError:
                    continue
            yield f, resolve_claim_state(f, repo_root=repo_root)


def _oracle_primary_consumed_handoff_scan(repo_root: Path, sid: str) -> PrimaryScanResult:
    """This bin script's OWN detector set: every file under state/handoffs/
    whose claim state — resolved ledger-first via
    `coordinator_core.claim_state.resolve_claim_state`, frontmatter mirror
    as fallback (C1, plan `2026-08-07-claim-state-ledger-first-authoritative-
    read.md`) — names `sid` as holder, collected and sorted. Was previously
    an UNANCHORED regex `(claimed_by|consumed_by):\\s*<sid>` scanned directly
    over file text, which only ever saw the frontmatter mirror; a claim held
    only in the branch-independent ledger (mirror desynced by a branch
    switch — see `claim_state.py`'s module docstring for the incident this
    closes) was invisible to it. Deliberately NOT the same computation as
    branch_resolution.py's anchored `step0_evidence["consumed_handoff_paths"]`
    one layer down — this is a self-contained local scan, not a cross-package
    agreement.

    HOLDER-MISMATCH PARTITION (2026-08-26). A ledger-resolved holder match
    whose frontmatter mirror positively names a DIFFERENT session is NOT
    evidence that this session consumed that baton — it is the residue of an
    INCOMPLETE takeover. `pickup_assemble.apply` promotes the ledger claim to
    `apply` stage BEFORE any directive runs; a run that then halts at
    `claim_grant` (or fails) leaves that durable ledger claim behind with
    `d2`'s frontmatter stamp never landed. The mirror still names the peer
    who actually holds the baton, and the ledger names a session that only
    ever read it. Adopting that as `predecessor-consumed` caps a PEER's
    workstream: the live incident (2026-08-26, session
    2f937280 vs. baton holder 24d63e82 on
    `state/handoffs/2026-08-22-warm-engine-and-door-install-from-published-
    root.md`) resolved predecessor-consumed against a peer's in_flight baton
    and stood `d-complete-entry` down as "already exists for chain", so the
    session's own work got no completion entry.

    Narrower than "any ledger/mirror disagreement", and deliberately the
    complement of `ClaimState.disagreement`: the branch-switch-revert case
    this whole ledger-first read exists for leaves the mirror EMPTY
    (`mirror_holder is None`), which stays in `adopted` untouched. Only a
    mirror that positively names somebody else is partitioned out — see
    `ClaimState.disagreement`'s own docstring, which records that a
    same-slot holder mismatch is left for callers to detect via
    `ledger_holder`/`mirror_holder`. This is that caller.

    Negative-spec: does NOT release, demote, or otherwise mutate the
    conflicting ledger claim — read-only, like every other leg here. The
    conflict is reported to the caller so the disposition can decline to
    rest on it; reaping it belongs to the claim-hygiene surface, not to a
    session-shape detector."""
    _bootstrap_engine_imports()
    handoffs_dir = repo_root / "state" / "handoffs"
    if not handoffs_dir.is_dir():
        return PrimaryScanResult([], [])
    matches: list[Path] = []
    conflicts: list[Path] = []
    for f, state in _oracle_claim_states_naming(handoffs_dir, repo_root, sid):
        if state.holder != sid:
            continue
        if state.mirror_holder and state.mirror_holder != sid:
            conflicts.append(f)
        else:
            matches.append(f)
    matches.sort()
    conflicts.sort()
    # POSIX separators, not the native ones: this value becomes the ceremony's
    # `consumed_handoff` wire-id — it is written into frontmatter, exported as
    # WSC_CONSUMED_HANDOFF, and string-compared against handoff paths recorded
    # by other sessions and other platforms. A backslashed form compares
    # unequal to the same handoff recorded anywhere else. `repo_root / value`
    # still resolves correctly on Windows, which accepts forward slashes.
    return PrimaryScanResult(
        [f.relative_to(repo_root).as_posix() for f in matches],
        [f.relative_to(repo_root).as_posix() for f in conflicts],
    )


def _oracle_detector_a(repo_root: Path, sid: str) -> str | None:
    """An ARCHIVED handoff whose claim state — resolved ledger-first via
    `coordinator_core.claim_state.resolve_claim_state`, frontmatter mirror as
    fallback (C1) — names this session as holder AND carries a
    `predecessor:` field. Was previously this function's own private
    UNANCHORED regex `^(claimed_by|consumed_by): +<sid> *$` read directly off
    file text — replaced for the same reason as
    `primary_consumed_handoff_paths` above (a mirror-only read misses a claim
    that survives only in the branch-independent ledger)."""
    archive_dir = repo_root / "archive" / "handoffs"
    if not archive_dir.is_dir():
        return None
    predecessor_re = re.compile(r"^predecessor:", re.MULTILINE)
    candidates = []
    for f, state in _oracle_claim_states_naming(archive_dir, repo_root, sid):
        if state.holder == sid:
            candidates.append(f)
    for f in sorted(candidates):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        if predecessor_re.search(text):
            # POSIX separators — same wire-id reasoning as the primary scan's
            # own return above.
            return f.relative_to(repo_root).as_posix()
    return None


def _oracle_find_memo_predecessor(repo_root: Path, sid: str, diagnostics: list[str]) -> str | None:
    """Scans `cross-repo/inbox/*.md` and `cross-repo/archive/*.md` (flat,
    non-nested trees as of this writing — no `rglob` needed) for a memo
    whose frontmatter `picked_up_by` equals `sid` AND whose `picked_up_at`
    is at-or-before this session's own start time.

    The `picked_up_at` gate is load-bearing, not incidental:
    `picked_up_by` alone proves a CLAIM, not an ORIGIN — EMs claim memos
    mid-workstream routinely, and without this gate an incidental
    late-session memo claim would silently preempt a real resolution. If
    the session's start time cannot be resolved, the leg does NOT fire
    (fail closed) rather than skip the gate.

    Unreadable memos, memos with no frontmatter fence, and an unparseable
    `picked_up_at` are SKIPPED, never fatal. Several matches resolve
    deterministically: sort by repo-relative path, take the first.

    The filesystem scan for a `picked_up_by == sid` candidate runs FIRST,
    before the session-start git ladder is ever paid for — Review:
    coordinator-code-reviewer (Findings 2, 3). When zero candidates exist
    (the common case — this leg only matters for a session that opened on a
    memo), the git ladder never runs and no diagnostic is emitted, keeping
    `resolve_disposition`'s "byte-identical to pre-memo-leg HEAD when no
    memo matched" docstring claim true without exception. Only when at
    least one `picked_up_by` candidate is found does the ladder run to gate
    it against session start.

    Returns the matched memo's repo-relative path, or `None` if the leg did
    not fire or nothing matched."""
    candidates: list[tuple[Path, str, "datetime"]] = []
    corpus_root = repo_root / "state" / "cross-repo"
    if not corpus_root.is_dir():
        corpus_root = repo_root / "cross-repo"
    for dirname in ("inbox", "archive"):
        memo_dir = corpus_root / dirname
        if not memo_dir.is_dir():
            continue
        for f in memo_dir.glob("*.md"):
            if not f.is_file():
                continue
            try:
                if sid.encode("utf-8") not in f.read_bytes():
                    continue
            except OSError:
                continue
            picked_up_by = _read_fm_field_local(f, "picked_up_by")
            if not picked_up_by or picked_up_by != sid:
                continue
            picked_up_at_raw = _read_fm_field_local(f, "picked_up_at")
            if not picked_up_at_raw:
                continue
            try:
                picked_up_at = datetime.fromisoformat(picked_up_at_raw)
            except ValueError:
                continue
            if picked_up_at.tzinfo is None:
                picked_up_at = picked_up_at.replace(tzinfo=timezone.utc)
            candidates.append((f, picked_up_at_raw, picked_up_at))

    if not candidates:
        return None

    session_start = _resolve_memo_session_start_time(repo_root, sid)
    if session_start is None:
        diagnostics.append(
            "NOTE: memo-predecessor leg did not fire — could not resolve this session's start "
            "time (no .git/coordinator-sessions/<sid>/ claim dir under the git common dir, and "
            "no upstream/origin/main/master merge-base or first-commit fallback "
            "resolved), so a picked_up_by match cannot be gated to at-or-before session start; "
            "failing closed rather than risk an incidental late-session memo claim preempting a "
            "real resolution."
        )
        return None

    matches = [
        (f, picked_up_at_raw)
        for f, picked_up_at_raw, picked_up_at in candidates
        if picked_up_at <= session_start
    ]
    if not matches:
        return None
    matches.sort(key=lambda pair: pair[0])
    chosen, chosen_at_raw = matches[0]
    rel = rel_id(chosen, repo_root)
    diagnostics.append(
        f"NOTE: memo-predecessor candidate: {rel} (picked_up_by={sid}, "
        f"picked_up_at={chosen_at_raw} <= session-start {session_start.isoformat()})"
    )
    return rel


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

SID = "sid-parity-me"
OTHER = "sid-parity-peer"
T0 = 1_700_000_000.0  # session start (epoch seconds)
BELOW = T0 - 86_400.0
ABOVE = T0 + 3_600.0
START_ISO = datetime.fromtimestamp(T0, tz=timezone.utc).isoformat()
BEFORE_ISO = datetime.fromtimestamp(T0 - 600, tz=timezone.utc).isoformat()
AFTER_ISO = datetime.fromtimestamp(T0 + 600, tz=timezone.utc).isoformat()


def _fm(**fields: str) -> str:
    lines = "".join(f"{k}: {v}\n" for k, v in fields.items())
    return f"---\nkind: session-handoff\n{lines}---\nbody\n"


def _put(repo: Path, rel: str, text: str, mtime: float) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    os.utime(p, (mtime, mtime))
    return p


def _ledger(repo: Path, handoff_name: str, sid: str) -> None:
    d = repo / ".git" / "coordinator-sessions" / "handoff-claims" / handoff_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "session_id").write_text(sid, encoding="utf-8")
    (d / "claimed_at").write_text("2026-08-07T10:00:00Z", encoding="utf-8")  # dr084: ledger filename fixture write, not a frontmatter read


def _memo(repo: Path, name: str, by: str, at: str, mtime: float, folder: str = "inbox") -> None:
    _put(
        repo,
        f"state/cross-repo/{folder}/{name}",
        f"---\nkind: memo\npicked_up_at: '{at}'\npicked_up_by: {by}\n---\nbody\n",
        mtime,
    )


def _case_above_floor_mirror_hit(repo: Path) -> None:
    _put(repo, "state/handoffs/a-hit.md", _fm(claimed_by=SID), ABOVE)


def _case_legacy_consumed_by_hit(repo: Path) -> None:
    _put(repo, "state/handoffs/a-legacy.md", _fm(consumed_by=SID), ABOVE)


def _case_below_floor_non_hit(repo: Path) -> None:
    _put(repo, "state/handoffs/b-other.md", _fm(claimed_by=OTHER), BELOW)
    _put(repo, "state/handoffs/b-unclaimed.md", _fm(status="open"), BELOW)
    _put(repo, "archive/handoffs/b-arch-other.md", _fm(claimed_by=OTHER, predecessor="abc"), BELOW)


def _case_ledger_only_below_floor(repo: Path) -> None:
    _put(repo, "state/handoffs/c-ledger.md", _fm(status="open"), BELOW)
    _ledger(repo, "c-ledger.md", SID)
    _put(repo, "archive/handoffs/c-arch-ledger.md", _fm(predecessor="abc"), BELOW)
    _ledger(repo, "c-arch-ledger.md", SID)


def _case_mirror_names_other_sid(repo: Path) -> None:
    _put(repo, "state/handoffs/d-mirror-other.md", _fm(claimed_by=OTHER), ABOVE)
    _ledger(repo, "d-mirror-other.md", SID)


def _case_holder_mismatch_conflict_below_floor(repo: Path) -> None:
    _put(repo, "state/handoffs/e-conflict.md", _fm(claimed_by=OTHER), BELOW)
    _ledger(repo, "e-conflict.md", SID)
    _put(repo, "state/handoffs/e-mine.md", _fm(claimed_by=SID), ABOVE)


def _case_archive_with_predecessor(repo: Path) -> None:
    _put(repo, "archive/handoffs/f-with.md", _fm(claimed_by=SID, predecessor="abc"), ABOVE)


def _case_archive_without_predecessor(repo: Path) -> None:
    _put(repo, "archive/handoffs/g-without.md", _fm(claimed_by=SID), ABOVE)


def _case_archive_picks_first_sorted_with_predecessor(repo: Path) -> None:
    _put(repo, "archive/handoffs/h-1-without.md", _fm(claimed_by=SID), ABOVE)
    _put(repo, "archive/handoffs/h-2-with.md", _fm(claimed_by=SID, predecessor="abc"), ABOVE)
    _put(repo, "archive/handoffs/h-3-with.md", _fm(claimed_by=SID, predecessor="def"), ABOVE)


def _case_nested_handoff_dir(repo: Path) -> None:
    _put(repo, "state/handoffs/sub/i-nested.md", _fm(claimed_by=SID), ABOVE)


def _case_memo_picked_up_before_start(repo: Path) -> None:
    _memo(repo, "memo-j-before.md", SID, BEFORE_ISO, ABOVE)


def _case_memo_picked_up_after_start(repo: Path) -> None:
    _memo(repo, "memo-k-after.md", SID, AFTER_ISO, ABOVE)


def _case_memo_other_sid_and_archive_sorted(repo: Path) -> None:
    _memo(repo, "memo-l-other.md", OTHER, BEFORE_ISO, ABOVE)
    _memo(repo, "memo-l-b.md", SID, BEFORE_ISO, ABOVE, folder="archive")
    _memo(repo, "memo-l-a.md", SID, BEFORE_ISO, ABOVE, folder="inbox")


def _case_empty_corpus(repo: Path) -> None:
    return None


CASES = {
    "above_floor_mirror_hit": _case_above_floor_mirror_hit,
    "legacy_consumed_by_hit": _case_legacy_consumed_by_hit,
    "below_floor_non_hit": _case_below_floor_non_hit,
    "ledger_only_below_floor": _case_ledger_only_below_floor,
    "mirror_names_other_sid": _case_mirror_names_other_sid,
    "holder_mismatch_conflict_below_floor": _case_holder_mismatch_conflict_below_floor,
    "archive_with_predecessor": _case_archive_with_predecessor,
    "archive_without_predecessor": _case_archive_without_predecessor,
    "archive_first_sorted_with_predecessor": _case_archive_picks_first_sorted_with_predecessor,
    "nested_handoff_dir": _case_nested_handoff_dir,
    "memo_picked_up_before_start": _case_memo_picked_up_before_start,
    "memo_picked_up_after_start": _case_memo_picked_up_after_start,
    "memo_other_sid_and_folders_sorted": _case_memo_other_sid_and_archive_sorted,
    "empty_corpus": _case_empty_corpus,
}


def _init_repo(root: Path, *, seed_started_at: bool) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(root), "init", "-q"],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    sdir = root / ".git" / "coordinator-sessions" / SID
    sdir.mkdir(parents=True, exist_ok=True)
    if seed_started_at:
        (sdir / "started_at").write_text(START_ISO, encoding="utf-8")
    os.utime(sdir, (T0, T0))
    return root


def _observe(repo: Path, primary, detector_a, memo):
    from coordinator_core import claim_state as claim_state_mod

    diagnostics: list[str] = []
    with mock.patch.object(claim_state_mod, "cs_claim_holder_live", return_value=True):
        scan = primary(repo, SID)
        arch = detector_a(repo, SID)
        memo_hit = memo(repo, SID, diagnostics)
    return (tuple(scan.adopted), tuple(scan.mirror_conflicts), arch, memo_hit, tuple(diagnostics))


@pytest.mark.parametrize("seed_started_at", [True, False], ids=["started_at", "no_started_at"])
@pytest.mark.parametrize("case", sorted(CASES))
def test_live_legs_match_frozen_oracles(tmp_path, case, seed_started_at):
    repo = _init_repo(tmp_path / "repo", seed_started_at=seed_started_at)
    CASES[case](repo)
    oracle = _observe(
        repo, _oracle_primary_consumed_handoff_scan, _oracle_detector_a, _oracle_find_memo_predecessor
    )
    live = _observe(
        repo, wsc.primary_consumed_handoff_scan, wsc.detector_a, wsc.find_memo_predecessor
    )
    assert live == oracle


def test_matrix_is_not_vacuous(tmp_path):
    hit = _init_repo(tmp_path / "hit", seed_started_at=True)
    for c in (
        "above_floor_mirror_hit",
        "archive_with_predecessor",
        "memo_picked_up_before_start",
        "holder_mismatch_conflict_below_floor",
    ):
        CASES[c](hit)
    adopted, conflicts, arch, memo_hit, diagnostics = _observe(
        hit, wsc.primary_consumed_handoff_scan, wsc.detector_a, wsc.find_memo_predecessor
    )
    assert adopted == ("state/handoffs/a-hit.md", "state/handoffs/e-mine.md")
    assert conflicts == ("state/handoffs/e-conflict.md",)
    assert arch == "archive/handoffs/f-with.md"
    assert memo_hit == "state/cross-repo/inbox/memo-j-before.md"
    assert diagnostics


def test_below_floor_non_ledger_file_naming_sid_is_outside_the_floor_contract(tmp_path):
    repo = _init_repo(tmp_path / "repo", seed_started_at=True)
    _put(repo, "state/handoffs/z-unreachable.md", _fm(claimed_by=SID), BELOW)
    oracle = _oracle_primary_consumed_handoff_scan(repo, SID)
    assert oracle.adopted == ["state/handoffs/z-unreachable.md"]

    from coordinator_core import claim_state as claim_state_mod

    with mock.patch.object(claim_state_mod, "cs_claim_holder_live", return_value=True):
        live = wsc.primary_consumed_handoff_scan(repo, SID)
    assert live.adopted == []

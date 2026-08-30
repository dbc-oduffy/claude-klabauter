"""Tests for `coordinator_core.ops.git_maintenance` — tier mapping, lock
arbitration, defer semantics (C4), the prune leg (C5), and the 500ms brightline
(C8).

TIER STRUCTURE. The mapping and defer tests are ordinary fast-tier work: they
drive a throwaway repo and assert argv shapes and refusals, spawning at most a
couple of cheap git calls each. The brightline measurement is NOT — it clones a
repo, seeds induced churn, and runs three maintenance tiers twice each. On a box
carrying ~50 concurrent sessions that is cadence-tier work and is marked
accordingly, per docs/reference/test-tiers.md.

NEVER AGAINST THIS REPO'S LIVE `.git`. Every test here builds its own repo under
tmp_path. ~50 peer sessions share this worktree, and an index-lock, gc, or prune
experiment against it is a defect regardless of what it proves.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import git_maintenance as gm
from coordinator_core.win_portability import no_console_creationflags


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    # The tiers' measured budgets hold only with prefetch off; set it here for
    # the same reason git_perf_config sets it per-repo at install.
    _git(repo, "config", "maintenance.prefetch.enabled", "false")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "seed")
    return repo


# --------------------------------------------------------------------------
# C4: tier -> invocation mapping
# --------------------------------------------------------------------------


def test_hourly_is_a_task_list_never_the_hourly_schedule():
    """`--schedule=hourly` drags `prefetch` in: a `git fetch` against every
    remote plus two credential round-trips, 293.8ms and 11.2 procs, in an
    otherwise network-free design."""
    assert gm._TIER_ARGV["hourly"] == ("maintenance", "run", "--task=commit-graph")
    assert "--schedule=hourly" not in gm._TIER_ARGV["hourly"]


def test_daily_and_weekly_are_schedules():
    assert gm._TIER_ARGV["daily"] == ("maintenance", "run", "--schedule=daily")
    assert gm._TIER_ARGV["weekly"] == ("maintenance", "run", "--schedule=weekly")


def test_unknown_tier_is_an_error_not_a_default(tmp_path):
    result = gm.run_tier(_init_repo(tmp_path), "monthly")

    assert result.rc == 1
    assert "unknown tier" in result.errors[0]


def test_main_rejects_a_missing_tier(tmp_path, monkeypatch):
    monkeypatch.chdir(_init_repo(tmp_path))
    assert gm.main([]) == 2
    assert gm.main(["hourly", "daily"]) == 2
    assert gm.main(["monthly"]) == 2


def test_hourly_tier_runs_and_reports(tmp_path):
    result = gm.run_tier(_init_repo(tmp_path), "hourly")

    assert result.ran is True
    assert result.deferred is None
    assert result.rc == 0


# --------------------------------------------------------------------------
# C4: lock arbitration and defer semantics
# --------------------------------------------------------------------------


def test_defers_on_a_held_index_lock(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")

    result = gm.run_tier(repo, "weekly")

    assert result.deferred is not None
    assert "index.lock" in result.deferred
    assert result.ran is False
    assert result.pruned is False
    assert result.rc == 0


@pytest.mark.parametrize(
    "marker,word",
    [
        ("REBASE_HEAD", "rebase"),
        ("MERGE_HEAD", "merge"),
        ("BISECT_LOG", "bisect"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
    ],
)
def test_defers_mid_operation(tmp_path, marker, word):
    repo = _init_repo(tmp_path)
    (repo / ".git" / marker).write_text("", encoding="utf-8")

    result = gm.run_tier(repo, "daily")

    assert result.deferred is not None
    assert word in result.deferred
    assert result.ran is False


def test_defer_predicate_takes_posix_shaped_paths(tmp_path):
    """MULTI-OS. The failure that motivates this predicate is a Windows
    file-sharing artifact, but a held index is equally real on POSIX and the
    production path carries no `os.name` branch — only path tests and plain
    git."""
    repo = _init_repo(tmp_path)
    git_dir = Path(str(repo / ".git").replace("\\", "/"))
    (git_dir / "index.lock").write_text("", encoding="utf-8")

    assert gm.defer_reason(repo, git_dir) is not None
    (git_dir / "index.lock").unlink()
    assert gm.defer_reason(repo, git_dir) is None


def test_no_os_name_branch_in_the_production_path():
    """MULTI-OS, asserted on the AST rather than on the text: no branch
    spelled as a bare `os.name`/`sys.platform`/`platform.system` attribute
    access reads the host identity. A text grep cannot make this claim — the
    module's own docstrings say the words.

    SCOPE: the walk only flags `ast.Attribute` nodes whose `.value` is a bare
    `ast.Name` -- an aliased import (`import os as o; o.name`) or indirect
    access via `getattr` would not be caught. That is out of scope for this
    regression guard, which exists to catch a future reintroduction spelled
    the ordinary way, not every conceivable evasion of it."""
    import ast

    tree = ast.parse(Path(gm.__file__).read_text(encoding="utf-8"))
    reads = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            reads.add(f"{node.value.id}.{node.attr}")
    assert "os.name" not in reads
    assert "sys.platform" not in reads
    assert "platform.system" not in reads


def test_defer_is_reported_distinctly_from_a_run(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")

    gm._report(gm.run_tier(repo, "hourly"))

    err = capsys.readouterr().err
    assert "deferred" in err


# --------------------------------------------------------------------------
# C5: the prune leg
# --------------------------------------------------------------------------


def _plant_unreachable_blob(repo: Path, body: str, age_days: int) -> str:
    """Write a loose object nothing references, aged `age_days` old.

    THE BACKDATING IS LOAD-BEARING, not test convenience. `git prune --expire`
    keys off the loose object's MTIME, so an object written a second ago
    survives any expiry window longer than a second. A test that planted a
    FRESH blob and asserted it was pruned could only pass under
    `--expire=now`, which is not the expiry this module ships — see
    `test_an_unreachable_object_inside_the_expiry_window_survives`.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=body,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    sha = proc.stdout.strip()
    loose = repo / ".git" / "objects" / sha[:2] / sha[2:]
    old = time.time() - age_days * 86400
    os.utime(loose, (old, old))
    return sha


def test_weekly_prunes_an_unreachable_object(tmp_path):
    """Under maintenance.strategy=incremental nothing ever drops an
    unreachable object — loose-objects packs them, incremental-repack repacks
    them. Without this leg, gc.auto=0 means unreachable history forever."""
    repo = _init_repo(tmp_path)
    sha = _plant_unreachable_blob(repo, "unreachable\n", age_days=30)
    assert _git(repo, "cat-file", "-e", sha).returncode == 0

    result = gm.run_tier(repo, "weekly")

    assert result.pruned is True
    assert _git(repo, "cat-file", "-e", sha).returncode != 0, "unreachable blob survived"


def test_an_unreachable_object_inside_the_expiry_window_survives(tmp_path):
    """The other half of the contract, asserted so the expiry is a real window
    and not decoration: 2.weeks.ago means RECENT garbage stays. Anything
    wanting it gone immediately is choosing to drop objects a concurrent peer
    may still be writing against — on this box, ~50 of them."""
    repo = _init_repo(tmp_path)
    sha = _plant_unreachable_blob(repo, "recent\n", age_days=1)

    gm.run_tier(repo, "weekly")

    assert _git(repo, "cat-file", "-e", sha).returncode == 0


def test_prune_runs_before_the_maintenance_run_not_after(tmp_path, monkeypatch):
    """ORDERING IS LOAD-BEARING. `--schedule=weekly` includes `loose-objects`,
    which PACKS loose objects, unreachable ones included. `git prune` only ever
    removes LOOSE objects — once garbage is packed, dropping it needs a full
    `repack -A -d` or a `gc`, and `gc` is a kill-bar item here. A prune
    sequenced AFTER the maintenance run therefore reaps nothing, exits 0, and
    lets unreachable history accumulate forever behind a green tier.

    Found by the plan's falsifier: conjunct 4 read FAIL with the legs in the
    other order, with everything else identical.
    """
    invoked = _recorded_argv(tmp_path, "weekly", monkeypatch)
    verbs = [args[0] for args in invoked]

    assert "prune" in verbs and "maintenance" in verbs
    assert verbs.index("prune") < verbs.index("maintenance"), verbs


def test_prune_expiry_is_gits_own_default():
    """2.weeks.ago is git's gc.pruneExpire default; diverging needs an argument
    about how fast unreachable objects accumulate here, and none exists."""
    assert gm._PRUNE_EXPIRE == "2.weeks.ago"


def _recorded_argv(tmp_path, tier, monkeypatch):
    """Every git argument vector one tier invocation actually issues.

    Behavioural, not a source grep: what matters is what the module RUNS, and
    the module's own docstrings legitimately contain every forbidden word.
    """
    repo = _init_repo(tmp_path)
    invoked = []
    real = gm._git

    def recording(r, *args, **kwargs):
        invoked.append(args)
        return real(r, *args, **kwargs)

    monkeypatch.setattr(gm, "_git", recording)
    gm.run_tier(repo, tier)
    return invoked


def test_gc_is_never_used_as_the_reaper(tmp_path, monkeypatch):
    """`git prune --expire=2.weeks.ago` is 40.6ms/1 proc; `git gc
    --prune=2.weeks.ago` is 10,068.8ms/9 procs — twenty times over the bar, a
    kill-bar item on sight. Git's own docs add an independent reason: enabling
    `gc` beside `loose-objects` is contraindicated."""
    for tier in gm.TIERS:
        invoked = _recorded_argv(tmp_path / tier, tier, monkeypatch)
        assert not any(args[:1] == ("gc",) for args in invoked), invoked
        assert not any(a.startswith("--prune=") for args in invoked for a in args), invoked


def test_maintenance_register_and_start_are_never_invoked(tmp_path, monkeypatch):
    """`register` writes this repo's path into the operator's GLOBAL config,
    read only by `git for-each-repo`, run only by the scheduler this design
    never runs. `start` writes that scheduler entry itself."""
    forbidden = {"register", "unregister", "start", "stop"}
    for tier in gm.TIERS:
        invoked = _recorded_argv(tmp_path / tier, tier, monkeypatch)
        assert not any(set(args) & forbidden for args in invoked), invoked


def test_no_global_config_is_ever_written(tmp_path, monkeypatch):
    """A same-host global write can strand a peer machine mid-sync, and
    `git_perf_config`'s negative spec forbids this module that surface."""
    for tier in gm.TIERS:
        invoked = _recorded_argv(tmp_path / tier, tier, monkeypatch)
        assert not any("--global" in args for args in invoked), invoked


def test_weekly_reaps_an_orphan_pack_through_the_ceremony(tmp_path, monkeypatch):
    """The call site the prime falsifier's orphan-pack conjunct reaches when it
    says 'the ceremony runs'."""
    repo = _init_repo(tmp_path)
    pack_dir = repo / ".git" / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    orphan = pack_dir / ".tmp-1234-pack-abcdef.pack"
    orphan.write_bytes(b"x" * 64)
    import os
    old = 1
    os.utime(orphan, (old, old))

    result = gm.run_tier(repo, "weekly")

    assert result.orphan_packs_reaped == 1
    assert not orphan.exists()


def test_stamp_does_not_fire_when_the_prune_leg_fails(tmp_path, monkeypatch):
    """The liveness stamp is the ONLY surface distinguishing 'never ran' from
    'ran and is fine' -- it must not also claim 'fine' for a tier whose prune
    leg errored, even though the maintenance-run leg after it succeeded."""
    repo = _init_repo(tmp_path)
    real = gm._git

    def failing_prune(r, *args, **kwargs):
        if args and args[0] == "prune":
            return subprocess.CompletedProcess(args, returncode=128, stdout="", stderr="boom")
        return real(r, *args, **kwargs)

    monkeypatch.setattr(gm, "_git", failing_prune)
    stamped = []
    monkeypatch.setattr(gm, "_stamp", lambda repo: stamped.append(repo))

    result = gm.run_tier(repo, "weekly")

    assert result.errors, "prune failure should be recorded"
    assert result.ran is True, "the maintenance-run leg still ran"
    assert stamped == [], "stamp must not fire when the tier had an error"


def test_stamp_fires_on_a_clean_weekly_run(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    stamped = []
    monkeypatch.setattr(gm, "_stamp", lambda repo: stamped.append(repo))

    result = gm.run_tier(repo, "weekly")

    assert not result.errors
    assert stamped == [repo]


def test_non_weekly_tiers_do_not_prune_or_sweep(tmp_path):
    repo = _init_repo(tmp_path)
    for tier in ("hourly", "daily"):
        result = gm.run_tier(repo, tier)
        assert result.pruned is False, tier
        assert result.orphan_packs_reaped == 0, tier


# --------------------------------------------------------------------------
# C8: the brightline. Cadence tier -- clones and runs real maintenance.
# --------------------------------------------------------------------------


@pytest.mark.cadence
def test_every_tier_is_under_the_500ms_brightline(tmp_path):
    """THE ACCEPTANCE ORACLE. Process time and spawn count, never wall clock.

    Measured against INDUCED CHURN, not a freshly cloned probe with nothing to
    do: a guard that reproduces the same optimistic low-churn condition as its
    standing measurement never exercises the expensive state it exists to
    catch. Each tier is measured twice — first-invocation and warm.

    THE 500ms VERDICT RESTS ON n=1 TODAY. Every figure in the plan's § Problem
    came from one spike run; this test's first green run is the SECOND data
    point, not a confirmation of the first.

    Over the bar is a kill-bar item: rebuild under the bar, do not shave spawns.
    """
    from coordinator_core.benchmarks.process_time import batched_process_time_ms

    repo = _init_repo(tmp_path)
    # INDUCED CHURN. A freshly cloned probe with nothing to do reproduces the
    # same optimistic condition the spike measured under; commit-graph,
    # loose-objects and incremental-repack need real work to be measured
    # honestly. The spike's figures are lower bounds for exactly this reason.
    for i in range(300):
        (repo / "a.txt").write_text(f"line {i}\n", encoding="utf-8")
        _git(repo, "add", "a.txt")
        _git(repo, "commit", "-qm", f"churn {i}")

    # Each tier's full git-leg cost: the maintenance run, plus weekly's prune.
    # The orphan-pack sweep is deliberately NOT here — its ~2s is a SLEEP, not
    # process time, and this bar is process time. That distinction is the
    # reason the sweep is weekly-tier work and not commit-path work; wall
    # clock on this box measures peer load, never our cost.
    legs = {
        tier: [["git", "-C", str(repo), *gm._TIER_ARGV[tier]]] for tier in gm.TIERS
    }
    legs["weekly"].append(["git", "-C", str(repo), "prune", f"--expire={gm._PRUNE_EXPIRE}"])

    breaches = []
    measured = {}
    for tier, cmds in legs.items():
        for pass_name in ("cold", "warm"):
            total_ms = 0.0
            total_procs = 0.0
            for cmd in cmds:
                sample = batched_process_time_ms(cmd, k=3, cwd=str(repo))
                total_ms += sample["process_time_ms"]
                total_procs += sample["procs_per_call"]
            measured[(tier, pass_name)] = (total_ms, total_procs)
            if total_ms >= 500:
                breaches.append(f"{tier} ({pass_name}): {total_ms:.1f}ms / {total_procs:.1f} procs")

    assert not breaches, (
        "over the 500ms brightline -- a KILL-BAR item: rebuild under the bar, "
        f"do not shave spawns off it. {breaches}. All samples: {measured}"
    )


# --------------------------------------------------------------------------
# C3 (folded into C4): sweep_orphan_packs -- reaper (a)
#
# The three gates are tested independently because each one alone is
# insufficient: "no .idx" is true of a live repack for its whole duration, age
# alone is true of a stalled-but-still-open handle, and stability alone is true
# of a completed pack. The last test is the COST assertion -- N orphans must
# cost ONE stability window, not N -- which is the reason this function does
# not simply call reap_stale_locks.stale_and_stable per file.
#
# No sleeps are paid: every test drives the on_wait seam.
# --------------------------------------------------------------------------


def _pack_dir(tmp_path: Path) -> Path:
    d = tmp_path / "objects" / "pack"
    d.mkdir(parents=True)
    return d


def _write(path: Path, body: bytes = b"x" * 64, age_sec: int = 0) -> Path:
    path.write_bytes(body)
    if age_sec:
        old = time.time() - age_sec
        os.utime(path, (old, old))
    return path


def test_aged_stable_orphan_with_no_idx_is_reaped(tmp_path):
    d = _pack_dir(tmp_path)
    orphan = _write(d / ".tmp-1234-pack-abcdef.pack", age_sec=3600)

    result = gm.sweep_orphan_packs(d, on_wait=lambda: None)

    assert result.reaped == [orphan]
    assert not orphan.exists()
    assert result.failed == []


def test_fresh_orphan_is_left_alone(tmp_path):
    """A repack that started seconds ago has no `.idx` yet either."""
    d = _pack_dir(tmp_path)
    fresh = _write(d / ".tmp-1234-pack-abcdef.pack", age_sec=0)

    result = gm.sweep_orphan_packs(d, on_wait=lambda: None)

    assert result.reaped == []
    assert result.skipped == 1
    assert fresh.exists()


def test_unstable_orphan_is_left_alone(tmp_path):
    """Past the age floor but still growing -- a slow repack, not garbage."""
    d = _pack_dir(tmp_path)
    growing = _write(d / ".tmp-1234-pack-abcdef.pack", age_sec=3600)

    def mutate():
        growing.write_bytes(b"y" * 4096)

    result = gm.sweep_orphan_packs(d, on_wait=mutate)

    assert result.reaped == []
    assert result.skipped == 1
    assert growing.exists()


def test_pack_with_an_idx_sibling_is_never_reaped(tmp_path):
    """A completed pack, whatever its age -- gate 1 rejects it at collection
    time so it never even enters the stability window."""
    d = _pack_dir(tmp_path)
    complete = _write(d / ".tmp-1234-pack-abcdef.pack", age_sec=86400)
    _write(d / ".tmp-1234-pack-abcdef.idx", age_sec=86400)

    result = gm.sweep_orphan_packs(d, on_wait=lambda: None)

    assert result.reaped == []
    assert result.skipped == 0  # rejected before the age gate, not "skipped"
    assert complete.exists()


def test_n_orphans_cost_exactly_one_stability_window(tmp_path):
    """THE COST ASSERTION. A per-file window would be N waits; at the orphan
    counts observed in this worktree that is roughly ten serial 2s waits."""
    d = _pack_dir(tmp_path)
    orphans = [
        _write(d / f".tmp-{i}-pack-abcdef{i}.pack", age_sec=3600) for i in range(8)
    ]

    waits = []
    result = gm.sweep_orphan_packs(d, on_wait=lambda: waits.append(1))

    assert len(waits) == 1, f"{len(waits)} windows for {len(orphans)} orphans"
    assert sorted(result.reaped) == sorted(orphans)


def test_no_candidates_pays_no_window_at_all(tmp_path):
    d = _pack_dir(tmp_path)
    _write(d / "pack-abcdef.pack", age_sec=3600)  # an ordinary pack, not a tmp body

    waits = []
    result = gm.sweep_orphan_packs(d, on_wait=lambda: waits.append(1))

    assert waits == []
    assert result.reaped == []


def test_all_candidates_fresh_pays_no_window(tmp_path):
    """The age gate runs BEFORE the window, so a set of fresh candidates costs
    no wait at all."""
    d = _pack_dir(tmp_path)
    _write(d / ".tmp-1-pack-a.pack", age_sec=0)
    _write(d / ".tmp-2-pack-b.pack", age_sec=0)

    waits = []
    result = gm.sweep_orphan_packs(d, on_wait=lambda: waits.append(1))

    assert waits == []
    assert result.skipped == 2


def test_missing_pack_dir_is_not_an_error(tmp_path):
    result = gm.sweep_orphan_packs(tmp_path / "objects" / "pack")

    assert result.reaped == []
    assert result.failed == []


def test_age_floor_default_is_ten_times_the_plausible_repack(tmp_path):
    """600s, defended against this plan's own observation that a repack of a
    412 MB pack legitimately runs for minutes."""
    assert gm._ORPHAN_PACK_AGE_SEC == 600


def test_env_knob_overrides_the_age_floor(tmp_path, monkeypatch):
    d = _pack_dir(tmp_path)
    orphan = _write(d / ".tmp-1234-pack-abcdef.pack", age_sec=30)

    monkeypatch.setenv("COORDINATOR_ORPHAN_PACK_REAP_AGE_SEC", "10")
    result = gm.sweep_orphan_packs(d, on_wait=lambda: None)

    assert result.reaped == [orphan]

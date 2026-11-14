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

from coordinator_core.benchmarks.maintenance_tier_budget import (
    SCRATCH_ROOT as _FALSIFIER_SCRATCH_ROOT,
    check_maintenance_tier_budget,
)
from coordinator_core.git.run import GitResult
from coordinator_core.ops import git_maintenance as gm
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _unpaused(monkeypatch):
    """These tests pin the tiers themselves; the paused behaviour is pinned in test_pack_deletion_pause."""
    from coordinator_core.git import pack_deletion_pause

    monkeypatch.setattr(pack_deletion_pause, "PACK_DELETION_PAUSED", False)


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


def test_daily_and_weekly_are_task_lists_never_a_schedule():
    """`maintenance.strategy=incremental` makes git's schedules cumulative, so
    `--schedule=weekly` re-runs every task `--schedule=daily` ran that same
    day. Measured, that put the weekly tier at 515.6ms/9 procs against the
    500ms brightline, of which 328ms was daily's work done twice. Weekly
    names the one task the daily set does not carry.

    Daily is ALSO no longer a schedule (R9 / P153-C25): `--schedule=daily`
    included `loose-objects`, which packs unreachable loose objects out from
    under weekly's plain `git prune` before it ever gets a chance to see them
    loose (state/bug-backlog/2026-08-30-the-daily-tier-packs-unreachable-
    objects-309a82437447.yaml). See `test_daily_then_weekly_reaps_an_object_daily_already_packed`
    for the falsifier this row asked for.

    These two equality assertions ARE the cumulative-schedule-trap pin: no
    value of `_TIER_ARGV["weekly"]` (or a hypothetical fourth tier above
    daily) can regain `--schedule=weekly`/`--schedule=monthly` without
    breaking one of them first, which is why there is no separate loop test
    for that keyword ban here."""
    assert gm._TIER_ARGV["daily"] == ("maintenance", "run", "--task=commit-graph")
    assert gm._MIDX_WRITE_ARGV == ("multi-pack-index", "write")
    assert "--schedule=daily" not in gm._TIER_ARGV["daily"]
    assert "loose-objects" not in " ".join(gm._TIER_ARGV["daily"])
    assert "incremental-repack" not in " ".join(gm._TIER_ARGV["daily"])
    assert gm._TIER_ARGV["weekly"] == ("maintenance", "run", "--task=pack-refs")


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

    result = gm.run_tier(repo, "weekly")

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

    gm._report(gm.run_tier(repo, "weekly"))

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
    """ORDERING IS LOAD-BEARING ACROSS TIERS, NOT WITHIN THIS ONE. Weekly's own
    `--task=pack-refs` packs nothing, so this order is harmless-but-no-longer-
    required in isolation. It is still asserted because the DAILY tier's
    `loose-objects` task PACKS loose objects, unreachable ones included, and
    `git prune` only ever removes LOOSE objects — once garbage is packed,
    dropping it needs a full `repack -A -d` or a `gc`, and `gc` is a kill-bar
    item here. On a day both tiers fire, a weekly prune sequenced after that
    run reaps nothing, exits 0, and lets unreachable history accumulate
    forever behind a green tier. Filed as
    state/bug-backlog/2026-08-30-the-daily-tier-packs-unreachable-objects-309a82437447.yaml.

    Found by the plan's falsifier: conjunct 4 read FAIL with the legs in the
    other order, with everything else identical.
    """
    invoked = _recorded_argv(tmp_path, "weekly", monkeypatch)
    verbs = [args[0] for args in invoked]

    assert "prune" in verbs and "maintenance" in verbs
    assert verbs.index("prune") < verbs.index("maintenance"), verbs


def _churn(repo: Path, n: int) -> None:
    """N throwaway commits, mirroring the spike's own `repro_steps` -- a
    fresh repo's `loose-objects` task has its own internal count threshold
    and is a no-op below it, so a repro without churn silently proves
    nothing about the trap it claims to reproduce."""
    for i in range(n):
        (repo / f"churn-{i}.txt").write_text(f"{i}\n", encoding="utf-8")
        _git(repo, "add", f"churn-{i}.txt")
        _git(repo, "commit", "-qm", f"churn {i}")


def test_daily_then_weekly_reaps_an_object_daily_already_packed(tmp_path):
    """THE R9 FALSIFIER (P153-C25): daily THEN weekly, one repo, an
    unreachable blob planted and backdated past the prune expiry BEFORE
    daily runs. Reproduces the spike's repro_steps and its kill criterion
    (1): the blob must be gone after daily-then-weekly.

    This case FAILS against the pre-change `_TIER_ARGV["daily"]` --
    `--schedule=daily` includes `loose-objects`, which packs the already-old
    blob before weekly's prune leg ever runs, and a plain `git prune` only
    ever drops LOOSE objects. Asserted directly below by monkeypatching
    `_TIER_ARGV` back to that shape and confirming the same blob survives."""
    repo = _init_repo(tmp_path)
    _churn(repo, 30)
    sha = _plant_unreachable_blob(repo, "packed-by-daily\n", age_days=30)
    assert _git(repo, "cat-file", "-e", sha).returncode == 0

    gm.run_tier(repo, "daily")
    result = gm.run_tier(repo, "weekly")

    assert result.pruned is True
    assert _git(repo, "cat-file", "-e", sha).returncode != 0, "unreachable blob survived"


def test_daily_then_weekly_falsifier_fails_against_the_pre_change_daily_argv(tmp_path, monkeypatch):
    """The other half of the falsifier: replaying the SAME scenario against
    the pre-R9 daily argv (`--schedule=daily`, which carries `loose-objects`)
    must reproduce the filed bug -- the blob survives, because daily already
    packed it before weekly's prune leg got a chance to see it loose."""
    monkeypatch.setitem(gm._TIER_ARGV, "daily", ("maintenance", "run", "--schedule=daily"))
    repo = _init_repo(tmp_path)
    # `maintenance.strategy=incremental` is what assigns `loose-objects` to
    # the daily schedule in the first place -- without it `--schedule=daily`
    # runs no tasks at all, and the trap this test replays never fires. This
    # is the `git_perf_config.apply()` shape the R9 spike's own repro_steps
    # used, set here directly rather than through that module so this test
    # keeps its own scenario self-contained.
    _git(repo, "config", "maintenance.strategy", "incremental")
    _churn(repo, 30)
    sha = _plant_unreachable_blob(repo, "packed-by-daily\n", age_days=30)
    assert _git(repo, "cat-file", "-e", sha).returncode == 0

    gm.run_tier(repo, "daily")
    gm.run_tier(repo, "weekly")

    assert _git(repo, "cat-file", "-e", sha).returncode == 0, (
        "blob should still survive against the pre-change daily argv -- "
        "if this now fails, the falsifier above stopped proving anything"
    )


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
    real_run_git = gm.run_git

    def recording(args, **kwargs):
        invoked.append(tuple(args))
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(gm, "run_git", recording)
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
    real_run_git = gm.run_git

    def failing_prune(args, **kwargs):
        if args and args[0] == "prune":
            # Stub the
            # real GitResult shape run_git now returns, not a bare
            # subprocess.CompletedProcess.
            return GitResult(returncode=128, stdout="", stderr="boom", timed_out=False)
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(gm, "run_git", failing_prune)
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


def _loose_shas(repo: Path) -> set:
    objects = repo / ".git" / "objects"
    return {
        d.name + f.name
        for d in objects.iterdir()
        if len(d.name) == 2 and d.is_dir()
        for f in d.iterdir()
    }


def _daily_fixture(tmp_path: Path) -> tuple[Path, str]:
    from coordinator_core.benchmarks.maintenance_tier_budget import add_small_packs_and_loose

    repo = _init_repo(tmp_path)
    _git(repo, "config", "maintenance.strategy", "incremental")
    _git(repo, "config", "maintenance.auto", "false")
    return repo, add_small_packs_and_loose(repo, 40, 15)


def _reachable(repo: Path) -> set:
    return {
        line.split()[0]
        for line in _git(repo, "rev-list", "--objects", "--all").stdout.splitlines()
        if line
    }


def _pack_files(repo: Path) -> list:
    return sorted(p.name for p in (repo / ".git" / "objects" / "pack").iterdir())


def _packed_repo(tmp_path: Path, packs: int) -> Path:
    from coordinator_core.benchmarks.maintenance_tier_budget import add_small_packs_and_loose

    repo = _init_repo(tmp_path)
    add_small_packs_and_loose(repo, packs, 0)
    return repo


def _age(path: Path, seconds: int = 7200) -> Path:
    t = time.time() - seconds
    os.utime(path, (t, t))
    return path


def _small_store_tuning(monkeypatch, *, max_packs: int, batch_packs: int, loose_dir_entries: int):
    """Shrink the hygiene thresholds so a small fixture crosses them deterministically."""
    from coordinator_core.git import store_hygiene

    monkeypatch.setattr(store_hygiene, "CONSOLIDATE_MAX_PACKS", max_packs)
    monkeypatch.setattr(store_hygiene, "CONSOLIDATE_BATCH_PACKS", batch_packs)
    monkeypatch.setattr(store_hygiene, "_loose_in_fanout", lambda objects_dir: loose_dir_entries)


def test_daily_merges_small_packs_and_covers_every_pack_with_the_midx(tmp_path):
    from coordinator_core.git.store_hygiene import status

    repo, unreachable = _daily_fixture(tmp_path)
    before = status(repo / ".git")
    assert before.pack_count >= 40 and (before.midx_pack_count or 0) < before.pack_count

    result = gm.run_tier(repo, "daily")

    assert result.rc == 0, result.errors
    assert result.packs_merged > 1
    after = status(repo / ".git")
    assert after.pack_count < before.pack_count
    assert after.midx_pack_count == after.pack_count
    assert unreachable in _loose_shas(repo), "daily packed an unreachable object"


def test_daily_process_time_is_under_the_brightline(tmp_path):
    """Self plus children, the commit-graph and MIDX legs, on the loose-plus-many-pack fixture."""
    from coordinator_core.benchmarks.process_time import batched_process_time_ms

    repo, _ = _daily_fixture(tmp_path)
    total = 0.0
    for leg in (gm._TIER_ARGV["daily"], gm._MIDX_WRITE_ARGV):
        sample = batched_process_time_ms(["git", "-C", str(repo), *leg], k=1, cwd=str(repo))
        assert sample["rc"] == 0, leg
        total += sample["process_time_ms"]
    assert total < 500, total


def test_daily_never_issues_a_repack(tmp_path, monkeypatch):
    """`git repack` reads the index (rc 128 under a held one); no daily leg may be one."""
    invoked = _recorded_argv(tmp_path, "daily", monkeypatch)
    assert "repack" not in [args[0] for args in invoked], invoked


def test_daily_runs_under_a_held_index_lock_through_the_handler(tmp_path):
    """AC1."""
    repo, _ = _daily_fixture(tmp_path)
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")

    out = gm._git_maintenance({"tier": "daily", "repo": str(repo)})

    assert out["ran"] is True and out["deferred"] is None, out
    assert out["ok"] is True
    assert (repo / ".git" / "objects" / "pack" / "multi-pack-index").is_file()


@pytest.mark.parametrize("tier", ["hourly", "daily"])
def test_non_index_tiers_ignore_a_mid_operation_marker(tmp_path, tier):
    repo = _init_repo(tmp_path)
    (repo / ".git" / "REBASE_HEAD").write_text("", encoding="utf-8")
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")

    result = gm.run_tier(repo, tier)

    assert result.deferred is None and result.ran is True


def test_weekly_through_the_handler_still_defers_on_a_held_index_lock(tmp_path):
    """AC2."""
    repo = _init_repo(tmp_path)
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")

    out = gm._git_maintenance({"tier": "weekly", "repo": str(repo)})

    assert out["ran"] is False and "index.lock" in out["deferred"], out


def test_daily_reaps_the_orphan_classes_and_keeps_live_and_unknown_files(tmp_path):
    """AC3."""
    repo = _packed_repo(tmp_path, 3)
    pack_dir = repo / ".git" / "objects" / "pack"
    live = {n for n in _pack_files(repo) if n.startswith("pack-")}
    gone = [
        _age(_write(pack_dir / ("pack-" + "a" * 40 + ".rev"))),
        _age(_write(pack_dir / ("pack-" + "b" * 40 + ".pack"))),
        _age(_write(pack_dir / (".tmp-4242-pack-" + "c" * 40 + ".pack"))),
        _age(_write(pack_dir / "tmp_pack_Zq81Ab")),
    ]
    kept = [
        _age(_write(pack_dir / "claude-klabauter-tail")),
        _write(pack_dir / ("pack-" + "d" * 40 + ".rev")),
    ]

    result = gm.run_tier(repo, "daily")

    assert result.rc == 0, result.errors
    assert result.pack_files_reaped == len(gone)
    assert not any(p.exists() for p in gone)
    assert all(p.exists() for p in kept)
    assert live <= set(_pack_files(repo)), "a live pack triplet was reaped"


def test_daily_heals_a_midx_that_names_a_missing_pack(tmp_path):
    """AC4."""
    from coordinator_core.git import store_hygiene

    repo = _packed_repo(tmp_path, 3)
    common = repo / ".git"
    assert gm.run_tier(repo, "daily").rc == 0
    midx = common / "objects" / "pack" / "multi-pack-index"
    assert midx.is_file()
    victim = sorted((common / "objects" / "pack").glob("pack-*.idx"))[0]
    for suffix in (".idx", ".pack", ".rev"):
        store_hygiene._unlink_quiet(victim.with_suffix(suffix))
    assert store_hygiene.midx_names_missing_pack(common)

    result = gm.run_tier(repo, "daily")

    assert result.rc == 0, result.errors
    assert result.midx_healed is True
    assert not store_hygiene.midx_names_missing_pack(common)
    status = store_hygiene.status(common)
    assert status.midx_pack_count == status.pack_count


def test_one_daily_call_runs_one_batch_and_the_next_call_reduces_packs_further(tmp_path, monkeypatch):
    """AC5."""
    from coordinator_core.git.store_hygiene import status

    repo, _ = _daily_fixture(tmp_path)
    _small_store_tuning(monkeypatch, max_packs=4, batch_packs=10, loose_dir_entries=10)
    reachable_loose = _loose_shas(repo) & _reachable(repo)
    assert reachable_loose
    n0 = status(repo / ".git").pack_count

    first = gm.run_tier(repo, "daily")
    n1 = status(repo / ".git").pack_count

    assert first.rc == 0, first.errors
    assert 0 < first.packs_merged <= 10 and first.loose_packed == 0
    assert n1 < n0
    assert _loose_shas(repo) & _reachable(repo) == reachable_loose, "a second batch ran in one call"

    second = gm.run_tier(repo, "daily")

    assert second.rc == 0, second.errors
    assert status(repo / ".git").pack_count < n1


def test_daily_loose_leg_packs_a_bounded_reachable_batch_and_leaves_unreachable(tmp_path, monkeypatch):
    repo, unreachable = _daily_fixture(tmp_path)
    _small_store_tuning(monkeypatch, max_packs=1000, batch_packs=10, loose_dir_entries=10)
    monkeypatch.setattr(gm, "_LOOSE_BATCH_OBJECTS", 5)
    before = _loose_shas(repo) & _reachable(repo)
    assert len(before) > 5

    result = gm.run_tier(repo, "daily")

    assert result.rc == 0, result.errors
    assert result.packs_merged == 0 and result.loose_packed == 5
    assert len(_loose_shas(repo) & _reachable(repo)) == len(before) - 5
    assert unreachable in _loose_shas(repo)
    for _ in range(10):
        if not (_loose_shas(repo) & _reachable(repo)):
            break
        assert gm.run_tier(repo, "daily").rc == 0
    assert not (_loose_shas(repo) & _reachable(repo)), "reachable loose objects survived repeated daily calls"
    assert unreachable in _loose_shas(repo)
    assert _git(repo, "fsck", "--no-dangling").returncode == 0


def test_loose_leg_respects_the_byte_bound(tmp_path, monkeypatch):
    repo, _ = _daily_fixture(tmp_path)
    _small_store_tuning(monkeypatch, max_packs=1000, batch_packs=10, loose_dir_entries=10)
    monkeypatch.setattr(gm, "_LOOSE_BATCH_BYTES", 1)

    packed, error = gm._pack_loose_batch(repo, repo / ".git")

    assert error is None and packed == 1, "the first object always fits; the byte cap stops the second"


def test_a_clean_store_pays_no_loose_walk(tmp_path, monkeypatch):
    invoked = _recorded_argv(tmp_path, "daily", monkeypatch)
    assert not any(args[0] in ("rev-list", "pack-objects") for args in invoked), invoked


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
    """THE ACCEPTANCE ORACLE, and the only implementation of it left in the
    tree. Delegates to `coordinator_core.benchmarks.maintenance_tier_budget
    .check_maintenance_tier_budget` -- the same function the plan's own
    falsifier (`docs/plans/2026-08-30-ceremony-driven-git-maintenance
    .falsifier.py`) imports and runs -- instead of restating a second
    sampling design here. The oracle lives in `coordinator_core/` rather
    than in the `docs/plans/` artifact so that archiving the plan (closed
    plans move to `archive/specs/YYYY-MM/`, with nothing moving a
    `.falsifier.py` sidecar alongside its `.md`) cannot silently break this
    test. A prior version of this test measured with a single shared repo,
    k=3 repeat-averaging of a non-idempotent command, and the weekly prune
    leg sequenced AFTER the maintenance run, all three defects the oracle's
    docstring documents fixing. Keeping a second, uncorrected copy next to
    the corrected one is how that drift happened the first time.

    Process time and spawn count, never wall clock: N independent COLD
    repos per tier, each registered through coordinator's own config path
    then churned, one first-run sample each, averaged over N. Over the bar
    is a kill-bar item: rebuild under the bar, do not shave spawns.
    """
    _FALSIFIER_SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    ok, detail = check_maintenance_tier_budget(tmp_path)
    assert ok, (
        "over the 500ms brightline -- a KILL-BAR item: rebuild under the bar, "
        f"do not shave spawns off it. {detail}"
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
    d = tmp_path / ".git" / "objects" / "pack"
    d.mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
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


# --------------------------------------------------------------------------
# Object-store writer lock: another engine writer holding it defers the tier
# --------------------------------------------------------------------------

_HOLDER_CHILD = """
import sys
from pathlib import Path
from coordinator_core.git.object_store_lock import hold
with hold(Path(sys.argv[1]), wait_s=30.0, holder_label="test-holder"):
    Path(sys.argv[2]).write_text("ready")
    sys.stdin.read()
"""


@pytest.fixture()
def foreign_holder(tmp_path):
    """Start a real process holding the object-store lock of `<repo>/.git`."""
    import sys

    procs = []

    def start(common: Path) -> None:
        ready = tmp_path / "holder-ready"
        env = dict(os.environ)
        root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER_CHILD, str(common), str(ready)],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
        procs.append(proc)
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert time.monotonic() < deadline, "holder never took the lock"
            time.sleep(0.02)

    yield start
    for proc in procs:
        proc.stdin.close()
        proc.wait(timeout=60)


@pytest.mark.parametrize("tier", ["hourly", "daily", "weekly"])
def test_a_held_object_store_lock_defers_every_tier_with_no_git_spawned(
    tmp_path, monkeypatch, foreign_holder, tier
):
    repo = _init_repo(tmp_path)
    foreign_holder(repo / ".git")

    def no_git(*args, **kwargs):
        raise AssertionError(f"git spawned under a held object-store lock: {args}")

    monkeypatch.setattr(gm, "run_git", no_git)
    monkeypatch.setattr(gm, "absolute_git_dir", lambda r: str(repo / ".git"))
    monkeypatch.setattr(gm, "git_common_dir", lambda r: str(repo / ".git"))

    result = gm.run_tier(repo, tier)

    assert result.deferred is not None and "object-store lock" in result.deferred
    assert result.ran is False and result.pruned is False
    assert result.rc == 0


def test_a_held_object_store_lock_defers_the_orphan_sweep_and_unlinks_nothing(
    tmp_path, foreign_holder
):
    d = _pack_dir(tmp_path)
    common = d.parent.parent
    orphan = _write(d / ".tmp-1234-pack-abcdef.pack", age_sec=3600)
    foreign_holder(common)

    result = gm.sweep_orphan_packs(d, on_wait=lambda: pytest.fail("window paid under a held lock"))

    assert result.deferred is not None
    assert result.reaped == [] and orphan.exists()


def test_the_tier_runs_once_the_foreign_holder_is_gone(tmp_path):
    repo = _init_repo(tmp_path)
    result = gm.run_tier(repo, "hourly")
    assert result.deferred is None and result.ran is True


def test_sweep_refuses_a_pack_dir_not_shaped_objects_pack(tmp_path):
    odd = tmp_path / "elsewhere"
    odd.mkdir()
    with pytest.raises(ValueError):
        gm.sweep_orphan_packs(odd, on_wait=lambda: None)

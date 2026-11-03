"""Machine-local group-em home, resolve-only.

Port of coordinator-content-repo `coordinator/hooks/scripts/_gem_home.py`. A change to either side is made
to both. The resolve path is zero spawn, zero write (registry TOML read directly); only
`ensure_home` writes, and spawns `git init`.

Also carries the G-EM memo identity: the home signs as SENDER_ID and receives at its own
`state/cross-repo/inbox`; RECEIVER_IDS name the addresses that resolve to it.
`machine_resolver` imports this module lazily to avoid a cycle.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from coordinator_core import machine_resolver
from coordinator_core._settings_home import settings_home
from coordinator_core.git.run import run_git
from coordinator_core.session import record_homes

KEY = "coordinator.group_em_home"
LEGACY_KEY = "coordinator.general_work_home"
DEFAULT_DIRNAME = "group-em"
LEGACY_DIRNAME = "general-work"
SENDER_ID = "group-em-em"
RECEIVER_IDS = frozenset({SENDER_ID})
REMOTE_URL_KEY = "coordinator.group_em_home_remote_url"
REMOTE_BRANCH_KEY = "coordinator.group_em_home_remote_branch"
REMOTE_DECLINED_KEY = "coordinator.group_em_home_remote_declined"
DEFAULT_REMOTE_BRANCH = "main"
OPT_OUTS = ("gh-absent", "gh-unauthenticated", "declined")
# Per-box runtime the home never commits; `state/cross-repo/` stays tracked (the memo inbox).
_IGNORED = (
    ".coordinator-local/",
    "state/group-em-watch*",  # watch json, its lock sidecar, spool, parked map
    "scratch/",  # governor.log and governor-reaps.jsonl live under <home>/scratch
    "governor.log*",
    "governor-reaps.jsonl",
    "pm_turns.jsonl",
    "state/residue/",
    Path(record_homes.home_dir(".", "audits")).as_posix().removeprefix("./") + "/",
    "state/block-discharge/",
    "block-discharge/",
)


class HomeRefused(Exception):
    """The resolved home sits inside a checkout; `.checkout` names it."""

    def __init__(self, home: str, checkout: str):
        self.home = home
        self.checkout = checkout
        super().__init__(f"group-em home {home} is inside checkout {checkout}")


def _norm(p) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def contains(root, path) -> bool:
    """True when `path` is `root` or lies under it, compared on normalised real paths."""
    r, p = _norm(root), _norm(path)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def inside_checkout(path) -> str | None:
    """The checkout `path` lies in, else None: the first strict ancestor holding `.git`, or
    the first `repos.*` registry root equal to or containing it."""
    target = _norm(path)
    for anc in Path(target).parents:
        if (anc / ".git").exists():
            return str(anc)
    for key, val in machine_resolver.merged_flat_registry().items():
        if key.startswith("repos.") and isinstance(val, str) and val and contains(val, target):
            return val
    return None


def resolve_home() -> Path:
    """The group-em home: configured KEY, else LEGACY_KEY, else `<settings-home>/group-em`
    (the legacy dirname only when the default is absent and the legacy dir exists).
    Raises HomeRefused when it lies inside a checkout."""
    flat = machine_resolver.merged_flat_registry()
    # registry_value returns "" for an empty declaration; that reads as unset.
    configured = (
        machine_resolver.registry_value(KEY, flat)
        or machine_resolver.registry_value(LEGACY_KEY, flat)
    )
    if configured:
        home = Path(configured)
    else:
        base = settings_home()
        home = base / DEFAULT_DIRNAME
        if not home.exists() and (base / LEGACY_DIRNAME).exists():
            home = base / LEGACY_DIRNAME
    hit = inside_checkout(home)
    if hit:
        raise HomeRefused(str(home), hit)
    return home


def is_home(path) -> bool:
    """True when `path` is the home or under it. A refused home is never a home."""
    try:
        return contains(resolve_home(), path)
    except HomeRefused:
        return False


def _configured() -> str | None:
    flat = machine_resolver.merged_flat_registry()
    return (
        machine_resolver.registry_value(KEY, flat)
        or machine_resolver.registry_value(LEGACY_KEY, flat)
        or None
    )


def gem_root() -> str | None:
    rec = gem_holder()
    root = rec.get("repo_root") if rec else None
    return root if isinstance(root, str) and root else None


def _legacy_home_live(legacy: Path) -> bool:
    """True when anything runs in `legacy`. Fails closed: any error reads as live."""
    try:
        if contains(legacy, os.getcwd()):
            return True
        from coordinator_core.group_em import session_registry

        for row in session_registry.read_rows():
            if row.cwd and session_registry.pid_alive(row.pid) and contains(legacy, row.cwd):
                return True
        root = gem_root()
        return bool(root and contains(legacy, root))
    except Exception:
        return True


def migrate_legacy_home() -> str:
    """Rename the default-path `<settings-home>/general-work` to `group-em` when safe.

    Returns one of moved, no-legacy, configured, both-present, live, busy, refused. Only the
    default-path legacy dir moves; a configured home is never touched, neither key is written,
    and an existing target is never merged or overwritten. Never raises."""
    try:
        if _configured():
            return "configured"
        base = settings_home()
        legacy, target = base / LEGACY_DIRNAME, base / DEFAULT_DIRNAME
        if not legacy.exists():
            return "no-legacy"
        if target.exists():
            return "both-present"
        if inside_checkout(target):
            return "refused"
        if _legacy_home_live(legacy):
            return "live"
        os.rename(legacy, target)
        return "moved"
    except OSError:
        return "busy"
    except Exception:
        return "live"


def _ensure_ignored(home: Path, entries: tuple[str, ...] = _IGNORED) -> None:
    """Append any missing `entries` line to the home's `.gitignore`; never rewrites a line."""
    path = home / ".gitignore"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    missing = [entry for entry in entries if entry not in lines]
    if missing:
        path.write_text("\n".join([*lines, *missing]) + "\n", encoding="utf-8")


def default_template() -> Path | None:
    """The DoE-owned home-CLAUDE.md under the DoE content root, else None. Never raises."""
    try:
        from coordinator_core import content_root

        root = content_root.read_content_root()
    except Exception:
        return None
    if not root:
        return None
    for base in (Path(root), Path(root) / "coordinator"):
        cand = base / "templates" / "group-em-home" / "home-CLAUDE.md"
        if cand.is_file():
            return cand
    return None


def ensure_home(template: Path | None = None, refresh: bool = False) -> Path:
    """`ensure_home_outcome` for a caller that does not report the seed outcome."""
    return ensure_home_outcome(template, refresh)[0]


def remote_status() -> tuple[str | None, str | None]:
    """`(url, opt_out)`: the recorded remote URL, else None plus why there is none.

    opt_out is one of OPT_OUTS, or None when a URL is recorded. The gh probe runs only when no
    URL is recorded: `declined` (the declined key is set, or gh is ready and nothing asked for a
    remote), `gh-absent` (no gh on PATH), `gh-unauthenticated` (`gh auth status` non-zero).
    Never raises."""
    try:
        url = machine_resolver.registry_get(REMOTE_URL_KEY)
        if url:
            return url, None
        declined = (machine_resolver.registry_get(REMOTE_DECLINED_KEY) or "").strip().lower()
        if declined in ("1", "true", "yes"):
            return None, "declined"
        if not shutil.which("gh"):
            return None, "gh-absent"
        res = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return None, ("declined" if res.returncode == 0 else "gh-unauthenticated")
    except Exception:
        return None, "gh-unauthenticated"


def _attach_remote(home: Path, url: str) -> None:
    """Attach `url` as origin of a commit-less home: fetch and check out the recorded branch.
    The URL lands only in `.git/config`. Best-effort: a fetch or checkout failure leaves the
    home as it was."""
    cwd = str(home)
    if run_git(["rev-parse", "--verify", "-q", "HEAD"], cwd=cwd).returncode == 0:
        return
    if run_git(["remote", "get-url", "origin"], cwd=cwd).returncode != 0:
        run_git(["remote", "add", "origin", url], cwd=cwd)
    branch = machine_resolver.registry_get(REMOTE_BRANCH_KEY) or DEFAULT_REMOTE_BRANCH
    if run_git(["fetch", "-q", "origin", branch], cwd=cwd, remote=True).returncode != 0:
        return
    run_git(["checkout", "-q", "-B", branch, "--track", f"origin/{branch}"], cwd=cwd)


def ensure_home_outcome(template: Path | None = None, refresh: bool = False) -> tuple[Path, str]:
    """`ensure_home_report` narrowed to `(home, seed)` for callers that ignore the remote."""
    rep = ensure_home_report(template, refresh)
    return rep["home"], rep["seed"]


def ensure_home_report(template: Path | None = None, refresh: bool = False) -> dict:
    """Migrate a legacy home when safe, then create the home, `git init` it, attach the recorded
    remote when the home has no commits, add `state/`, write the ignore list, seed CLAUDE.md when
    absent (or on refresh, which overwrites it).

    Returns `{home, seed, remote, remote_opt_out}`. The template defaults to `default_template()`.
    `seed` is one of seeded, kept (CLAUDE.md present, no refresh), no-template (none resolved or
    not a file; nothing written). `remote` is the recorded URL or None; with no URL,
    `remote_opt_out` names why (see `remote_status`) and the home stays local-only."""
    migrate_legacy_home()
    home = resolve_home()
    home.mkdir(parents=True, exist_ok=True)
    if not (home / ".git").exists():
        res = run_git(["init", "-q", str(home)])
        if res.returncode != 0:
            raise subprocess.CalledProcessError(
                res.returncode, ["git", "init", "-q", str(home)], res.stdout, res.stderr
            )
    url, opt_out = remote_status()
    if url:
        _attach_remote(home, url)
    (home / "state").mkdir(exist_ok=True)
    _ensure_ignored(home)
    report = {"home": home, "seed": "no-template", "remote": url, "remote_opt_out": opt_out}
    src = template or default_template()
    if src is None or not src.is_file():
        return report
    seed = home / "CLAUDE.md"
    if not refresh and seed.exists():
        report["seed"] = "kept"
        return report
    seed.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    report["seed"] = "seeded"
    return report


_receiver_ids_cache: frozenset[str] | None = None


def receiver_ids() -> frozenset[str]:
    """RECEIVER_IDS unioned with the manifest's `identity.groupEmReceiverIds`, read once per
    process. A manifest unreadable on the first call degrades to RECEIVER_IDS for the rest of
    the process (the reader returns an empty set on failure, indistinguishable from absent).
    Tests reset `_receiver_ids_cache` to None."""
    global _receiver_ids_cache
    if _receiver_ids_cache is None:
        try:
            # Lazy: _memo_resolver imports this module at top level.
            from coordinator_core.ops.fleet._memo_resolver import read_group_em_receiver_ids

            extra = read_group_em_receiver_ids()
        except Exception:
            extra = set()
        _receiver_ids_cache = RECEIVER_IDS | frozenset(extra)
    return _receiver_ids_cache


def is_group_em_receiver(receiver: str) -> bool:
    """True when `receiver`, stripped and lowercased, is a G-EM receiver id."""
    return receiver.strip().lower() in receiver_ids()


def gem_holder() -> dict | None:
    """The live box-wide G-EM holder record, else None. Never raises; writes nothing."""
    try:
        from coordinator_core.group_em import nomination

        rec = nomination.box_holder()
        if isinstance(rec, dict) and rec.get("live") is True:
            return rec
    except Exception:
        pass
    return None


def home_inbox_dirs(home: Path) -> tuple[Path, ...]:
    """The home's inbox directories: current layout first, then the legacy one."""
    return (
        Path(record_homes.home_dir(str(home), "cross-repo")) / "inbox",
        home / "cross-repo" / "inbox",
    )

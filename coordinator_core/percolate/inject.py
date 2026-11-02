"""Delete-sync survival: post-delete content injection + destination-native preservation.

Two co-located but architecturally distinct operations (§ docs/plans/
2026-07-10-percolation-engine-claude-klabauter.md § Transform-kind taxonomy, `inject` /
`preserve-destination-native` rows; § Phase model, class (c) and the paired-op carve-out):

  - `inject`: a post-sync **class (c)** hook. Copies source-analog-less content INTO the
    target tree AFTER the delete pass (the rsync `--delete` step removes any prior
    injected content; this hook restores it -- self-healing by construction, not a
    one-time seed). Idempotent via dist-source purity: the copy is `mkdir -p` + `cp -r`
    + prune semantics (§ `_copy_tree_overwrite`), so re-running it over an
    already-injected tree is a no-op-equivalent (the destination ends up identical
    either way) -- including converging AWAY a `dst` file `src` no longer has, not only
    overwriting the files `src` still does (the gap a real already-populated destination
    could hit before the prune step existed). Caveat: this overwrite-converges
    basis is the DEFAULT-mode (`mode: overwrite`) behavior only -- an entry declaring
    `mode: create-if-absent` converges by ABSENCE-SKIP instead (never touches a `dst`
    that already exists), and may additionally be gated on a `condition_present` sibling
    path (§ `run_inject`). Because it is a stdin-fed hook in
    the rsync `--itemize-changes` pipeline like every post-sync hook, it MUST drain its
    stdin even though it ignores the content -- an un-drained pipe deadlocks rsync on a
    full output buffer (the class-(c) "ignores content, still drains" gotcha).

  - `preserve_destination_native`: NOT class (c). It is its own paired two-phase
    operation spanning BOTH sides of the rsync delete-sync: a pre-rsync BACKUP leg (copy
    dest-only files matching a basename allowlist out of the target tree before rsync's
    `--delete` can remove them) and a post-rsync-before-transform RESTORE leg (copy them
    back before any content transform runs). Idempotency basis is the backup-restore
    ROUND-TRIP itself, keyed on a sentinel file that hands off state between the two
    legs -- distinct from inject's dist-source purity.

Negative-spec: this module contains zero repo-specific scrub content -- no persona
names, no codenames, no DoE path literals, and (the sentinel-home gotcha) no hard-coded
consumer-home literal. The sentinel's directory is a GENERIC default
(`consumer_state_home()`) that any consumer resolves the same way; a store may override
it via `sentinel_home_override`, and the `<target>` component of the sentinel filename is
consumer-target identity supplied by the caller/store at call time, never embedded here.
"""

from __future__ import annotations

GENERATES = []

import shutil
import sys
from pathlib import Path
from typing import IO, Callable


class RequiredChildMissingError(RuntimeError):
    pass


def consumer_state_home() -> Path:
    return Path.home() / '.local' / 'state' / 'percolate'


def drain_stdin(stream: IO[str] | None = None) -> None:
    stream = stream if stream is not None else sys.stdin
    for _ in stream:
        pass


def run_inject(
    entries: list[dict],
    target_root: str | Path,
    stdin: IO[str] | None = None,
    *,
    on_copied: "Callable[[Path], None] | None" = None,
) -> None:
    """Run the `inject` class-(c) hook: drain stdin, then copy every entry after the delete pass.

    `entries` is the store's resolved `inject` section (list of `{src, dst,
    required_children?, mode?, condition_present?}` dicts -- § Store schema `injectEntry`).
    `target_root` is the root of the already-rsync'd destination tree; each entry's `dst`
    is resolved relative to it, and `condition_present` (when set) is also resolved
    relative to it.

    Per-entry `mode` (default `"overwrite"`):
      - `"overwrite"`: current behavior -- always copy, converging via dist-source purity
        (re-running over an already-injected tree is a no-op-equivalent).
      - `"create-if-absent"`: skip the copy if `dst` already exists. This entry's
        idempotency basis is ABSENCE-SKIP, not overwrite-converges -- re-running never
        touches a `dst` that already exists, whoever created it. `required_children` is
        still enforced against the pre-existing `dst` on the absent-skip path: declaring
        `required_children` is a guarantee about `dst`'s final shape regardless of which
        run (or no run at all) produced it.

    Per-entry `condition_present` (optional, str): a path relative to `target_root`. When
    set, the entry (copy AND required_children check) is skipped entirely unless that path
    exists -- the "conditional on a sibling path" case.

    `on_copied` (optional): called with `dst` immediately after this call ACTUALLY performed
    the copy for that entry -- i.e. every `mode: "overwrite"` entry, and every
    `mode: "create-if-absent"` entry whose `dst` did not already exist. NOT called for a
    `create-if-absent` entry that skipped the copy (§ that branch's own "never touches a
    dst that already exists, whoever created it" contract -- a caller using `on_copied` to
    drive a post-copy transform must not run that transform over content this call did not
    itself write), nor for an entry `condition_present` skipped entirely. This module stays
    content-agnostic (§ module docstring negative-spec): `on_copied` is the caller's own
    hook (`coordinator_core.percolate.engine.run_inject_for_section` supplies one that
    scrubs the copy via the store's own content-transform config) -- this function never
    inspects or names what the callback does.

    Raises RequiredChildMissingError if a post-inject required-child invariant fails.
    """
    drain_stdin(stdin)

    target_root = Path(target_root)
    for entry in entries:
        condition_present = entry.get('condition_present')
        if condition_present is not None and not (target_root / condition_present).exists():
            continue

        src = Path(entry['src'])
        dst = target_root / entry['dst']
        mode = entry.get('mode', 'overwrite')

        if mode == 'create-if-absent' and dst.exists():
            pass
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                _copy_tree_overwrite(src, dst)
            else:
                shutil.copy2(src, dst)
            if on_copied is not None:
                on_copied(dst)

        for child_name in entry.get('required_children') or []:
            if not (dst / child_name).exists():
                raise RequiredChildMissingError(
                    f'inject entry dst={dst} is missing required child "{child_name}" '
                    f'after copying from src={src}'
                )


def _copy_tree_overwrite(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    _prune_dst_files_absent_from_src(src, dst)


def _prune_dst_files_absent_from_src(src: Path, dst: Path) -> None:
    for dst_file in sorted(p for p in dst.rglob("*") if p.is_file()):
        rel = dst_file.relative_to(dst)
        if not (src / rel).is_file():
            dst_file.unlink()

    for dst_dir in sorted((p for p in dst.rglob("*") if p.is_dir()), reverse=True):
        try:
            dst_dir.rmdir()
        except OSError:
            pass


def _sentinel_path(section: dict, target_name: str) -> Path:
    override = section.get('sentinel_home_override')
    root = Path(override) if override else consumer_state_home()
    return root / f'preserve-destination-native.{target_name}.sentinel'


def backup_destination_native(section: dict, target_root: str | Path, target_name: str) -> Path:
    target_root = Path(target_root)
    sentinel = _sentinel_path(section, target_name)
    sentinel.parent.mkdir(parents=True, exist_ok=True)

    backup_dir = sentinel.parent / f'{sentinel.name}.backup'
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    backed_up: list[str] = []
    for basename in section.get('basename_allowlist') or []:
        src = target_root / basename
        if not src.exists():
            continue
        dst = backup_dir / basename
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        backed_up.append(basename)

    sentinel.write_text('\n'.join(backed_up) + ('\n' if backed_up else ''), encoding='utf-8', newline="\n")
    return sentinel


def restore_destination_native(section: dict, target_root: str | Path, target_name: str) -> list[str]:
    target_root = Path(target_root)
    sentinel = _sentinel_path(section, target_name)
    if not sentinel.exists():
        return []

    backup_dir = sentinel.parent / f'{sentinel.name}.backup'
    restored: list[str] = []
    for basename in sentinel.read_text(encoding='utf-8').splitlines():
        basename = basename.strip()
        if not basename:
            continue
        src = backup_dir / basename
        if not src.exists():
            continue
        dst = target_root / basename
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        restored.append(basename)

    sentinel.unlink(missing_ok=True)
    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    return restored

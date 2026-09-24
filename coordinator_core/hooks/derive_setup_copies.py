"""coordinator_core.hooks.derive_setup_copies — PostToolUse
(Write|Edit|MultiEdit) op: re-derive DERIVED install-template copies from
their CANONICAL repo-root sources whenever a canonical row is written, in
the coordinator-claude doctrine-plane repo.

Arrival note (W4-C5, `docs/plans/2026-09-18-doe-holds-no-scripts.md`): ported
from DoE-claude `coordinator/hooks/scripts/derive-setup-copies.py`. Shape,
per the W4-C1 verdict: command/native-door, `hooks.<name>` op,
payload-dict-in/response-out -- stdin JSON read and `_message_envelope.emit()`
are replaced with `register_op`'s contract and this package's own
`allow_advisory`/`no_advisory` builders. The pure `evaluate()` core keeps the
ROWS table, the byte-copy/contract-only mode split, and the
canonical<->derived direction contract byte-faithfully; only the I/O/channel
plumbing changes.

Repo-root resolution -- REUSES
`coordinator_core.hooks.derive_global_doctrine_live_copy._resolve_doctrine_repo_root`
rather than a second copy of the same multi-rung probe: this hook's ROWS are
relative to the identical coordinator-claude doctrine-plane repo root that
hook already resolves (`global-doctrine/`, `coordinator/templates/...` are
siblings under the same root as `setup/`, `coordinator/templates/setup/...`).
Unlike that sibling hook, this one does NOT gate on the `.coordinator-dev-repo`
OSS-clobber sentinel -- there is no live-global-config clobber hazard here
(every row's canonical AND derived side lives inside the same tracked repo
tree), so a resolution miss degrades to "no repo root -> no row ever
matches -> silent no-op" rather than needing an explicit sentinel check.

Everything else (the ROWS table, `Row`/`ResolvedRow` shapes,
`ContractOnlyNotOverwritten`, the derived-write-is-never-clobbered contract,
the fail-loud-on-real-failure / silent-on-non-matching-path contract) is
unchanged.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C5
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Optional

from coordinator_core._hook_envelope import payload_of
from coordinator_core.hooks._envelope import allow_advisory, no_advisory
from coordinator_core.hooks.derive_global_doctrine_live_copy import (
    _display_path,
    _resolve_doctrine_repo_root,
)
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.ipc import register_op

_WIKI_ANCHOR = (
    "coordinator/docs/wiki/guard-message-concision.md"
    "#derive-setup-copies-parity-modes-and-remedies"
)

BYTE_COPY = "byte-copy"
CONTRACT_ONLY = "contract-only"

_VALID_MODES = frozenset({BYTE_COPY, CONTRACT_ONLY})


@dataclass(frozen=True)
class Row:
    canonical: Path
    derived: Path
    mode: str
    parity_test: "Optional[str]" = None

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            raise ValueError(
                f"Row for {self.canonical} has unrecognized mode {self.mode!r}; "
                f"must be one of {sorted(_VALID_MODES)}"
            )


@dataclass(frozen=True)
class ResolvedRow:
    canonical: Path
    derived: Path
    mode: str
    parity_test: "Optional[str]" = None

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            raise ValueError(
                f"ResolvedRow for {self.canonical} has unrecognized mode "
                f"{self.mode!r}; must be one of {sorted(_VALID_MODES)}"
            )


ROWS: "tuple[Row, ...]" = (
    Row(
        canonical=Path("setup") / "percolate-hooks" / "percolate-store.yaml",
        derived=Path("coordinator") / "templates" / "setup" / "percolate-hooks" / "percolate-store.yaml",
        mode=BYTE_COPY,
    ),
    Row(
        canonical=Path("setup") / "percolate-hooks" / "README.md",
        derived=Path("coordinator") / "templates" / "setup" / "percolate-hooks" / "README.md",
        mode=BYTE_COPY,
    ),
    Row(
        canonical=Path("setup") / "publish_sync.py",
        derived=Path("coordinator") / "templates" / "setup" / "publish_sync.py",
        mode=CONTRACT_ONLY,
        parity_test="test_publish_sync_copies_parity.py",
    ),
)


class ContractOnlyNotOverwritten(Exception):
    def __init__(self, row: ResolvedRow) -> None:
        super().__init__(f"contract-only row, not overwritten: {row.derived}")
        self.row = row


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except Exception:
        return path


def _resolved_rows(repo_root: Path) -> "tuple[ResolvedRow, ...]":
    return tuple(
        ResolvedRow(
            canonical=_resolve(repo_root / row.canonical),
            derived=_resolve(repo_root / row.derived),
            mode=row.mode,
            parity_test=row.parity_test,
        )
        for row in ROWS
    )


def _derive_or_raise(row: ResolvedRow) -> None:
    if row.mode == CONTRACT_ONLY:
        raise ContractOnlyNotOverwritten(row)
    row.derived.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(row.canonical, row.derived)


def _exc_reason(exc: Exception) -> str:
    return getattr(exc, "strerror", None) or str(exc)


def _derived_write_advisory(file_path: str, row: ResolvedRow, repo_root: Path):
    canonical = _display_path(row.canonical, repo_root)
    if row.mode == CONTRACT_ONLY:
        if row.parity_test:
            prose = (
                f"hand-maintained DERIVED copy, not reverted -- keep "
                f"{row.parity_test} passing; re-author {canonical}."
            )
        else:
            prose = f"hand-maintained DERIVED copy, not reverted (no parity test yet); re-author {canonical}."
    else:
        prose = f"DERIVED copy, not propagated -- lost on next canonical write; re-author {canonical}."
    return compose(prose, anchor=_WIKI_ANCHOR)


def _read_failure_message(row: ResolvedRow, exc: Exception, repo_root: Path):
    prose = f"could not read canonical, derived not re-derived ({_exc_reason(exc)})."
    return compose(prose, anchor=_WIKI_ANCHOR)


def _contract_only_message(row: ResolvedRow, repo_root: Path):
    if row.parity_test:
        prose = f"canonical is contract-only, derived not re-derived. Confirm {row.parity_test} passes."
    else:
        prose = "canonical is contract-only, derived not re-derived (no parity test yet)."
    return compose(prose, anchor=_WIKI_ANCHOR)


def _write_failure_message(row: ResolvedRow, exc: Exception, repo_root: Path):
    prose = f"could not write derived -- canonical read OK ({_exc_reason(exc)})."
    return compose(prose, anchor=_WIKI_ANCHOR)


def _success_message(row: ResolvedRow, source_bytes: bytes, repo_root: Path):
    prose = f"re-derived {_display_path(row.derived, repo_root)} ({len(source_bytes)}B)."
    return compose(prose, anchor=_WIKI_ANCHOR)


def _handle_canonical_write(row: ResolvedRow, repo_root: Path):
    try:
        source_bytes = row.canonical.read_bytes()
    except Exception as exc:
        return _read_failure_message(row, exc, repo_root)

    try:
        _derive_or_raise(row)
    except ContractOnlyNotOverwritten:
        return _contract_only_message(row, repo_root)
    except Exception as exc:
        return _write_failure_message(row, exc, repo_root)

    return _success_message(row, source_bytes, repo_root)


def evaluate(payload: dict):
    """Pure core: given a parsed PostToolUse(Write|Edit|MultiEdit) payload,
    returns the advisory `Message` for a matching write (derived-write
    warning, or the canonical-write derivation outcome), or `None` for a
    silent no-op (non-matching path, or repo root unresolvable)."""
    if not isinstance(payload, dict):
        return None

    tool_input = payload.get("tool_input")
    file_path = ""
    if isinstance(tool_input, dict):
        file_path = tool_input.get("file_path", "") or ""
    if not isinstance(file_path, str) or not file_path:
        return None

    repo_root = _resolve_doctrine_repo_root()
    if repo_root is None:
        return None

    try:
        written = Path(PureWindowsPath(file_path).as_posix()).resolve()
    except Exception:
        return None

    rows = _resolved_rows(repo_root)

    resolved_repo_root = _resolve(repo_root)

    for row in rows:
        if written == row.derived:
            return _derived_write_advisory(file_path, row, resolved_repo_root)

    for row in rows:
        if written == row.canonical:
            return _handle_canonical_write(row, resolved_repo_root)

    return None


@register_op("hooks.derive_setup_copies")
def _handler(params: dict, repo_root=None) -> dict:
    """PostToolUse(Write|Edit|MultiEdit) op: re-derive a canonical
    `setup/`-tree write's paired install-template copy, or warn on a write
    landing directly on a derived copy."""
    params = payload_of(params)
    message = evaluate(params)
    if message is None:
        return no_advisory()
    return allow_advisory("PostToolUse", render(message))

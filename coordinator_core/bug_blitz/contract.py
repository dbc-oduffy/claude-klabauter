"""Contract for the bug-blitz package: types, outcome codes, names and record path arithmetic.

No logic beyond path arithmetic. `evidence` implements `ParseTargets`, `mint` implements
`MintRows`, `targeted_run` writes `RunnerRecord`; `hooks.submit_bug_blitz` depends on the
Protocols here, never on those modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from coordinator_core.session.record_homes import home_dir

SUBMIT_OP = "hooks.submit_bug_blitz"
STUB_NAME = "bug-blitz"
CLI_NAME = "targeted-test-run"
RUN_TAG_PREFIX = "bug-blitz-"
RECORD_SCHEMA_FILENAME = "targeted-test-run-record.schema.json"

OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_NOTHING_TO_RUN = "nothing-to-run"
OUTCOME_REFUSED = "refused"
OUTCOME_NO_RUNNER = "no-runner"
OUTCOME_FOREIGN_UNCOMMITTED = "passed-on-foreign-uncommitted"

OUTCOMES = (
    OUTCOME_PASSED,
    OUTCOME_FAILED,
    OUTCOME_NOTHING_TO_RUN,
    OUTCOME_REFUSED,
    OUTCOME_NO_RUNNER,
    OUTCOME_FOREIGN_UNCOMMITTED,
)

# Exit 2 is reserved for usage errors and is not an outcome.
EXIT_USAGE = 2
EXIT_CODES = {
    OUTCOME_PASSED: 0,
    OUTCOME_FAILED: 1,
    OUTCOME_NOTHING_TO_RUN: 3,
    OUTCOME_REFUSED: 4,
    OUTCOME_NO_RUNNER: 5,
    OUTCOME_FOREIGN_UNCOMMITTED: 6,
}

TargetKind = Literal["dir", "file", "backlog"]


@dataclass(frozen=True)
class FailingTest:
    """One failing test. `file` is the test file, `package` is "" outside a workspace."""

    file: str
    name: str
    package: str = ""
    message: str = ""


@dataclass(frozen=True)
class TargetSpec:
    """A resolved `targets` arg. `path` is "" for kind `backlog`."""

    kind: TargetKind
    path: str = ""


@dataclass(frozen=True)
class ParseResult:
    """The failing tests of a fully parsed target; an unparsed target refuses, it is never carried here."""

    tests: tuple[FailingTest, ...]


@dataclass(frozen=True)
class TfRow:
    """A minted bug-backlog row for one failing test file, tagged `tf` and the run tag."""

    row_id: str
    surface: str
    tests: tuple[FailingTest, ...]
    severity: Literal["P0", "P1"] = "P1"
    reused: bool = False


@dataclass(frozen=True)
class RunnerRecord:
    """The `.tests.json` record; validates against `targeted-test-run-record.schema.json`."""

    row_id: str
    runner: str
    argv: tuple[str, ...]
    selected: tuple[str, ...]
    outcome: str
    exit_code: int | None
    failing_ids: tuple[str, ...] = ()
    process_s: float = 0.0
    foreign_holders: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def to_json(self) -> dict:
        """JSON-ready dict in schema field order."""
        return {
            "row_id": self.row_id,
            "runner": self.runner,
            "argv": list(self.argv),
            "selected": list(self.selected),
            "outcome": self.outcome,
            "exit_code": self.exit_code,
            "failing_ids": list(self.failing_ids),
            "foreign_holders": {p: list(h) for p, h in self.foreign_holders},
            "process_s": self.process_s,
        }


class ParseTargets(Protocol):
    """Parse a resolved target into failing tests; raises `NoFailingTests` on zero parsed for non-backlog and `UnparsedTargets` when any target line is unparsed."""

    def __call__(self, spec: TargetSpec) -> ParseResult: ...


class MintRows(Protocol):
    """Mint one open TF row per failing test file, idempotently; returns the row ids."""

    def __call__(
        self,
        repo_root: Path,
        run_id: str,
        tests: tuple[FailingTest, ...],
        *,
        target_ref: str,
    ) -> list[str]: ...


def run_tag(run_id: str) -> str:
    """The backlog tag every row minted or reused by the run carries."""
    return f"{RUN_TAG_PREFIX}{run_id}"


REAP_CLOSED_BY = "bug-backlog-reap"
REAP_RECORD_REL = "state/scratch/bug-blitz/reap/latest.json"


@dataclass(frozen=True)
class ReapRecord:
    """Result of the pre-dispatch reap: row ids by test verdict at `head_sha`."""

    head_sha: str
    ran_at: str
    red: tuple[str, ...] = ()
    green: tuple[str, ...] = ()
    unrun: dict[str, str] | None = None

    def to_json(self) -> dict:
        """JSON-ready dict; `unrun` maps row id to the reason it was not run."""
        return {
            "head_sha": self.head_sha,
            "ran_at": self.ran_at,
            "red": list(self.red),
            "green": list(self.green),
            "unrun": dict(self.unrun or {}),
        }

    @classmethod
    def from_json(cls, data: dict) -> "ReapRecord":
        """Inverse of `to_json`; absent lists/maps read as empty."""
        return cls(
            head_sha=data["head_sha"],
            ran_at=data["ran_at"],
            red=tuple(data.get("red") or ()),
            green=tuple(data.get("green") or ()),
            unrun=dict(data.get("unrun") or {}),
        )


def reap_record_rel() -> str:
    """Repo-relative POSIX path of the latest reap record."""
    return REAP_RECORD_REL


def tests_record_rel(profile: str, run_id: str, row_id: str) -> str:
    """Repo-relative POSIX path of a row's runner record."""
    home = Path(home_dir("", "queue-grind")).as_posix()
    return f"{home}/{profile}/evidence/{run_id}/{row_id}.tests.json"


def record_schema_path() -> Path:
    """Absolute path of the runner-record JSON schema."""
    return Path(__file__).resolve().parent.parent / "contract" / RECORD_SCHEMA_FILENAME

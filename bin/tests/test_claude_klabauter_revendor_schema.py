"""
bin/tests/test_claude_klabauter_revendor_schema.py — unit tests for bin/claude-klabauter-revendor-schema.py,
the general named-schema re-vendor + re-pin entrypoint.

Purpose: prove the property the script exists for — that a re-vendor moves the vendored
BYTES and the gating PIN together, or moves neither. The defect this closes is a fresh
installer following `cp`-shaped remediation prose, satisfying the advisory drift probe
(which compares against coordinator-claude HEAD) while breaking the gating tamper-check (which compares
against `_QUEUE_SCHEMA_PINS`) — see
state/audits/2026-07-28-windows-install-dogfood-friction.md § F3.

Coverage:
  1. `_load_pin_registry` reads literal pins, module-constant aliases, and fails loud on
     a missing/malformed registry (it must never silently degrade a pin-tracked schema
     to HEAD-tracked — that is the original bug).
  2. `_rewrite_pin_registry` moves a pin, replaces an alias with a literal, records the
     reason as a comment, leaves other entries untouched, and keeps the file parseable.
  3. `_major_bump_reasons` is DELTA-scoped: a sticky `x-bump-class: major` on an
     unchanged version is a note, not a gate.
  4. `run()` end-to-end against a real local fake-clone git repo: idempotent no-op,
     dry-run writes nothing, a real run writes bytes AND pin together, `--reason` and
     `--ack-major` are enforced, `--ref` is refused for HEAD-tracked schemas, and a
     failed post-vendor verification rolls BOTH files back.
  5. Decline records: a live decline blocks a matching re-vendor, `--ack-declined`
     overrides it, the decline self-expires once the incoming version moves past the
     declined one, a decline for a different schema never blocks, and `_write_decline`
     round-trips a record `_decline_gate_reasons` can then read back. Includes a
     regression reproducing the 2026-08-13 incident this feature responds to: coordinator-claude moves
     a schema's validation shape while leaving `x-schema-version` unchanged, an operator
     backs it out and records the decline, and a second re-vendor attempt at the same
     shape+version is refused without `--ack-declined`.

No network. Real local `git` only, in tmp fake clones. Module path constants
(`_SCHEMAS_DIR`, `_PIN_REGISTRY_FILE`, `_DECLINE_RECORDS_DIR`) are monkeypatched to
isolated tmp dirs — this suite never touches the real vendored schemas, pin registry,
or decline records.

Spec backlink: state/audits/2026-07-28-windows-install-dogfood-friction.md § F3
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

# Real local git repos are load-bearing: run() is asserted end-to-end against
# a real fake-clone repo, including that a real run writes vendored bytes AND
# the pin registry together and a failed verification rolls BOTH back --
# properties a mocked git object model can't reproduce.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# ---------------------------------------------------------------------------
# Load the script module via importlib (dash in filename).
# Registered in sys.modules before exec so @dataclass can resolve __module__.
# ---------------------------------------------------------------------------
_BIN_DIR = Path(__file__).resolve().parent.parent
_SCRIPT = _BIN_DIR / "claude-klabauter-revendor-schema.py"

_spec = importlib.util.spec_from_file_location("_revendor_schema_mod", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_PIN_SRC_TEMPLATE = '''"""Stand-in for the real gating test module."""

_C1_LANDING_SHA = "{c1}"

# An existing hand-authored note, to prove rewrites stack below it rather than
# clobbering it.
_QUEUE_SCHEMA_PINS = {{
    'alpha': _C1_LANDING_SHA,
    'beta': "{beta}",
}}
'''


def _write_pin_registry(path: Path, c1: str = "a" * 40, beta: str = "b" * 40) -> Path:
    path.write_text(_PIN_SRC_TEMPLATE.format(c1=c1, beta=beta), encoding="utf-8")
    return path


def _schema_bytes(version: str = "1.0.0", bump: str | None = None, extra: str = "") -> bytes:
    body: dict[str, object] = {"x-schema-version": version, "title": f"t{extra}"}
    if bump is not None:
        body["x-bump-class"] = bump
    return (json.dumps(body, indent=2) + "\n").encode("utf-8")


def _git(clone: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(clone), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def fake_clone(tmp_path: Path) -> Path:
    """A real local git repo shaped like coordinator-claude (coordinator/schemas/<name>.schema.json)."""
    clone = tmp_path / "fake-doe"
    (clone / "coordinator" / "schemas").mkdir(parents=True)
    _git_init = ["git", "init", "-q", str(clone)]
    subprocess.run(_git_init, check=True, capture_output=True)
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    return clone


def _commit_schema(clone: Path, name: str, content: bytes) -> str:
    p = clone / "coordinator" / "schemas" / f"{name}.schema.json"
    p.write_bytes(content)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", f"add {name}")
    out = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point the module's vendored-schema dir and pin registry at tmp paths."""
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    registry = _write_pin_registry(tmp_path / "test_schema_validate.py")
    declines = tmp_path / "schema-decline-records"
    monkeypatch.setattr(_mod, "_SCHEMAS_DIR", schemas)
    monkeypatch.setattr(_mod, "_PIN_REGISTRY_FILE", registry)
    monkeypatch.setattr(_mod, "_DECLINE_RECORDS_DIR", declines)
    return {"schemas": schemas, "registry": registry, "declines": declines}


# ---------------------------------------------------------------------------
# 1. Pin-registry reading
# ---------------------------------------------------------------------------

class TestLoadPinRegistry:
    def test_reads_literal_and_alias_pins(self, tmp_path: Path) -> None:
        reg = _write_pin_registry(tmp_path / "r.py", c1="c" * 40, beta="d" * 40)
        pins = _mod._load_pin_registry(reg)
        assert set(pins) == {"alpha", "beta"}
        assert pins["alpha"].sha == "c" * 40
        assert pins["alpha"].via_alias == "_C1_LANDING_SHA"
        assert pins["beta"].sha == "d" * 40
        assert pins["beta"].via_alias is None

    def test_missing_file_fails_loud(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            _mod._load_pin_registry(tmp_path / "nope.py")

    def test_missing_symbol_fails_loud(self, tmp_path: Path) -> None:
        """A renamed/relocated registry must abort, never degrade to 'no pins'.

        Degrading here would reclassify every pin-tracked schema as HEAD-tracked and
        reproduce the exact advisory-green/gate-red split this script exists to close.
        """
        p = tmp_path / "r.py"
        p.write_text("X = 1\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            _mod._load_pin_registry(p)

    def test_unresolvable_alias_fails_loud(self, tmp_path: Path) -> None:
        p = tmp_path / "r.py"
        p.write_text("_QUEUE_SCHEMA_PINS = {\n    'a': _NOT_DEFINED,\n}\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            _mod._load_pin_registry(p)

    def test_real_repo_registry_parses(self) -> None:
        """The tracked, real pin registry must be readable by this script.

        Guards the coupling directly: if the real `_QUEUE_SCHEMA_PINS` ever changes
        shape, this fails here rather than at the moment someone re-vendors.
        """
        pins = _mod._load_pin_registry(_mod._PIN_REGISTRY_FILE)
        assert pins, "real _QUEUE_SCHEMA_PINS parsed as empty"
        assert all(len(p.sha) == 40 for p in pins.values())


# ---------------------------------------------------------------------------
# 2. Pin-registry rewriting
# ---------------------------------------------------------------------------

class TestRewritePinRegistry:
    def test_alias_becomes_literal_and_other_entries_untouched(self, tmp_path: Path) -> None:
        reg = _write_pin_registry(tmp_path / "r.py", c1="a" * 40, beta="b" * 40)
        pins = _mod._load_pin_registry(reg)
        new_sha = "e" * 40
        src = _mod._rewrite_pin_registry(reg, [(pins["alpha"], new_sha)], "HEAD", "because")
        reg.write_text(src, encoding="utf-8")

        reread = _mod._load_pin_registry(reg)
        assert reread["alpha"].sha == new_sha
        assert reread["alpha"].via_alias is None, "alias should be replaced by a literal"
        assert reread["beta"].sha == "b" * 40, "unrelated pin must not move"
        # _C1_LANDING_SHA itself must survive — other consumers may still alias it.
        assert "_C1_LANDING_SHA" in src

    def test_reason_is_recorded_as_a_comment(self, tmp_path: Path) -> None:
        reg = _write_pin_registry(tmp_path / "r.py")
        pins = _mod._load_pin_registry(reg)
        src = _mod._rewrite_pin_registry(
            reg, [(pins["beta"], "f" * 40)], "HEAD", "coordinator-claude landed the widened enum"
        )
        assert "coordinator-claude landed the widened enum" in src
        assert "bin/claude-klabauter-revendor-schema.py" in src
        ast.parse(src)  # still valid Python

    def test_multiple_moves_in_one_rewrite(self, tmp_path: Path) -> None:
        reg = _write_pin_registry(tmp_path / "r.py")
        pins = _mod._load_pin_registry(reg)
        src = _mod._rewrite_pin_registry(
            reg, [(pins["alpha"], "1" * 40), (pins["beta"], "2" * 40)], "HEAD", "why"
        )
        reg.write_text(src, encoding="utf-8")
        reread = _mod._load_pin_registry(reg)
        assert reread["alpha"].sha == "1" * 40
        assert reread["beta"].sha == "2" * 40

    def test_crlf_file_stays_crlf(self, tmp_path: Path) -> None:
        """Windows first-class: the real pin registry is CRLF on disk.

        A default text-mode read-modify-write would translate every line to LF and
        bury a two-character pin change inside a 3.7k-line whole-file rewrite.
        """
        reg = tmp_path / "r.py"
        reg.write_bytes(
            _PIN_SRC_TEMPLATE.format(c1="a" * 40, beta="b" * 40)
            .replace("\n", "\r\n")
            .encode("utf-8")
        )
        pins = _mod._load_pin_registry(reg)
        src = _mod._rewrite_pin_registry(reg, [(pins["beta"], "9" * 40)], "HEAD", "why")
        reg.write_text(src, encoding="utf-8", newline="")

        data = reg.read_bytes()
        assert data.count(b"\n") == data.count(b"\r\n"), "every line must stay CRLF"
        assert _mod._load_pin_registry(reg)["beta"].sha == "9" * 40

    def test_lf_file_stays_lf(self, tmp_path: Path) -> None:
        reg = _write_pin_registry(tmp_path / "r.py")
        reg.write_bytes(reg.read_bytes().replace(b"\r\n", b"\n"))
        pins = _mod._load_pin_registry(reg)
        src = _mod._rewrite_pin_registry(reg, [(pins["beta"], "8" * 40)], "HEAD", "why")
        assert b"\r\n" not in src.encode("utf-8")

    def test_reason_with_newlines_cannot_break_the_module(self, tmp_path: Path) -> None:
        reg = _write_pin_registry(tmp_path / "r.py")
        pins = _mod._load_pin_registry(reg)
        src = _mod._rewrite_pin_registry(
            reg, [(pins["beta"], "3" * 40)], "HEAD", "line one\nline two\n'''quotes'''"
        )
        ast.parse(src)


# ---------------------------------------------------------------------------
# 3. Delta-scoped bump classification
# ---------------------------------------------------------------------------

class TestMajorBumpReasons:
    def test_sticky_major_on_unchanged_version_is_a_note_not_a_gate(self) -> None:
        """`x-bump-class` persists after the bump it describes.

        Gating on it file-scoped would refuse every later re-vendor of a schema whose
        last bump happened to be major — including a whitespace change.
        """
        local = _schema_bytes("3.0.0", "major")
        incoming = _schema_bytes("3.0.0", "major", extra="-changed")
        reasons, notes = _mod._major_bump_reasons(local, incoming)
        assert reasons == []
        assert notes and "unchanged" in notes[0]

    def test_semver_major_increase_gates(self) -> None:
        reasons, _ = _mod._major_bump_reasons(_schema_bytes("1.2.0"), _schema_bytes("2.0.0"))
        assert reasons and "major advances" in reasons[0]

    def test_version_change_classified_major_gates(self) -> None:
        reasons, _ = _mod._major_bump_reasons(
            _schema_bytes("1.0.0", "major"), _schema_bytes("1.1.0", "major")
        )
        assert reasons and "x-bump-class: major" in reasons[0]

    def test_additive_bump_does_not_gate(self) -> None:
        reasons, notes = _mod._major_bump_reasons(
            _schema_bytes("1.0.0", "nested-field-additive"),
            _schema_bytes("1.1.0", "nested-field-additive"),
        )
        assert reasons == [] and notes == []

    def test_absent_metadata_never_fabricates_a_major(self) -> None:
        reasons, notes = _mod._major_bump_reasons(b"{}\n", b'{"title": "x"}\n')
        assert reasons == [] and notes == []


# ---------------------------------------------------------------------------
# 4a. Decline records
# ---------------------------------------------------------------------------

def _handoff_like_schema(with_mapping_member: bool) -> dict:
    """A minimal schema shaped like the incident: `x-schema-version` at 7.1.0,
    with a nested mapping under `x-producer-typed-command` whose member set
    changes shape without the version moving."""
    mapping: dict[str, object] = {"kind": {"type": "string"}}
    if with_mapping_member:
        mapping["carried_items"] = {"type": "array"}
    return {
        "x-schema-version": "7.1.0",
        "x-producer-typed-command": {"mapping": mapping},
        "type": "object",
    }


def _dump(schema: dict) -> bytes:
    return (json.dumps(schema, indent=2, sort_keys=True) + "\n").encode("utf-8")


class TestDeclineGate:
    def test_write_decline_record_round_trips(
        self, fake_clone: Path, sandbox: dict[str, Path]
    ) -> None:
        _commit_schema(fake_clone, "alpha", _dump(_handoff_like_schema(True)))
        (sandbox["schemas"] / "alpha.schema.json").write_bytes(_dump(_handoff_like_schema(True)))

        rc = _mod._write_decline(
            schema_names=["alpha"],
            doe_clone_arg=str(fake_clone),
            ref="HEAD",
            reason="that debt is not ours to import",
            backout_sha="e" * 40,
        )
        assert rc == 0
        record_path = sandbox["declines"] / "alpha.json"
        assert record_path.is_file()
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert record["schema"] == "alpha"
        assert record["declined_schema_version"] == "7.1.0"
        assert record["reason"] == "that debt is not ours to import"
        assert record["backout_sha"] == "e" * 40
        assert record["status"] == "active"
        assert record["declined_shape_hash"] == _mod.semantic_shape_hash(
            _handoff_like_schema(True)
        )

    def test_write_decline_rejects_malshaped_backout_sha(
        self, fake_clone: Path, sandbox: dict[str, Path]
    ) -> None:
        _commit_schema(fake_clone, "alpha", _dump(_handoff_like_schema(True)))
        with pytest.raises(SystemExit):
            _mod._write_decline(
                schema_names=["alpha"],
                doe_clone_arg=str(fake_clone),
                ref="HEAD",
                reason="reason",
                backout_sha="not-a-sha!",
            )

    def test_live_decline_blocks_matching_revendor_incident_regression(
        self, fake_clone: Path, sandbox: dict[str, Path]
    ) -> None:
        """Reproduces the 2026-08-13 incident: same version, shape moved, previously
        declined — a second re-vendor attempt at the identical shape must refuse."""
        declined_shape = _handoff_like_schema(False)  # coordinator-claude's shape-narrowing edit
        _commit_schema(fake_clone, "alpha", _dump(declined_shape))
        (sandbox["schemas"] / "alpha.schema.json").write_bytes(_dump(_handoff_like_schema(True)))

        rc = _mod._write_decline(
            schema_names=["alpha"],
            doe_clone_arg=str(fake_clone),
            ref="HEAD",
            reason="that debt is not ours to import",
            backout_sha="e6af1c6cf" + "0" * 31,
        )
        assert rc == 0

        with pytest.raises(SystemExit):
            _mod.run(schema_names=["alpha"], doe_clone_arg=str(fake_clone))

    def test_ack_declined_overrides(
        self, fake_clone: Path, sandbox: dict[str, Path]
    ) -> None:
        declined_shape = _handoff_like_schema(False)
        head = _commit_schema(fake_clone, "alpha", _dump(declined_shape))
        (sandbox["schemas"] / "alpha.schema.json").write_bytes(_dump(_handoff_like_schema(True)))
        _mod._write_decline(
            schema_names=["alpha"],
            doe_clone_arg=str(fake_clone),
            ref="HEAD",
            reason="reason",
            backout_sha="f" * 40,
        )
        rc = _mod.run(
            schema_names=["alpha"],
            doe_clone_arg=str(fake_clone),
            ack_declined=True,
            reason="operator overrides the decline",
        )
        assert rc == 0
        assert (sandbox["schemas"] / "alpha.schema.json").read_bytes() == _dump(declined_shape)

    def test_decline_self_expires_once_version_moves(
        self, fake_clone: Path, sandbox: dict[str, Path]
    ) -> None:
        declined_shape = _handoff_like_schema(False)
        _commit_schema(fake_clone, "alpha", _dump(declined_shape))
        (sandbox["schemas"] / "alpha.schema.json").write_bytes(_dump(_handoff_like_schema(True)))
        _mod._write_decline(
            schema_names=["alpha"],
            doe_clone_arg=str(fake_clone),
            ref="HEAD",
            reason="reason",
            backout_sha="a" * 40,
        )

        # coordinator-claude resolves it: same shape, but bumped past the declined version (8.0.0).
        moved_shape = dict(declined_shape)
        moved_shape["x-schema-version"] = "8.0.0"
        _commit_schema(fake_clone, "alpha", _dump(moved_shape))

        rc = _mod.run(
            schema_names=["alpha"],
            doe_clone_arg=str(fake_clone),
            ack_major=True,
            reason="coordinator-claude resolved the shape narrow by bumping to 8.0.0",
        )
        assert rc == 0, "a decline must not block once upstream moved the version"
        assert (sandbox["schemas"] / "alpha.schema.json").read_bytes() == _dump(moved_shape)

    def test_decline_self_expiry_prints_a_note(
        self, fake_clone: Path, sandbox: dict[str, Path], capsys: pytest.CaptureFixture
    ) -> None:
        """A decline that lapses (version moved) must say so, not vanish silently."""
        declined_shape = _handoff_like_schema(False)
        _commit_schema(fake_clone, "alpha", _dump(declined_shape))
        (sandbox["schemas"] / "alpha.schema.json").write_bytes(_dump(_handoff_like_schema(True)))
        _mod._write_decline(
            schema_names=["alpha"],
            doe_clone_arg=str(fake_clone),
            ref="HEAD",
            reason="reason",
            backout_sha="a" * 40,
        )

        moved_shape = dict(declined_shape)
        moved_shape["x-schema-version"] = "8.0.0"
        _commit_schema(fake_clone, "alpha", _dump(moved_shape))

        rc = _mod.run(
            schema_names=["alpha"],
            doe_clone_arg=str(fake_clone),
            ack_major=True,
            reason="coordinator-claude resolved the shape narrow by bumping to 8.0.0",
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "decline no longer applies" in out
        assert "alpha" in out

    def test_decline_gate_reasons_returns_expiry_note_on_version_move(self) -> None:
        reasons, note = _mod._decline_gate_reasons(
            "alpha",
            _dump(_handoff_like_schema(False)),
        )
        # No record loaded (module-level path, no sandbox) -> no reason, no note.
        assert reasons == [] and note is None

    def test_decline_for_different_schema_does_not_block(
        self, fake_clone: Path, sandbox: dict[str, Path]
    ) -> None:
        declined_shape = _handoff_like_schema(False)
        _commit_schema(fake_clone, "alpha", _dump(declined_shape))
        _commit_schema(fake_clone, "gamma", _dump(_handoff_like_schema(True)))
        (sandbox["schemas"] / "alpha.schema.json").write_bytes(_dump(_handoff_like_schema(True)))
        (sandbox["schemas"] / "gamma.schema.json").write_bytes(_dump(_handoff_like_schema(False)))
        _mod._write_decline(
            schema_names=["alpha"],
            doe_clone_arg=str(fake_clone),
            ref="HEAD",
            reason="reason",
            backout_sha="b" * 40,
        )

        # gamma is HEAD-tracked (not in the pin registry) and carries no decline.
        rc = _mod.run(schema_names=["gamma"], doe_clone_arg=str(fake_clone))
        assert rc == 0
        assert (sandbox["schemas"] / "gamma.schema.json").read_bytes() == _dump(
            _handoff_like_schema(True)
        )

    def test_unparseable_incoming_at_matching_version_notes_but_does_not_block(
        self, sandbox: dict[str, Path]
    ) -> None:
        """Fail-open on unparseable JSON stays fail-open, but must not go silent."""
        sandbox["declines"].mkdir(parents=True, exist_ok=True)
        (sandbox["declines"] / "alpha.json").write_text(
            json.dumps(
                {
                    "schema": "alpha",
                    "declined_shape_hash": "irrelevant",
                    "declined_schema_version": "7.1.0",
                    "reason": "r",
                    "backout_sha": "a" * 40,
                    "status": "active",
                }
            ),
            encoding="utf-8",
        )
        # Invalid UTF-8 bytes: the lenient `errors="replace"` decode used to read
        # x-schema-version still succeeds (the version key sits before the bad byte),
        # but the strict decode used to parse+hash the shape raises — the exact split
        # that makes this branch reachable rather than dead code shadowed by the
        # version-mismatch check above.
        malformed = b'{"x-schema-version": "7.1.0", "junk": "\xff\xfe"}'
        reasons, note = _mod._decline_gate_reasons("alpha", malformed)
        assert reasons == []
        assert note is not None and "did not parse as JSON" in note

    def test_no_decline_record_is_a_pure_no_op_on_the_gate(
        self, sandbox: dict[str, Path]
    ) -> None:
        assert _mod._decline_gate_reasons("nonexistent", _dump(_handoff_like_schema(True))) == ([], None)


# ---------------------------------------------------------------------------
# 3a. Decline record schema
# ---------------------------------------------------------------------------

class TestDeclineRecordSchema:
    def test_well_formed_record_validates(self) -> None:
        schema = _mod._load_decline_record_schema()
        record = {
            "schema": "alpha",
            "declined_shape_hash": "sha256:" + "a" * 64,
            "declined_schema_version": "7.1.0",
            "reason": "that debt is not ours to import",
            "backout_sha": "e" * 40,
            "declared_ref": "HEAD",
            "declared_doe_sha": "f" * 40,
            "declared": "2026-08-13",
            "status": "active",
        }
        errors = _mod._validate_json_schema_node(record, schema, schema, "")
        assert errors == []

    def test_missing_required_field_is_rejected(self) -> None:
        schema = _mod._load_decline_record_schema()
        record = {
            "schema": "alpha",
            "declined_shape_hash": "sha256:" + "a" * 64,
            "declined_schema_version": "7.1.0",
            "reason": "that debt is not ours to import",
            # backout_sha missing
            "declared_ref": "HEAD",
            "declared_doe_sha": "f" * 40,
            "declared": "2026-08-13",
            "status": "active",
        }
        errors = _mod._validate_json_schema_node(record, schema, schema, "")
        assert errors, "a record missing backout_sha must fail schema validation"

    def test_malformed_backout_sha_is_rejected(self) -> None:
        schema = _mod._load_decline_record_schema()
        record = {
            "schema": "alpha",
            "declined_shape_hash": "sha256:" + "a" * 64,
            "declined_schema_version": "7.1.0",
            "reason": "that debt is not ours to import",
            "backout_sha": "not-a-sha!",
            "declared_ref": "HEAD",
            "declared_doe_sha": "f" * 40,
            "declared": "2026-08-13",
            "status": "active",
        }
        errors = _mod._validate_json_schema_node(record, schema, schema, "")
        assert errors, "a malformed backout_sha must fail schema validation"

    def test_write_decline_round_trip_is_schema_valid(
        self, fake_clone: Path, sandbox: dict[str, Path]
    ) -> None:
        _commit_schema(fake_clone, "alpha", _dump(_handoff_like_schema(True)))
        (sandbox["schemas"] / "alpha.schema.json").write_bytes(_dump(_handoff_like_schema(True)))

        rc = _mod._write_decline(
            schema_names=["alpha"],
            doe_clone_arg=str(fake_clone),
            ref="HEAD",
            reason="that debt is not ours to import",
            backout_sha="e" * 40,
        )
        assert rc == 0
        record = _mod._load_decline_record("alpha")
        assert record is not None
        schema = _mod._load_decline_record_schema()
        errors = _mod._validate_json_schema_node(record, schema, schema, "")
        assert errors == []


# ---------------------------------------------------------------------------
# 4. run() end-to-end
# ---------------------------------------------------------------------------

class TestRunEndToEnd:
    def test_pin_tracked_revendor_moves_bytes_and_pin_together(
        self, fake_clone: Path, sandbox: dict[str, Path]
    ) -> None:
        """The property this whole script exists for."""
        head = _commit_schema(fake_clone, "alpha", _schema_bytes("1.0.0", extra="-new"))
        vendored = sandbox["schemas"] / "alpha.schema.json"
        vendored.write_bytes(_schema_bytes("1.0.0", extra="-old"))

        rc = _mod.run(
            schema_names=["alpha"],
            doe_clone_arg=str(fake_clone),
            reason="upstream widened the enum",
        )
        assert rc == 0
        assert vendored.read_bytes() == _schema_bytes("1.0.0", extra="-new")
        pins = _mod._load_pin_registry(sandbox["registry"])
        assert pins["alpha"].sha == head, "pin must move in the SAME operation as the bytes"
        assert pins["beta"].sha == "b" * 40

    def test_idempotent_second_run_is_a_no_op(
        self, fake_clone: Path, sandbox: dict[str, Path]
    ) -> None:
        _commit_schema(fake_clone, "alpha", _schema_bytes("1.0.0"))
        vendored = sandbox["schemas"] / "alpha.schema.json"
        vendored.write_bytes(_schema_bytes("1.0.0", extra="-old"))
        _mod.run(schema_names=["alpha"], doe_clone_arg=str(fake_clone), reason="r")
        before = sandbox["registry"].read_bytes()

        assert _mod.run(schema_names=["alpha"], doe_clone_arg=str(fake_clone)) == 0
        assert sandbox["registry"].read_bytes() == before, "no-op must not churn the registry"

    def test_dry_run_writes_nothing(
        self, fake_clone: Path, sandbox: dict[str, Path]
    ) -> None:
        _commit_schema(fake_clone, "alpha", _schema_bytes("1.0.0", extra="-new"))
        vendored = sandbox["schemas"] / "alpha.schema.json"
        vendored.write_bytes(_schema_bytes("1.0.0", extra="-old"))
        reg_before = sandbox["registry"].read_bytes()

        assert _mod.run(
            schema_names=["alpha"], doe_clone_arg=str(fake_clone), reason="r", dry_run=True
        ) == 0
        assert vendored.read_bytes() == _schema_bytes("1.0.0", extra="-old")
        assert sandbox["registry"].read_bytes() == reg_before

    def test_dry_run_previews_rather_than_refusing_on_an_unsatisfied_gate(
        self, fake_clone: Path, sandbox: dict[str, Path], capsys: pytest.CaptureFixture
    ) -> None:
        """A preview an unsatisfied gate blocks is a preview in name only."""
        _commit_schema(fake_clone, "alpha", _schema_bytes("2.0.0"))
        (sandbox["schemas"] / "alpha.schema.json").write_bytes(_schema_bytes("1.0.0"))

        assert _mod.run(
            schema_names=["alpha"], doe_clone_arg=str(fake_clone), dry_run=True
        ) == 0
        out = capsys.readouterr().out
        assert "would be REFUSED" in out
        assert "--ack-major" in out and "--reason" in out

    def test_pin_move_without_reason_is_refused(
        self, fake_clone: Path, sandbox: dict[str, Path]
    ) -> None:
        _commit_schema(fake_clone, "alpha", _schema_bytes("1.0.0", extra="-new"))
        (sandbox["schemas"] / "alpha.schema.json").write_bytes(_schema_bytes("1.0.0"))
        with pytest.raises(SystemExit):
            _mod.run(schema_names=["alpha"], doe_clone_arg=str(fake_clone))

    def test_major_advance_without_ack_is_refused(
        self, fake_clone: Path, sandbox: dict[str, Path]
    ) -> None:
        _commit_schema(fake_clone, "alpha", _schema_bytes("2.0.0"))
        (sandbox["schemas"] / "alpha.schema.json").write_bytes(_schema_bytes("1.0.0"))
        with pytest.raises(SystemExit):
            _mod.run(schema_names=["alpha"], doe_clone_arg=str(fake_clone), reason="r")

    def test_major_advance_with_ack_proceeds(
        self, fake_clone: Path, sandbox: dict[str, Path]
    ) -> None:
        head = _commit_schema(fake_clone, "alpha", _schema_bytes("2.0.0"))
        (sandbox["schemas"] / "alpha.schema.json").write_bytes(_schema_bytes("1.0.0"))
        assert _mod.run(
            schema_names=["alpha"],
            doe_clone_arg=str(fake_clone),
            reason="r",
            ack_major=True,
        ) == 0
        assert _mod._load_pin_registry(sandbox["registry"])["alpha"].sha == head

    def test_head_tracked_schema_needs_no_pin_and_refuses_a_non_head_ref(
        self, fake_clone: Path, sandbox: dict[str, Path]
    ) -> None:
        _commit_schema(fake_clone, "gamma", _schema_bytes("1.0.0", extra="-1"))
        _commit_schema(fake_clone, "gamma", _schema_bytes("1.0.0", extra="-2"))
        vendored = sandbox["schemas"] / "gamma.schema.json"
        vendored.write_bytes(_schema_bytes("1.0.0", extra="-old"))
        reg_before = sandbox["registry"].read_bytes()

        # gamma is not in the pin registry -> HEAD-tracked: no reason required, no pin write.
        assert _mod.run(schema_names=["gamma"], doe_clone_arg=str(fake_clone)) == 0
        assert vendored.read_bytes() == _schema_bytes("1.0.0", extra="-2")
        assert sandbox["registry"].read_bytes() == reg_before

        vendored.write_bytes(_schema_bytes("1.0.0", extra="-old"))
        with pytest.raises(SystemExit):
            _mod.run(
                schema_names=["gamma"], doe_clone_arg=str(fake_clone), ref="HEAD~1"
            )

    def test_unknown_schema_name_fails_loud(
        self, fake_clone: Path, sandbox: dict[str, Path]
    ) -> None:
        _commit_schema(fake_clone, "alpha", _schema_bytes())
        with pytest.raises(SystemExit):
            _mod.run(schema_names=["not-vendored"], doe_clone_arg=str(fake_clone))

    def test_failed_verification_rolls_back_bytes_and_pin(
        self, fake_clone: Path, sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No half-applied outcome: a red post-vendor check restores BOTH files."""
        _commit_schema(fake_clone, "alpha", _schema_bytes("1.0.0", extra="-new"))
        vendored = sandbox["schemas"] / "alpha.schema.json"
        original_bytes = _schema_bytes("1.0.0", extra="-old")
        vendored.write_bytes(original_bytes)
        reg_before = sandbox["registry"].read_bytes()

        def _boom(*_a, **_k):
            raise _mod.SchemaDriftError("simulated post-vendor divergence")

        monkeypatch.setattr(_mod, "check_schema_drift", _boom)
        with pytest.raises(SystemExit):
            _mod.run(schema_names=["alpha"], doe_clone_arg=str(fake_clone), reason="r")

        assert vendored.read_bytes() == original_bytes, "bytes must roll back"
        assert sandbox["registry"].read_bytes() == reg_before, "pin must roll back"

    def test_created_file_is_removed_on_rollback(
        self, fake_clone: Path, sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A schema absent from the vendored path must not survive a failed run."""
        _commit_schema(fake_clone, "alpha", _schema_bytes("1.0.0"))
        vendored = sandbox["schemas"] / "alpha.schema.json"
        # Present for the name check, removed after the plan is built.
        vendored.write_bytes(b"")

        real_build = _mod._build_plan

        def _build_then_delete(name, clone, sha, pins):
            plan = real_build(name, clone, sha, pins)
            plan.current = None
            vendored.unlink()
            return plan

        monkeypatch.setattr(_mod, "_build_plan", _build_then_delete)
        monkeypatch.setattr(
            _mod, "check_schema_drift",
            lambda *a, **k: (_ for _ in ()).throw(_mod.SchemaDriftError("nope")),
        )
        with pytest.raises(SystemExit):
            _mod.run(schema_names=["alpha"], doe_clone_arg=str(fake_clone), reason="r")
        assert not vendored.exists()

    def test_missing_doe_clone_fails_closed(self, tmp_path: Path, sandbox) -> None:
        with pytest.raises(SystemExit):
            _mod.run(schema_names=["alpha"], doe_clone_arg=str(tmp_path / "absent"))


# ---------------------------------------------------------------------------
# Entrypoint wiring
# ---------------------------------------------------------------------------

class TestHandoffEntrypointDelegates:
    def test_handoff_script_calls_the_shared_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """bin/claude-klabauter-revendor-handoff-schema.py must not carry a second copy of the
        mechanism — it fixes the schema set and delegates."""
        script = _BIN_DIR / "claude-klabauter-revendor-handoff-schema.py"
        spec = importlib.util.spec_from_file_location("_handoff_revendor_mod", script)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        seen: dict = {}

        def _fake_run(**kwargs):
            seen.update(kwargs)
            return 0

        general = mod._load_general()
        monkeypatch.setattr(general, "run", _fake_run)
        monkeypatch.setattr(mod, "_load_general", lambda: general)

        assert mod.main(["--dry-run"]) == 0
        assert seen["schema_names"] == ["handoff", "handoff-archived"]
        assert seen["ref"] == "HEAD"
        assert seen["dry_run"] is True


# ---------------------------------------------------------------------------
# 5. Consumer-corpus pre-flight wiring (2026-07-31 hardening)
# ---------------------------------------------------------------------------

def _handoff_schema_bytes(version: str = "1.0.0", bump: str | None = None) -> bytes:
    """Like `_schema_bytes`, but stamped `x-schema-name: handoff` — the only
    schema `_fleet_corpus_gate_reasons` engages the pre-flight for."""
    body: dict[str, object] = {"x-schema-version": version, "x-schema-name": "handoff"}
    if bump is not None:
        body["x-bump-class"] = bump
    return (json.dumps(body, indent=2) + "\n").encode("utf-8")


class TestFleetCorpusGate:
    def test_non_handoff_schema_never_invokes_preflight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = {"n": 0}

        def _fake_preflight():
            called["n"] += 1
            return {"off_enum_live": [], "unclassified": []}

        monkeypatch.setattr(_mod, "_run_consumer_corpus_preflight", _fake_preflight)
        reasons, notes = _mod._major_bump_reasons(
            _schema_bytes("1.0.0"), _schema_bytes("2.0.0")
        )
        assert reasons and "major advances" in reasons[0]
        assert called["n"] == 0, "non-handoff schema must never invoke the pre-flight"

    def test_handoff_major_plus_dirty_corpus_appends_gating_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_preflight():
            return {
                "off_enum_live": [{"repo": "cockpit", "kind": "spinoff-roadmap", "count": 25}],
                "unclassified": [],
            }

        monkeypatch.setattr(_mod, "_run_consumer_corpus_preflight", _fake_preflight)
        reasons, notes = _mod._major_bump_reasons(
            _handoff_schema_bytes("3.1.0"), _handoff_schema_bytes("4.0.0")
        )
        assert len(reasons) == 2, "the semver-major reason AND the fleet-corpus reason"
        assert any("off-enum" in r and "cockpit" in r for r in reasons)
        assert notes == []

    def test_handoff_major_plus_unclassified_repo_appends_gating_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_preflight():
            return {
                "off_enum_live": [],
                "unclassified": [{"key": "repos.brand_new_em", "path": "/x", "reason": "r"}],
            }

        monkeypatch.setattr(_mod, "_run_consumer_corpus_preflight", _fake_preflight)
        reasons, notes = _mod._major_bump_reasons(
            _handoff_schema_bytes("3.1.0"), _handoff_schema_bytes("4.0.0")
        )
        assert any("unclassified" in r for r in reasons)

    def test_handoff_major_plus_clean_corpus_does_not_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_preflight():
            return {"off_enum_live": [], "unclassified": []}

        monkeypatch.setattr(_mod, "_run_consumer_corpus_preflight", _fake_preflight)
        reasons, notes = _mod._major_bump_reasons(
            _handoff_schema_bytes("3.1.0"), _handoff_schema_bytes("4.0.0")
        )
        # Only the semver-major reason — the pre-flight found nothing to add.
        assert len(reasons) == 1 and "major advances" in reasons[0]
        assert notes == []

    def test_preflight_oracle_failure_degrades_to_a_note_not_a_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_preflight():
            raise _mod.PreflightOracleError("schema enum unreadable")

        monkeypatch.setattr(_mod, "_run_consumer_corpus_preflight", _fake_preflight)
        reasons, notes = _mod._major_bump_reasons(
            _handoff_schema_bytes("3.1.0"), _handoff_schema_bytes("4.0.0")
        )
        assert len(reasons) == 1 and "major advances" in reasons[0], (
            "an oracle failure must not add a SECOND gating reason"
        )
        assert notes and "oracle" in notes[0]

    def test_non_major_handoff_delta_never_invokes_preflight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = {"n": 0}

        def _fake_preflight():
            called["n"] += 1
            return {"off_enum_live": [], "unclassified": []}

        monkeypatch.setattr(_mod, "_run_consumer_corpus_preflight", _fake_preflight)
        reasons, notes = _mod._major_bump_reasons(
            _handoff_schema_bytes("1.0.0", "nested-field-additive"),
            _handoff_schema_bytes("1.1.0", "nested-field-additive"),
        )
        assert reasons == [] and notes == []
        assert called["n"] == 0, "gate must stay behind the major-bump condition, not run on every re-vendor"

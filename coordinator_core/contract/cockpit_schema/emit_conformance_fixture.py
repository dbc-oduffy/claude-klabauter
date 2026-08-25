"""
emit_conformance_fixture — regenerate DoE's emission-conformance fixture.

The fixture at ``coordinator/cockpit-contract/conformance/emission-conformance.json``
in the DoE clone is the reference emission consumers validate their ingest against.
It has had NO generator in either repo since ``454bc0ab`` (2026-07-08, when
``gen-emission-conformance.sh`` was deleted), so it has drifted to
``contract_version 2.7.0`` while the contract moved on, and it now fails its own
schema. DoE re-homed generation to claude-klabauter on 2026-08-23; this is that generator.

MIGRATES, does not synthesize from scratch. The fixture's value is that it carries
REAL, representative records; regenerating it as minimal stub objects would leave a
document that validates and demonstrates nothing. So the committed fixture is the
input: records are carried across, fields the target contract has since added are
synthesized per-field, and fields it has since removed are dropped. Only genuinely
new fields are ever invented.

NEGATIVE SPEC — ``min_supported_contract_version`` is DoE-OWNED AND HAND-MAINTAINED.
This generator MUST preserve the value read from the committed fixture and MUST
NEVER derive it. A generator emitting ``min_supported_contract_version ==
contract_version`` would trip claude-klabauter's own ``doe_drift`` version-band gate on every
still-current consumer pin -- the precise failure DoE's CD-2 re-vendor-window
discipline exists to prevent. Constraint carried verbatim from the owner
(doe-claude-em, 2026-08-23). If the committed fixture is absent there is no value to
preserve, and this generator REFUSES rather than guessing one.

The target contract version is whatever the SCHEMA BUNDLE says, never
``CONTRACT_VERSION`` from this package. That is deliberate: it makes the version an
input rather than a hardcode, so the generator can be proven against an ordinary
patch release before a major depends on it, and so pointing it at DoE's schema dir
regenerates for THEIR pin rather than claude-klabauter's working head.

Byte conventions are a DIFF-HYGIENE target, not a correctness one -- nothing in the
consumption path demands byte-exactness (``test_emit_parity`` compares parsed
records; ``doe_drift`` compares versions). Match the committed file so a regen is a
readable diff: 2-space indent, LF, trailing newline, ``ensure_ascii=False``, and key
order following the SCHEMA's property order rather than sorted. The committed file's
top-level keys are NOT sorted, so a ``sort_keys`` emit would rewrite the whole file
on its first run and bury the real change.

Fails loud rather than emitting something invalid: the generated body is validated
against the target schema before it is written, and a required field this module
cannot synthesize a *validating* value for is named in the error rather than filled
with a plausible-looking guess.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

_FIXTURE_REL = Path("coordinator/cockpit-contract/conformance/emission-conformance.json")
_ENVELOPE_SCHEMA_NAME = "snapshot-envelope.schema.json"

#: Fixture-level metadata that is NOT part of the envelope contract. The envelope
#: schema declares neither and is `additionalProperties: false`, so these are held
#: out of the envelope validation pass and reattached to the written document.
_FIXTURE_METADATA_KEYS = frozenset({"contract_version", "min_supported_contract_version"})

#: Deterministic stand-ins for the two ``format`` values the contract uses. Fixed,
#: never "now": a generator whose output changes on every run makes every regen a
#: whole-file diff and destroys the diff-hygiene the byte conventions exist for.
_FORMAT_SAMPLES = {
    "date-time": "2026-07-06T15:54:47Z",
    "date": "2026-07-06",
}

#: Candidate values tried, in order, for a string constrained by ``pattern``. This
#: module never invents a value that does not validate -- each candidate is checked
#: against the field's own subschema and a field where none match is reported by
#: name. Extending this list is the intended way to teach the generator a new
#: pattern; guessing in the caller is not.
_PATTERN_CANDIDATES = (
    "2026-07-06T15:54:47Z",
    "2026-07-06",
    "fixture-owner/fixture-repo",
    "0000000000000000000000000000000000000000",
    "00000000-0000-4000-8000-000000000000",
    "fixture",
    "0.0.0",
    "",
)


class FixtureGenerationError(RuntimeError):
    """Raised when the fixture cannot be generated correctly.

    Never raised for a merely-stale fixture -- staleness is the condition this
    module exists to fix. Raised when preserving DoE-owned state is impossible, or
    when a validating value for a required field cannot be produced.
    """


def _jsonschema():
    import jsonschema  # deferred: keeps this module importable without the dep
    import jsonschema.validators  # noqa: F401 -- `validators` is not re-exported

    return jsonschema


def _validator_for(schema: dict):
    js = _jsonschema()
    cls = js.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


def _validates(value: Any, subschema: dict) -> bool:
    return not list(_validator_for(subschema).iter_errors(value))


def _synthesize(subschema: dict, field_path: str) -> Any:
    """Build a minimal value satisfying ``subschema``, or raise naming the field.

    Order matters: ``const`` and ``enum`` are exact answers and are taken first;
    ``anyOf`` prefers a non-null branch so the fixture demonstrates a real value
    rather than a null wherever the contract permits one.
    """
    if "const" in subschema:
        return subschema["const"]
    if "enum" in subschema:
        options = [o for o in subschema["enum"] if o is not None] or subschema["enum"]
        return options[0]

    if "anyOf" in subschema:
        branches = subschema["anyOf"]
        non_null = [b for b in branches if b.get("type") != "null"]
        for branch in non_null + branches:
            try:
                return _synthesize(branch, field_path)
            except FixtureGenerationError:
                continue
        raise FixtureGenerationError(
            f"{field_path}: no anyOf branch could be synthesized"
        )

    declared = subschema.get("type")
    types = declared if isinstance(declared, list) else [declared]

    if "null" in types and len(types) == 1:
        return None
    if "object" in types:
        return _synthesize_object(subschema, field_path)
    if "array" in types:
        return []
    if "boolean" in types:
        return False
    if "integer" in types or "number" in types:
        low = subschema.get("minimum", 0)
        return low if isinstance(low, (int, float)) and low > 0 else 0
    if "string" in types:
        fmt = subschema.get("format")
        if fmt in _FORMAT_SAMPLES:
            return _FORMAT_SAMPLES[fmt]
        if "not" in subschema or "minLength" in subschema:
            # Provenance anchors carry `not: {const: ""}` -- an empty string is
            # explicitly forbidden there, so "" is never a safe default. Probe the
            # candidates rather than assuming which sentinel this field accepts.
            for candidate in _PATTERN_CANDIDATES:
                if candidate and _validates(candidate, subschema):
                    return candidate
            raise FixtureGenerationError(
                f"{field_path}: no candidate satisfies the field's constraints"
            )
        if "pattern" in subschema:
            for candidate in _PATTERN_CANDIDATES:
                if _validates(candidate, subschema):
                    return candidate
            raise FixtureGenerationError(
                f"{field_path}: no candidate satisfies pattern "
                f"{subschema['pattern']!r}. Add a value to _PATTERN_CANDIDATES "
                "rather than relaxing the check."
            )
        return "fixture"

    raise FixtureGenerationError(
        f"{field_path}: cannot synthesize a value for type {declared!r}"
    )


def _synthesize_object(subschema: dict, field_path: str) -> dict:
    """Build an object carrying exactly its required properties, in schema order."""
    props = subschema.get("properties", {})
    required = set(subschema.get("required", []))
    out: dict[str, Any] = {}
    for name, spec in props.items():
        if name in required:
            out[name] = _synthesize(spec, f"{field_path}.{name}")
    return out


def _conform(value: Any, subschema: dict, field_path: str) -> Any:
    """Carry ``value`` across to ``subschema``, adding what is missing and dropping
    what the contract no longer permits.

    Recursive, and deliberately conservative: an existing value is never rewritten
    just because a synthesized one would also validate. Only three edits happen --
    add a missing required property, drop a property ``additionalProperties: false``
    forbids, and replace a value that fails a closed ``enum``.
    """
    if "anyOf" in subschema:
        for branch in subschema["anyOf"]:
            if _validates(value, branch):
                return value
        for branch in subschema["anyOf"]:
            if branch.get("type") == "object" and isinstance(value, dict):
                return _conform(value, branch, field_path)
        return _synthesize(subschema, field_path)

    declared = subschema.get("type")
    types = declared if isinstance(declared, list) else [declared]

    if "object" in types and isinstance(value, dict):
        props = subschema.get("properties", {})
        required = set(subschema.get("required", []))
        closed = subschema.get("additionalProperties") is False
        out: dict[str, Any] = {}
        # Schema property order first -- this is what keeps a regen a small diff.
        for name, spec in props.items():
            if name in value:
                out[name] = _conform(value[name], spec, f"{field_path}.{name}")
            elif name in required:
                out[name] = _synthesize(spec, f"{field_path}.{name}")
        if not closed:
            for name, held in value.items():
                if name not in out:
                    out[name] = held
        return out

    if "array" in types and isinstance(value, list):
        items = subschema.get("items")
        if isinstance(items, dict):
            return [
                _conform(entry, items, f"{field_path}[{i}]")
                for i, entry in enumerate(value)
            ]
        return value

    if "enum" in subschema and value not in subschema["enum"]:
        return _synthesize(subschema, field_path)

    return value


def resolve_doe_clone(explicit: Optional[str] = None) -> Path:
    """Resolve the DoE clone the fixture lives in.

    Mirrors ``doe_drift``'s resolution rather than inventing a second one: an
    explicit path wins, then ``REPO_DOE_CLAUDE``, then a sibling directory beside
    this repo.
    """
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("REPO_DOE_CLAUDE")
    if env:
        return Path(env).resolve()
    sibling = Path(__file__).resolve().parents[3].parent / "DoE-claude"
    return sibling.resolve()


def build_fixture(committed: dict, envelope_schema: dict) -> dict:
    """Return the regenerated fixture body. Pure -- no disk, no clock.

    ``min_supported_contract_version`` is carried from ``committed`` untouched.
    """
    if "min_supported_contract_version" not in committed:
        raise FixtureGenerationError(
            "committed fixture carries no min_supported_contract_version. It is "
            "DoE-owned and hand-maintained; this generator preserves it and will "
            "not derive one. Restore the field before regenerating."
        )
    preserved_min = committed["min_supported_contract_version"]
    target_version = envelope_schema.get("version")
    if not target_version:
        raise FixtureGenerationError(
            "envelope schema declares no 'version' -- cannot determine the target "
            "contract version, and this generator will not fall back to the "
            "package's own CONTRACT_VERSION."
        )

    # `contract_version` and `min_supported_contract_version` are fixture METADATA,
    # not envelope fields -- snapshot-envelope declares neither and is
    # `additionalProperties: false`, so conforming or validating the envelope with
    # them attached fails. They are stripped for the envelope pass and reattached
    # after, which is also why validate_body() below validates the envelope portion
    # alone. Discovered by the pre-write validation refusing the first run.
    envelope_input = {
        k: v for k, v in committed.items() if k not in _FIXTURE_METADATA_KEYS
    }
    body = _conform(envelope_input, envelope_schema, "<root>")
    if "schema_version" in envelope_schema.get("properties", {}):
        body["schema_version"] = target_version
    body["contract_version"] = target_version
    body["min_supported_contract_version"] = preserved_min
    return body


def envelope_portion(body: dict) -> dict:
    """The part of the fixture the envelope schema governs -- metadata removed."""
    return {k: v for k, v in body.items() if k not in _FIXTURE_METADATA_KEYS}


def render(body: dict) -> str:
    """Serialize to the committed file's conventions. No sort_keys -- see docstring."""
    return json.dumps(body, indent=2, ensure_ascii=False) + "\n"


def generate(
    doe_clone: Optional[str] = None,
    schema_dir: Optional[str] = None,
    write: bool = False,
) -> tuple[str, dict]:
    """Regenerate the fixture. Returns (rendered_text, body).

    Validates before writing; an invalid body is raised, never written.
    """
    clone = resolve_doe_clone(doe_clone)
    fixture_path = clone / _FIXTURE_REL
    if not fixture_path.exists():
        raise FixtureGenerationError(
            f"committed fixture not found at {fixture_path}. There is no "
            "min_supported_contract_version to preserve, so this generator "
            "refuses rather than inventing the whole document."
        )

    schema_root = (
        Path(schema_dir).resolve()
        if schema_dir
        else Path(__file__).resolve().parents[2]
        / "ops"
        / "emit"
        / "_vendor"
        / "cockpit-contract"
        / "schema"
    )
    envelope_schema = json.loads(
        (schema_root / _ENVELOPE_SCHEMA_NAME).read_text(encoding="utf-8")
    )

    committed = json.loads(fixture_path.read_text(encoding="utf-8"))
    body = build_fixture(committed, envelope_schema)

    errors = sorted(
        _validator_for(envelope_schema).iter_errors(envelope_portion(body)),
        key=lambda e: list(e.path),
    )
    if errors:
        rendered_errors = "\n".join(
            f"  {'.'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
            for e in errors[:20]
        )
        raise FixtureGenerationError(
            f"generated fixture does not validate against "
            f"{envelope_schema.get('version')} ({len(errors)} error(s)); refusing "
            f"to write it:\n{rendered_errors}"
        )

    text = render(body)
    if write:
        fixture_path.write_text(text, encoding="utf-8", newline="\n")
    return text, body


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coordinator-emit-conformance-fixture",
        description=(
            "Regenerate DoE's emission-conformance fixture from the committed one, "
            "conformed to a target schema bundle. Preserves "
            "min_supported_contract_version, which is DoE-owned."
        ),
    )
    parser.add_argument("--doe-clone", default=None, help="Path to the DoE clone.")
    parser.add_argument(
        "--schema-dir",
        default=None,
        help="Schema directory to target. Defaults to claude-klabauter's vendored bundle; "
        "point it at DoE's schema/ to regenerate for their pin.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the fixture. Without this, prints a summary and writes nothing.",
    )
    args = parser.parse_args(argv)

    try:
        text, body = generate(args.doe_clone, args.schema_dir, write=args.write)
    except FixtureGenerationError as exc:
        print(f"emit_conformance_fixture: {exc}", file=sys.stderr)
        return 1

    print(
        f"emit_conformance_fixture: contract_version={body.get('contract_version')} "
        f"min_supported_contract_version={body.get('min_supported_contract_version')} "
        f"({len(text)} bytes){' -- WRITTEN' if args.write else ' -- dry run'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

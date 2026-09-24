"""coordinator_core.hooks.guard_handoff_summary_cap_on_write — PreToolUse
(Write|Edit|MultiEdit) advisory op: warns on a write that would leave a
handoff's `summary:` frontmatter field over its 140-char cap.

Arrival note (W4-C7, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/guard-handoff-summary-cap-on-
write.py`. That script ran as an in-process guard body enrolled into a
second, doctrine-plane-resident guard registry fired only via
`preuse-write-dispatch.py`'s own dispatch. None of that applies here: this
lands as its own `hooks.<name>` op per this row's body.

Why this exists (unchanged from the source): a normalizer caps `summary:`
to 140 chars only at handoff CREATION; a later hand-edit runs entirely
after that normalizer and can push it back over-cap, which `pickup-assemble
apply` then refuses outright — a surface owned by whoever consumes the
handoff rather than whoever wrote it. This guard is the authoring-time
correction: flag the over-cap value at the moment it is typed, as advisory
context alongside the write. The write proceeds either way — the
downstream consumer's own refusal is the backstop that actually blocks an
over-cap claim; this guard deliberately does not duplicate that as a
second block at authoring time.

Schema is the source of the cap, not a local guess: `coordinator/schemas/
handoff.schema.json`'s `summary` property description states the cap in
prose ("One-line session summary (<=140 chars)"); the schema itself carries
no `maxLength`. `_HANDOFF_SUMMARY_CAP` below is that same 140, transcribed
once, at the one place this guard needs it.

Scope, mirroring `guard_python_syntax_on_write.py`: a `.md` file under
`state/handoffs/` (this repo's own live handoff directory). Archived
handoffs (`state/handoffs/archive/`) are NOT excluded on purpose — a
hand-edit to an archived file's frontmatter is the same class of defect.

Fail-open (returns `no_advisory()`), in order: `tool_name` not in the
guarded set; no target path in `tool_input`; target not a `.md` file under
`state/handoffs/`; on-disk read failure for an existing file; ambiguous
before/after reconstruction; no frontmatter fence in the reconstructed
after-text; unparseable YAML frontmatter (PyYAML unavailable included); no
`summary` key, or a non-string `summary` value; a `summary` at or under
the cap.

Warn message: names the actual length and the cap — the reader is mid-edit
on the exact file the count is wrong in, so no wiki anchor is spent
sending them elsewhere.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C7
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.hooks._envelope import allow_advisory, no_advisory, payload_of
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.hooks.support.sentinel_write_guard import extract_target_path
from coordinator_core.ipc import register_op
from coordinator_core.write_guards._sentinel_write_guard import reconstruct_after

try:
    import yaml
except Exception:  # pragma: no cover -- exercised only in a PyYAML-less env
    yaml = None  # type: ignore[assignment]

_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit")

#: Directory substring, not a suffix restriction beyond `.md` -- matches
#: `state/handoffs/` anywhere in the resolved path, including the
#: `archive/` subtree.
_SCOPE_DIR = "state/handoffs/"

#: Transcribed from `coordinator/schemas/handoff.schema.json`'s `summary`
#: property description ("One-line session summary (<=140 chars)").
_HANDOFF_SUMMARY_CAP = 140


def is_in_scope(target: Path) -> bool:
    """A `.md` file somewhere under a `state/handoffs/` directory."""
    if target.suffix != ".md":
        return False
    posix = target.as_posix()
    return _SCOPE_DIR in posix or posix.endswith(_SCOPE_DIR.rstrip("/"))


def _split_frontmatter(text: str) -> "tuple[dict | None, str]":
    """Split a `---\\n<yaml>\\n---\\n<body>` document into
    `(frontmatter_dict, body)`. Returns `(None, text)` on ANY shape
    mismatch — fail-open."""
    if not text.startswith("---"):
        return None, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, text
    fm_text, body = parts[1], parts[2]
    if body.startswith("\n"):
        body = body[1:]
    if yaml is None:
        return None, text
    try:
        fm = yaml.safe_load(fm_text)
    except Exception:
        return None, text
    if not isinstance(fm, dict):
        return None, text
    return fm, body


def _warn_reason(target: str, length: int) -> str:
    """The prose diagnosis (the only part `message_envelope.CEILING`
    counts)."""
    name = Path(target).name
    return (
        f"{name}: summary is {length} chars, over the {_HANDOFF_SUMMARY_CAP}-char "
        "schema cap. Write proceeds; fix on the next edit."
    )


@register_op("hooks.guard_handoff_summary_cap_on_write")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Write|Edit|MultiEdit) op: advise (never deny) when a
    write leaves a handoff's `summary:` frontmatter over its 140-char
    cap."""
    params = payload_of(params)
    if params.get("tool_name", "") not in _GUARDED_TOOLS:
        return no_advisory()

    tool_input = params.get("tool_input")
    if not isinstance(tool_input, dict):
        return no_advisory()

    target_raw = extract_target_path(tool_input)
    if not target_raw:
        return no_advisory()

    try:
        target = Path(target_raw).resolve()
    except Exception:
        return no_advisory()

    if not is_in_scope(target):
        return no_advisory()

    try:
        before = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    except Exception:
        return no_advisory()

    after = reconstruct_after(params.get("tool_name", ""), tool_input, before)
    if after is None:
        return no_advisory()

    fm, _body = _split_frontmatter(after)
    if fm is None:
        return no_advisory()

    summary = fm.get("summary")
    if not isinstance(summary, str):
        return no_advisory()

    length = len(summary)
    if length <= _HANDOFF_SUMMARY_CAP:
        return no_advisory()

    context = render(compose(_warn_reason(target_raw, length)))
    return allow_advisory("PreToolUse", context)

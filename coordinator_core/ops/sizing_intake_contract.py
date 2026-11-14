"""The intake-sizing contract: the record `sizing.intake_mint` writes and engine-ask reads.

Answers "what did the EM direct for this run" (`read_em_brief`) and "is this sizing the
EM's intake surface" (`is_intake`). No clock, no git; only `normalize_machine_path` reads the
settings home and the fleet registry.

The brief rides `em_analysis.em_brief` because the sizing-object schema is vendored and a
new top-level field is not ours to add. `premise.provenance` is a closed enum, so the intake
mark rides `em_analysis.intake` instead.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Iterable, Mapping, Optional

EM_BRIEF_KEY = "em_brief"
EM_CONTEXT_PATHS_KEY = "em_context_paths"
EM_CONTEXT_DIGEST_PARAM = "em_context_digest"
EM_CONTEXT_MARKER = "EM context (not the PM's words):"
_LINE_REF = re.compile(r":\d+$")
INTAKE_MARK_KEY = "intake"
INTAKE_MARK = "sizing.intake_mint"

_NAME_MAX = 60


def _em_analysis(doc: Mapping) -> Mapping:
    analysis = doc.get("em_analysis") if isinstance(doc, Mapping) else None
    return analysis if isinstance(analysis, Mapping) else {}


def read_em_brief(doc: Mapping) -> Optional[str]:
    """The stripped `em_analysis.em_brief`, or None when absent or blank."""
    value = _em_analysis(doc).get(EM_BRIEF_KEY)
    if not isinstance(value, str):
        return None
    return value.strip() or None


def em_context_digest(text: str) -> str:
    """sha256 hex of `text.strip()`: the identity of an EM-context block as `with_em_context` writes it."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def with_em_context(brief: Optional[str], text: str) -> str:
    """`brief` with the EM's context appended under its own marker; the ask text is never touched."""
    block = f"{EM_CONTEXT_MARKER}\n{text.strip()}"
    return f"{brief}\n\n{block}" if brief else block


def is_path_shaped(item: str) -> bool:
    """A single whitespace-free token carrying a separator or an extension; anything else is prose.
    A URL or a `file:LINE` reference names no file on disk, so it is prose: the emit refuses a context path that is missing."""
    text = item.strip()
    if not text or any(ch.isspace() for ch in text) or "://" in text or _LINE_REF.search(text):
        return False
    return "/" in text or "\\" in text or "." in text


_SETTINGS_TOKEN = "~/.coordinator-claude-settings"
SCRATCHPAD_TOKEN = "<scratchpad>"
# `<tempdir>/claude/<project-slug>/<session-id>/scratchpad/<rest>`; `ask_brief._context_path` resolves the token back.
_SCRATCHPAD = re.compile(r"(?:^|/)claude/[^/]+/[^/]+/scratchpad/(.+)$")


def _norm(path: str) -> str:
    return os.path.normcase(path).replace("\\", "/").rstrip("/")


def _normalization_roots(repo_root: Optional[Path | str]) -> list[tuple[str, str]]:
    """(normalized root, written token) pairs; a root that cannot be resolved is skipped, never raised."""
    roots: list[tuple[str, str]] = []
    if repo_root is not None:
        roots.append((_norm(str(repo_root)), ""))
    try:
        from coordinator_core._settings_home import settings_home

        roots.append((_norm(str(settings_home())), _SETTINGS_TOKEN))
    except Exception:
        pass
    try:
        from coordinator_core import _fleet_names

        for raw in _fleet_names._registry_paths():
            name = _fleet_names._leaf(raw)
            if name:
                roots.append((_norm(raw), f"<repo:{name}>"))
    except Exception:
        pass
    return [(r, t) for r, t in roots if r]


def normalize_machine_path(
    path: str, repo_root: Optional[Path | str] = None, roots: Optional[list[tuple[str, str]]] = None,
) -> str:
    """`path` with its machine root replaced by a portable token; a path under no known root is returned as-is.

    Under `repo_root` -> repo-relative; under the settings home -> `~/.coordinator-claude-settings/...`;
    under a registry fleet checkout -> `<repo:NAME>/...`. The longest matching root wins. `roots` is a
    precomputed `_normalization_roots` result: a caller normalizing many paths passes it so the fleet
    registry is read once.
    """
    scratch = _SCRATCHPAD.search(path.replace("\\", "/"))
    if scratch:
        return f"{SCRATCHPAD_TOKEN}/{scratch.group(1)}"
    probe = _norm(path)
    best: Optional[tuple[str, str]] = None
    for root, token in (roots if roots is not None else _normalization_roots(repo_root)):
        if (probe == root or probe.startswith(root + "/")) and (best is None or len(root) > len(best[0])):
            best = (root, token)
    if best is None:
        return path
    rest = path.replace("\\", "/").rstrip("/")[len(best[0]):].lstrip("/")
    if not best[1]:
        return rest or "."
    return f"{best[1]}/{rest}" if rest else best[1]


def em_context_paths_value(paths: Iterable[str], repo_root: Optional[Path | str] = None) -> Optional[str]:
    """The stripped non-empty paths, one per line, as the single string the sizing schema admits; None when none remain.

    Each path is written through `normalize_machine_path`, so the sizing never carries a machine root."""
    stripped = [p.strip() for p in paths if isinstance(p, str) and p.strip()]
    roots = _normalization_roots(repo_root) if stripped else []
    kept = [normalize_machine_path(p, repo_root, roots) for p in stripped]
    return "\n".join(kept) or None


def read_em_context_paths(doc: Mapping) -> tuple[str, ...]:
    """The context paths on `em_analysis.em_context_paths`; () when the key is absent or not a string."""
    value = _em_analysis(doc).get(EM_CONTEXT_PATHS_KEY)
    if not isinstance(value, str):
        return ()
    return tuple(line.strip() for line in value.split("\n") if line.strip())


def is_intake(doc: Mapping) -> bool:
    """True for a sizing carrying the mint's mark and still at `draft`."""
    if not isinstance(doc, Mapping):
        return False
    return (_em_analysis(doc).get(INTAKE_MARK_KEY) == INTAKE_MARK
            and doc.get("status") == "draft")


def needs_size_stage(doc: Mapping) -> bool:
    """True when the estimate is unsettled: an intake mint or `status: draft`.

    `estimate.provisional` is not a signal: a routed or sized sizing keeps it true and is settled by its route.
    """
    if not isinstance(doc, Mapping):
        return False
    return is_intake(doc) or doc.get("status") == "draft"


PROVENANCE_KEY = "provenance"
PROVENANCE_RELAYED = "relayed"
RELAYED_TEXT_KEY = "relayed_text"
RELAYED_SENDER_KEY = "relayed_sender"


def relayed_intake_record(*, name: str, text: str, sender: str, today: str) -> dict:
    """The schema-valid sizing dict for an ask that is a peer's, not the PM's words.

    No `pm_verbatims` row is written: a relayed ask never fabricates the PM's voice. The
    text and sender ride `em_analysis.relayed_text` / `relayed_sender`; `intent_source` is
    `em-elaborated`.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("relayed ask carries no text")
    record = intake_record(name=name, turn={"text": text}, today=today)
    record["intent_source"] = "em-elaborated"
    del record["pm_verbatims"]
    record["em_analysis"][PROVENANCE_KEY] = PROVENANCE_RELAYED
    record["em_analysis"][RELAYED_TEXT_KEY] = text.strip()
    record["em_analysis"][RELAYED_SENDER_KEY] = (sender or "").strip() or "unknown"
    return record


def is_relayed(doc: Mapping) -> bool:
    """True for a sizing whose ask was relayed rather than the PM's own words."""
    return _em_analysis(doc).get(PROVENANCE_KEY) == PROVENANCE_RELAYED


def intake_record(*, name: str, turn: Mapping, today: str) -> dict:
    """The schema-valid sizing dict for one PM turn.

    `turn` is a PM-turn row: `text` (or the log's `prompt`), optional `turn` and `ts`.
    The estimate and route are placeholders, not a sizing judgment: the run's size stage
    judges them. `em_brief` is not written here; the caller adds it only when non-blank.
    """
    text = turn.get("text", turn.get("prompt"))
    if not isinstance(text, str) or not text.strip():
        raise ValueError("turn carries no text")
    verbatim: dict = {"text": text}
    index = turn.get("turn")
    if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
        verbatim["turn"] = index
    if "ts" in turn:
        verbatim["ts"] = turn["ts"]
    record: dict = {
        "schema": "sizing-object",
        "name": (name or "").strip()[:_NAME_MAX] or "intake",
        "intent": text,
        "intent_source": "pm-verbatim",
        "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan",
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "draft",
        "premise": {
            "provenance": "not-applicable",
            "evidence": f"intake sizing minted {today} by {INTAKE_MARK}; "
                        "no premise judged until the size stage runs",
        },
        "pm_verbatims": [verbatim],
        "em_analysis": {INTAKE_MARK_KEY: INTAKE_MARK},
    }
    return record

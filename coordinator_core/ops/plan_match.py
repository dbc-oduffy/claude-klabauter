"""
coordinator_core.ops.plan_match — JSON-RPC "plan.match_candidates" operation.

Purpose: Read-only resolver that ranks the calling repo's ``docs/plans/*.md``
plan documents by fuzzy similarity of a caller-supplied ``text`` against each
plan's title.  Returns a ranked ``candidates`` list of ``{plan_id, title, score}``
dicts — the score is a blended float (0.0–1.0) combining 70% ``difflib.SequenceMatcher``
character-level ratio and 30% keyword token overlap, rounded to 4 decimals.  Powers the
offer-shaped "which plan did you mean?" picker at fork-authoring time.

COMPUTE_ONLY — this op reads ``docs/plans/*.md`` frontmatter and returns a computed
ranked list; it issues no git command and mutates no coordinator substrate.  Its one write is
the derived title index ``plan-match-index.json`` under the machinery cache dir (path+mtime+size
keyed, atomic, rebuildable), so a fresh process need not re-parse ~2.7k plan files.  The COMPUTE_ONLY invariant (DR-208) is enforced by classification in
``coordinator_core/authz/classification.py``.

Self-registration: importing this module calls
``register_op("plan.match_candidates", _handler)`` as a side-effect.
Add this module to ``coordinator_core/ops/__init__.py`` to trigger registration
at start_server() time.

Read-path: frontmatter is extracted and parsed via a minimal scalar-only approach
(the ``_parse_frontmatter`` flat helper is NOT used; we do our own split on ``---``
and extract title/plan_id with ``yaml.safe_load`` to handle quoted values).
Ranking delegates to ``coordinator_core.ops.match_core.rank_candidates``
(stdlib ``difflib.SequenceMatcher`` only — no third-party fuzzy library added).

Worktree resolution mirrors ``goals_match.py`` and ``initiatives_serve.py``:
  - When ``repo_root`` is provided (router-supplied git common dir), the worktree
    root is derived via ``main_worktree_root(repo_root)``.
  - If ``repo_root`` is absent the op returns ``{"candidates": []}`` with a logged
    warning rather than raising — empty is safe for a picker nudge.

Spec backlink: pln-claude-klabauter-fork-provenance-creatio-01c09f § C2
Sibling: coordinator_core/ops/handoff_match.py, coordinator_core/ops/goals_match.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import List, Optional

import yaml

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.match_core import QuarantineLog, rank_candidates
from coordinator_core.session.machinery_paths import cache_dir as machinery_cache_dir

_LOG = logging.getLogger(__name__)


_HEAD_CHUNK = 8192
_open_binary = open
_FM_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

#: invariant: path -> ((mtime_ns, size), entry); entry is ("item", id, title) | ("skip", reason, detail),
#: with a trailing True when the regex read (not YAML) produced it. A warm engine re-reads only the
#: plans that changed.
_ENTRY_CACHE: dict = {}
#: index_path -> file names its on-disk copy holds; set on first load per process.
_DISK_NAMES: dict = {}

_INDEX_FILENAME = "plan-match-index.json"
#: invariant: process-time spent in YAML per scan; past it a miss takes the regex read and is
#: re-verified by YAML on a later scan, so a cold scan of ~2.7k plans (1.2s of YAML) stays bounded.
_YAML_BUDGET_S = 0.02

_SQ_RE = re.compile(r"^'([^']*)'$")
_DQ_RE = re.compile(r'^"([^"\\]*)"$')
_PLAIN_BAD_RE = re.compile(r"""^[-?:,\[\]{}#&*!|>'"%@`]|: | #|:$""")
_NONSTR_RE = re.compile(
    r"^(~|null|true|false|yes|no|on|off|y|n|[-+]?[0-9][0-9_.,:eE+-]*|\.inf|\.nan|0x[0-9a-f]+|0o[0-7]+)$",
    re.IGNORECASE,
)


def _frontmatter_text(fpath) -> Optional[str]:
    """The text between the first two `---\n` delimiters (to EOF when unclosed), or None when the
    file does not open with one. Reads the head only: plan bodies are never needed."""
    head = b""
    with _open_binary(fpath, "rb") as fh:
        while True:
            chunk = fh.read(_HEAD_CHUNK)
            head += chunk
            norm = head.replace(b"\r\n", b"\n")
            if len(norm) >= 4 and not norm.startswith(b"---\n"):
                return None
            end = norm.find(b"---\n", 4)
            if end != -1:
                return norm[4:end].decode("utf-8")
            if not chunk:
                return norm[4:].decode("utf-8") if norm.startswith(b"---\n") else None


def _entry_from_fm(fm, stem: str) -> tuple:
    if not isinstance(fm, dict):
        return ("skip", "YAML did not produce a dict", None)
    title_val = fm.get("title")
    if not isinstance(title_val, str) or not title_val:
        return ("skip", "missing required field: title", None)
    plan_id_val = fm.get("plan_id")
    if not isinstance(plan_id_val, str) or not plan_id_val:
        plan_id_val = stem
    return ("item", plan_id_val, title_val)


def _fast_fm(fm_text: str) -> Optional[dict]:
    """title/plan_id read by regex when each is a simple one-line scalar; None sends the file to YAML."""
    out: dict = {}
    text = "\n" + fm_text
    for key in ("title", "plan_id"):
        at = text.find("\n" + key + ":")
        if at == -1:
            continue
        start = at + len(key) + 2
        end = text.find("\n", start)
        end = len(text) if end == -1 else end
        if text.find("\n" + key + ":", end) != -1:
            return None
        if start < end and text[start] not in " \t":
            return None
        val = text[start:end].strip(" \t")
        quoted = _SQ_RE.match(val) or _DQ_RE.match(val)
        if quoted:
            val = quoted.group(1)
        elif not val or _PLAIN_BAD_RE.search(val) or _NONSTR_RE.match(val):
            return None
        out[key] = val
    return out


def _plan_entry(fpath: str, stem: str, allow_fast: bool = False) -> tuple:
    try:
        fm_text = _frontmatter_text(fpath)
        if fm_text is None:
            return ("skip", "no YAML frontmatter block", None)
        if allow_fast:
            fast = _fast_fm(fm_text)
            if fast is not None and fast.get("title"):
                return _entry_from_fm(fast, stem) + (True,)
        fm = yaml.load(fm_text, Loader=_FM_LOADER)
    except Exception as exc:  # noqa: BLE001 — parity with goals_match quarantine pattern
        return ("skip", "parse error", str(exc))
    return _entry_from_fm(fm, stem)


def _load_disk(index_path: str, plans_dir: Path) -> None:
    names: set = set()
    prefix = str(plans_dir) + os.sep
    try:
        with open(index_path, "rb") as fh:
            blob = json.loads(fh.read())
        if blob["v"] == 1:
            for name, row in blob["e"].items():
                _ENTRY_CACHE[prefix + name] = ((row[0], row[1]), tuple(row[2:]))
                names.add(name)
    except Exception:  # noqa: BLE001 — absence/corruption reads as an empty cache
        names = set()
    _DISK_NAMES[index_path] = names


def _save_disk(index_path: str, plans_dir: Path, names: List[str]) -> None:
    prefix = str(plans_dir) + os.sep
    try:
        rows = {}
        for name in names:
            cached = _ENTRY_CACHE.get(prefix + name)
            if cached is not None:
                rows[name] = [cached[0][0], cached[0][1], *cached[1]]
        data = json.dumps({"v": 1, "e": rows}, separators=(",", ":")).encode("utf-8")
        os.makedirs(os.path.dirname(index_path), exist_ok=True)
        atomic_write_bytes(index_path, data)
        _DISK_NAMES[index_path] = set(rows)
    except Exception as exc:  # noqa: BLE001 — a cache that cannot persist only costs the next cold scan
        _LOG.debug("plan.match_candidates: index not written: %s", exc)


def _collect_plans(plans_dir: Path, cache_dir: Optional[str] = None) -> List[dict]:
    items: List[dict] = []

    if not plans_dir.is_dir():
        return items

    index_path = os.path.join(cache_dir, _INDEX_FILENAME) if cache_dir else None
    if index_path and index_path not in _DISK_NAMES:
        _load_disk(index_path, plans_dir)

    quarantine = QuarantineLog("plan.match_candidates", _LOG)
    stamps: List[tuple] = []
    try:
        with os.scandir(plans_dir) as it:
            for de in it:
                if not de.name.lower().endswith(".md"):
                    continue
                try:
                    st = de.stat()
                except OSError as exc:
                    quarantine.scanned()
                    quarantine.skip(de.name, "parse error", str(exc))
                    continue
                stamps.append((de.name, (st.st_mtime_ns, st.st_size)))
    except OSError:
        return items
    stamps.sort()

    prefix = str(plans_dir) + os.sep
    yaml_start = time.process_time()
    dirty = False
    for name, stamp in stamps:
        quarantine.scanned()
        key = prefix + name
        cached = _ENTRY_CACHE.get(key)
        entry = cached[1] if cached is not None and cached[0] == stamp else None
        over = time.process_time() - yaml_start >= _YAML_BUDGET_S
        if entry is None or (len(entry) == 4 and not over):
            entry = _plan_entry(key, name[:-3], allow_fast=over)
            _ENTRY_CACHE[key] = (stamp, entry)
            dirty = True
        if entry[0] == "skip":
            quarantine.skip(name, entry[1], entry[2])
            continue
        items.append({"id": entry[1], "title": entry[2], "text": entry[2].lower()})

    if index_path and (dirty or _DISK_NAMES[index_path] != {n for n, _ in stamps}):
        _save_disk(index_path, plans_dir, [n for n, _ in stamps])

    quarantine.summarize()
    return items


@register_op("plan.match_candidates")
def _handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC "plan.match_candidates" handler.

    Returns plans from ``docs/plans/*.md`` ranked by fuzzy similarity of
    ``params["text"]`` against each plan's title.

    Params:
        text (str): The string to match — e.g. a plan name fragment.
                    If absent or empty, returns ``{"candidates": []}``.
        repo (str): Accepted and logged; path resolution uses ``repo_root``
                    (the router-supplied git common dir) NOT ``repo``.
                    Negative-spec: claude-klabauter ops resolve paths from the router-supplied
                    repo_root — there is no cross-repo path registry in this op.

    Returns:
        {
            "candidates": [
                {"plan_id": str, "title": str, "score": float},
                ...
            ]
        }

    Candidates are sorted by score DESCENDING, tie-broken by plan_id ASCENDING.
    Score is a blended float (0.7 * SequenceMatcher ratio + 0.3 * keyword overlap,
    rounded to 4 decimals, range 0.0–1.0).

    Worktree resolution (mirrors goals_match.py / initiatives_serve.py):
    - repo_root (router-supplied git common dir) → main_worktree_root(repo_root)
    - None → return empty candidates with logged warning (empty is safe for a picker)
    """
    text = params.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return {"candidates": []}

    if repo_root is not None:
        worktree_root = main_worktree_root(repo_root)
    else:
        _LOG.warning(
            "plan.match_candidates: no repo_root resolved — "
            "repo_root arg absent; returning empty candidates"
        )
        return {"candidates": []}

    plans_dir = worktree_root / "docs" / "plans"
    raw = rank_candidates(
        text, _collect_plans(plans_dir, machinery_cache_dir(str(worktree_root)))
    )
    candidates = [
        {"plan_id": entry["id"], "title": entry["title"], "score": entry["score"]}
        for entry in raw
    ]

    return {"candidates": candidates}

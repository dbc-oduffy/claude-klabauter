"""
coordinator_core.ops.ceremony.wiki_update_candidates -- JSON-RPC
"ceremony.wiki_update_candidates": which wiki pages a /update-docs run hands a model.

Purpose: one deterministic, read-only query (no model call) that joins the plans completed in
the window to the wiki pages citing their spine `writes:` paths, ranks pages by the highest
contributing sizing tshirt, and previews each plan from git. Contract:
Coordinator-content-repo `coordinator/docs/contracts/wiki-update-candidates.md` (reply schema
`wiki-update-candidates`, version 1).

Window: `since` param, else the `ack_head` of `ceremony.update_docs_worklist`'s cache (read,
never advanced), else the last `DEFAULT_WINDOW_DAYS` days; a sha git does not know falls back
to the day window and says so in `window.source`.

Spawns, constant in plan count: rev-parse, one path-limited `git log`, one `cat-file --batch`
over every candidate (`<commit>:<plan>`), one fixed-string `git grep -f -` over the wiki.

Self-registration: importing this module registers the op; `ops/__init__.py` imports it at
start_server() time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.git.run import run_git
from coordinator_core.ipc import CEREMONY_BUDGET_SECS, register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.ceremony import update_docs_worklist as _worklist

SCHEMA = "wiki-update-candidates"
SCHEMA_VERSION = 1
PREVIEW_CHARS = 10000
PAGES_CAP = 10
PLANS_CAP = 20
DEFAULT_WINDOW_DAYS = 7

PLAN_DIR = "docs/plans"
WIKI_PATHSPECS = ("coordinator/docs/wiki/*.md", "docs/wiki/*.md")

SOURCE_ACK = "ack_head"
SOURCE_SINCE = "since-param"
SOURCE_DEFAULT = "default-7d"

_TSHIRT_RANK = {"XL": 5, "L": 4, "M": 3, "S": 2, "XS": 1}
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_WRITES_RE = re.compile(r"^\s*(?:-\s+)?writes:[ \t]*(.*)$")
_ITEM_RE = re.compile(r"^\s*-\s+(\S.*?)\s*$")
_FM_STATUS_RE = re.compile(r"^status:[ \t]*(.*?)[ \t]*$", re.M)
_FM_SIZING_RE = re.compile(r"^sizing_object:[ \t]*(.*?)[ \t]*(?:#.*)?$", re.M)
_TSHIRT_LINE_RE = re.compile(r"^\s+tshirt:[ \t]*['\"]?(\w+)")


def _unquote(value: str) -> str:
    return value.strip().strip("\"'").strip()


def _frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return ""
    close = re.search(r"^---[ \t]*$", text[3:], re.M)
    return text[3 : 3 + close.start()] if close else ""


def _spine_writes(text: str) -> List[str]:
    """Repo-relative `writes:` paths of the `plan-tasks` fenced block, forward slashes, in order."""
    paths: List[str] = []
    in_block = False
    pending = False
    for line in text.splitlines():
        if not in_block:
            in_block = line.lstrip().startswith("```") and "plan-tasks" in line
            continue
        if line.lstrip().startswith("```"):
            break
        item = _ITEM_RE.match(line)
        if pending and item:
            raw = [_unquote(item.group(1))]
        else:
            pending = False
            m = _WRITES_RE.match(line)
            if not m:
                continue
            rest = m.group(1).strip()
            if rest.startswith("["):
                raw = [_unquote(p) for p in rest.strip("[]").split(",")]
            else:
                pending = not rest
                continue
        for p in raw:
            p = p.replace("\\", "/")
            p = p[2:] if p.startswith("./") else p
            if p and "\n" not in p and p not in paths:
                paths.append(p)
    return paths


def _tshirt(root: Path, fm: str) -> Optional[str]:
    """`estimate.tshirt` of the plan's `sizing_object` yaml, None when absent or unreadable."""
    m = _FM_SIZING_RE.search(fm)
    rel = _unquote(m.group(1)) if m else ""
    if not rel or rel == "null":
        return None
    try:
        lines = (root / rel).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    in_estimate = False
    for line in lines:
        if line.startswith("estimate:"):
            in_estimate = True
        elif in_estimate:
            if line and not line[0].isspace():
                break
            t = _TSHIRT_LINE_RE.match(line)
            if t:
                value = t.group(1).upper()
                return value if value in _TSHIRT_RANK else None
    return None


def _rank(tshirt: Optional[str]) -> int:
    return _TSHIRT_RANK.get(tshirt or "", 0)


def _window(root: Path, since: Optional[str]) -> Tuple[Optional[str], str]:
    """(baseline sha or None, source) before git has confirmed the sha."""
    if since:
        if _SHA_RE.match(since):
            return since, SOURCE_SINCE
        return None, SOURCE_DEFAULT
    ack = _worklist._load_cache(_worklist._cache_path(root)).get("ack_head")
    if isinstance(ack, str) and _SHA_RE.match(ack):
        return ack, SOURCE_ACK
    return None, SOURCE_DEFAULT


def _completing_commits(root: Path, since: Optional[str]) -> Optional[List[Tuple[str, str]]]:
    """(plan path, newest in-window commit touching its `status:` line), newest commit first;
    None when git rejects the range."""
    args = ["log", "--no-renames", "--diff-filter=AM", "--name-only", "--format=%x01%H", "-G^status:"]
    args.append(f"{since}..HEAD" if since else f"--since={DEFAULT_WINDOW_DAYS}.days.ago")
    res = run_git(args + ["--", PLAN_DIR], cwd=str(root), timeout=CEREMONY_BUDGET_SECS)
    if not res.ok:
        return None
    seen: set = set()
    out: List[Tuple[str, str]] = []
    sha = ""
    for line in res.stdout.splitlines():
        if line.startswith("\x01"):
            sha = line[1:].strip()
        elif line.endswith(".md") and sha and line not in seen:
            seen.add(line)
            out.append((line, sha))
    return out


def _read_blobs(root: Path, specs: List[Tuple[str, str]]) -> List[str]:
    """Text of each (plan, commit) via one `cat-file --batch`; "" when unreadable."""
    if not specs:
        return []
    wanted = "".join(f"{sha}:{plan}\n" for plan, sha in specs).encode("utf-8")
    res = run_git(["cat-file", "--batch"], cwd=str(root), timeout=CEREMONY_BUDGET_SECS, input=wanted)
    data, pos = res.stdout_bytes, 0
    texts: List[str] = []
    for _ in specs:
        eol = data.find(b"\n", pos)
        if eol < 0:
            texts.append("")
            continue
        header = data[pos:eol].split()
        pos = eol + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            texts.append(data[pos : pos + size].decode("utf-8", errors="replace"))
            pos += size + 1
        else:
            texts.append("")
    return texts


def _citing_pages(root: Path, paths: List[str]) -> Dict[str, List[str]]:
    """{wiki page: touched paths its text cites}, from ONE fixed-string `git grep -f -`."""
    if not paths:
        return {}
    res = run_git(
        ["grep", "-F", "-f", "-", "--null", "-I", "--no-color", "--", *WIKI_PATHSPECS],
        cwd=str(root),
        timeout=CEREMONY_BUDGET_SECS,
        input="\n".join(paths).encode("utf-8") + b"\n",
    )
    cites: Dict[str, set] = {}
    for line in res.stdout.splitlines():
        page, sep, text = line.partition("\0")
        if not sep:
            continue
        hit = cites.setdefault(page, set())
        hit.update(p for p in paths if p in text)
    return {page: sorted(hit) for page, hit in cites.items() if hit}


def wiki_update_candidates(root: Path, since: Optional[str] = None) -> Dict[str, Any]:
    """The contract reply for the repo at `root`: window, ranked pages, preview total, truncation."""
    base, source = _window(root, since)
    commits = _completing_commits(root, base) if base else None
    if commits is None:
        base, source = None, SOURCE_DEFAULT
        commits = _completing_commits(root, None) or []
    head_res = run_git(["rev-parse", "HEAD"], cwd=str(root), timeout=CEREMONY_BUDGET_SECS)
    head = head_res.stdout.strip() if head_res.ok else None

    plans: List[Dict[str, Any]] = []
    for (plan, sha), text in zip(commits, _read_blobs(root, commits)):
        fm = _frontmatter(text)
        m = _FM_STATUS_RE.search(fm)
        if not m or _unquote(m.group(1)) != "complete":
            continue
        writes = _spine_writes(text)
        if writes:
            plans.append({
                "path": plan, "commit_sha": sha, "tshirt": _tshirt(root, fm),
                "preview": text[:PREVIEW_CHARS], "writes": writes,
            })

    cited = _citing_pages(root, sorted({p for pl in plans for p in pl["writes"]}))
    pages: List[Dict[str, Any]] = []
    for page, paths in cited.items():
        mine = sorted(
            (pl for pl in plans if any(w in paths for w in pl["writes"])),
            key=lambda pl: (-_rank(pl["tshirt"]), pl["path"]),
        )
        if mine:
            pages.append({"page": page, "plans": mine})
    pages.sort(key=lambda pg: (-_rank(pg["plans"][0]["tshirt"]), -len(pg["plans"]), pg["page"]))

    cut_pages = pages[PAGES_CAP:]
    kept = pages[:PAGES_CAP]
    cut_plans = sum(len(pg["plans"]) for pg in cut_pages)
    budget = PLANS_CAP
    for pg in kept:
        take = pg["plans"][:budget]
        cut_plans += len(pg["plans"]) - len(take)
        budget -= len(take)
        pg["plans"] = take
    emptied = [pg for pg in kept if not pg["plans"]]
    kept = [pg for pg in kept if pg["plans"]]

    total = 0
    for pg in kept:
        pg["plans"] = [
            {k: pl[k] for k in ("path", "commit_sha", "tshirt", "preview")} for pl in pg["plans"]
        ]
        total += sum(len(pl["preview"]) for pl in pg["plans"])
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "window": {"since": base, "head": head, "source": source},
        "pages": kept,
        "preview_chars_total": total,
        "truncated": {"pages": len(cut_pages) + len(emptied), "plans": cut_plans},
    }


@register_op("ceremony.wiki_update_candidates")
def _ceremony_wiki_update_candidates(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "ceremony.wiki_update_candidates" handler.

    Params: `since` (commit sha; window is `since..HEAD`, overriding the cached `ack_head`),
    `repo_root` (repo to query; default the invoking repo's main worktree). Returns
    {schema, schema_version, window{since, head, source}, pages[{page, plans[{path, commit_sha,
    tshirt, preview}]}], preview_chars_total, truncated{pages, plans}}. Read-only: writes no file
    and never advances the worklist `ack_head`. Caps: PREVIEW_CHARS, PAGES_CAP, PLANS_CAP.
    """
    p = params or {}
    override = p.get("repo_root")
    if override:
        root = Path(str(override))
        if not root.is_dir():
            raise ValueError(f"ceremony.wiki_update_candidates: repo_root is not a directory: {override!r}")
    elif repo_root is None:
        raise ValueError(
            "ceremony.wiki_update_candidates requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None."
        )
    else:
        root = main_worktree_root(repo_root)
    since = str(p["since"]).strip() if p.get("since") else None
    return wiki_update_candidates(root, since)

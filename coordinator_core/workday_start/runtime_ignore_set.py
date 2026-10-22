"""The template's runtime ignore set, derived and checked against a repo.

`load_template_rules` reads the DoE repo-setup gitignore template's rule lines;
`runtime_ignore_findings` answers which of them the repo's ignore config does not cover
(`missing`) and which index paths they match (`tracked`). Pure reads: no `.gitignore` write,
no index write, no per-rule or per-path git spawn (at most one batched `git check-ignore`).
Also hosts the gitignore parser the sweep and apply share.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from pathlib import Path

from coordinator_core.git.git_state import IndexParseError, read_index_paths  # noqa: F401
from coordinator_core.git.run import run_git

TEMPLATE_REL = Path("skills/repo-setup/templates/gitignore.project.template")

# Memo delivery force-tracks these; neither the sweep nor apply reports or untracks them.
UNTRACK_EXEMPT_PREFIXES: tuple[str, ...] = (".coordinator-local/memo-outbox/",)

_Rule = tuple[bool, bool, bool, "re.Pattern[str]"]  # negated, dir_only, anchored, regex
_GLOB_CHARS = "*?[\\"


@dataclass(frozen=True)
class RuntimeIgnoreFindings:
    missing: tuple[str, ...]
    tracked: tuple[str, ...]


def resolve_template_path() -> Path | None:
    from coordinator_core.subagent_sandbox.provision_report import resolve_plugin_root

    root = resolve_plugin_root()
    if not root:
        return None
    path = Path(root) / TEMPLATE_REL
    return path if path.is_file() else None


def load_template_rules(template: Path) -> tuple[str, ...]:
    rules: list[str] = []
    for raw in Path(template).read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            rules.append(line)
    return tuple(rules)


def _glob_regex(glob: str) -> "re.Pattern[str]":
    out: list[str] = []
    i = 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = glob.find("]", i + 2)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = glob[i + 1 : j]
                if body[:1] == "!":
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        elif c == "\\" and i + 1 < len(glob):
            i += 1
            out.append(re.escape(glob[i]))
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def _parse_rules(text: str) -> list[_Rule]:
    rules: list[_Rule] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        line = re.sub(r"(?<!\\)\s+$", "", line)
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        anchored = "/" in line
        line = line.lstrip("/")
        rules.append((negated, dir_only, anchored, _glob_regex(line)))
    return rules


def _verdict(rules: list[_Rule], rel: str, is_dir: bool) -> bool:
    """Last matching rule wins; no match is not ignored."""
    ignored = False
    for negated, dir_only, anchored, rx in rules:
        if dir_only and not is_dir:
            continue
        target = rel if anchored else rel.rsplit("/", 1)[-1]
        if rx.match(target):
            ignored = not negated
    return ignored


def _covered(rules: list[_Rule], path: str) -> bool:
    """An ignored parent directory ignores everything under it, whatever the file's own rules."""
    parts = path.split("/")
    for n in range(1, len(parts)):
        if _verdict(rules, "/".join(parts[:n]), True):
            return True
    return _verdict(rules, path, False)


def _confirm_ignored(repo_root: Path, paths: list[str]) -> set[str]:
    """One batched `git check-ignore --stdin`; paths it echoes are ignored."""
    done = run_git(
        ["check-ignore", "--stdin"], cwd=str(repo_root), input=("\n".join(paths) + "\n").encode(),
    )
    if done.returncode > 1:
        raise RuntimeError(f"git check-ignore failed ({done.returncode}): {done.stderr.strip()}")
    return {ln.strip() for ln in done.stdout.splitlines() if ln.strip()}


def _split_rule(rule: str) -> tuple[bool, bool, str]:
    """(dir_only, anchored, pattern) with the trailing and leading `/` stripped."""
    dir_only = rule.endswith("/")
    body = rule.rstrip("/")
    anchored = "/" in body
    return dir_only, anchored, body.lstrip("/")


def _probe(rule: str) -> str:
    """A concrete path the rule would ignore, for a coverage test."""
    dir_only, _anchored, body = _split_rule(rule)
    probe = _CLASS_RE.sub(_class_member, body).replace("**/", "a/").replace("*", "x")
    return probe + "/x" if dir_only else probe


_CLASS_RE = re.compile(r"\[(?:[!^]?\]?)[^\]]*\]")


def _class_member(m: "re.Match[str]") -> str:
    """One character the `[...]` class matches; `[!...]` takes a character outside the class."""
    inner = m.group(0)[1:-1]
    if inner[:1] in ("!", "^"):
        return next(c for c in "x0_Z" if c not in inner[1:])
    return inner[1:2] if inner[:1] == "\\" and len(inner) > 1 else inner[:1]


def _literal_prefix(pattern: str) -> str:
    for i, c in enumerate(pattern):
        if c in _GLOB_CHARS:
            return pattern[:i]
    return pattern


def _window(paths: list[str], prefix: str) -> list[str]:
    if not prefix:
        return paths
    lo = bisect.bisect_left(paths, prefix)
    out: list[str] = []
    for i in range(lo, len(paths)):
        if not paths[i].startswith(prefix):
            break
        out.append(paths[i])
    return out


def _match_rule(sorted_paths: list[str], path_set: frozenset, rule: str) -> list[str]:
    dir_only, anchored, body = _split_rule(rule)
    has_glob = any(c in body for c in _GLOB_CHARS)
    if not has_glob:
        if anchored:
            hits = _window(sorted_paths, body + "/")
            if not dir_only and body in path_set:
                hits = [body, *hits]
            return hits
        if dir_only:
            lead = body + "/"
            mid = "/" + lead
            return [p for p in sorted_paths if p.startswith(lead) or mid in p]
        return [p for p in sorted_paths if p.rsplit("/", 1)[-1] == body]
    rx = _glob_regex(body)
    if anchored:
        window = _window(sorted_paths, _literal_prefix(body))
        if dir_only:
            hits = []
            for p in window:
                parts = p.split("/")
                if any(rx.match("/".join(parts[:n])) for n in range(1, len(parts))):
                    hits.append(p)
            return hits
        return [p for p in window if rx.match(p)]
    if dir_only:
        lp = _literal_prefix(body)
        hits = []
        for p in sorted_paths:
            if lp in p and any(rx.match(seg) for seg in p.split("/")[:-1]):
                hits.append(p)
        return hits
    return [p for p in sorted_paths if rx.match(p.rsplit("/", 1)[-1])]


def runtime_ignore_findings(repo_root: Path, rules: tuple[str, ...]) -> RuntimeIgnoreFindings:
    """Raises `IndexParseError` when the index cannot be read."""
    root = Path(repo_root)
    paths = read_index_paths(root)
    try:
        repo_rules = _parse_rules((root / ".gitignore").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        repo_rules = []

    uncovered = [r for r in rules if not _covered(repo_rules, _probe(r))]
    missing: tuple[str, ...] = ()
    if uncovered:
        ignored = _confirm_ignored(root, [_probe(r) for r in uncovered])
        missing = tuple(r for r in uncovered if _probe(r) not in ignored)

    sorted_paths = sorted(paths)
    candidates: set[str] = set()
    for rule in rules:
        candidates.update(_match_rule(sorted_paths, paths, rule))
    effective = repo_rules + _parse_rules("\n".join(missing))
    tracked = tuple(
        p
        for p in sorted(candidates)
        if not p.startswith(UNTRACK_EXEMPT_PREFIXES) and _covered(effective, p)
    )
    return RuntimeIgnoreFindings(missing=missing, tracked=tracked)

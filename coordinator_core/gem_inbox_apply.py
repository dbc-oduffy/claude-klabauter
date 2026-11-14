"""gem-inbox-apply: apply a fleet-inbox-blitz APPLY MANIFEST to its repo.

Manifest keys: repo, closed, left_open, batons, moves, stamps, writes, memos_to_send,
guard, apply_rule, commit. Apply order: pending ledger, writes, baton lint, stamps,
moves, memos_to_send, one scoped commit, ledger delete.

Invariants:
  - The commit is `git commit -m <subject> -- <pathspec>`, never add-then-commit (git add
    fails on a removed move path). Only untracked paths that exist get `add -N`, so
    `commit --only` can see them.
  - Git spawns are constant in the memo count: stamps and moves land through one index
    probe, one intent-to-add and one commit. Stamp, send and lint lines run as one
    in-process batch per phase, never one spawn per line.
  - A guard entry's lines run only while its memo is still open/in_progress.
  - Squash: `reset --mixed BASE` only when BASE..HEAD holds nothing but stamp commits.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from coordinator_core.git.run import run_git

LEDGER_DIR = ".coordinator-local/gem-inbox-pending"
_LIVE_STATUSES = {"open", "in_progress"}
_STAMP_SUBJECT_PREFIXES = ("memo.transition", "archive-stamp-cli")
_BIN_DIR = Path(__file__).resolve().parent.parent / "coordinator" / "bin"
_VERBS = {"archive-stamp-cli", "cross-repo-memo", "lint-frontmatter"}
_STATUS_RE = re.compile(r"^status:\s*['\"]?([A-Za-z_]+)['\"]?\s*$", re.MULTILINE)

ToolRunner = Callable[[Sequence[Sequence[str]], str], "list[int]"]


def run_tools(argvs: Sequence[Sequence[str]], cwd: str) -> "list[int]":
    """Run coordinator bin verbs in-process from `cwd` (no spawn here); one exit code per argv.

    A line whose verb is not in the closed `_VERBS` list gets 127 and runs nothing.
    """
    import importlib.util

    codes: "list[int]" = []
    saved_cwd, saved_env = os.getcwd(), os.environ.get("CLAUDE_PROJECT_DIR")
    added_path = str(_BIN_DIR) not in sys.path
    if added_path:
        sys.path.insert(0, str(_BIN_DIR))
    os.environ["CLAUDE_PROJECT_DIR"] = cwd
    os.chdir(cwd)
    try:
        mods: dict = {}
        for argv in argvs:
            verb = argv[0] if argv else ""
            script = _BIN_DIR / f"{verb}.py"
            if verb not in _VERBS or not script.is_file():
                print(f"gem-inbox-apply: refusing line, not a coordinator verb: {verb!r}", file=sys.stderr)
                codes.append(127)
                continue
            if verb not in mods:
                spec = importlib.util.spec_from_file_location("gem_inbox_verb_" + verb.replace("-", "_"), script)
                mods[verb] = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mods[verb])
            try:
                rc = mods[verb].main(list(argv[1:]))
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            codes.append(int(rc or 0))
    finally:
        os.chdir(saved_cwd)
        if saved_env is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = saved_env
        if added_path and str(_BIN_DIR) in sys.path:
            sys.path.remove(str(_BIN_DIR))
    return codes


def _rel(repo: Path, p: str) -> str:
    q = Path(p)
    if q.is_absolute():
        try:
            return q.relative_to(repo).as_posix()
        except ValueError:
            return q.as_posix()
    return q.as_posix()


def _abs(repo: Path, p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else repo / q


def memo_status(path: Path) -> Optional[str]:
    """The memo's frontmatter `status:`; None when the file or the key is absent."""
    try:
        head = path.read_text(encoding="utf-8").split("\n---", 2)[0]
    except OSError:
        return None
    m = _STATUS_RE.search(head)
    return m.group(1) if m else None


def _live_lines(repo: Path, guard: list, lines: list) -> list:
    """`lines` minus those owned by a guard entry whose memo is no longer open/in_progress."""
    dropped = set()
    for entry in guard:
        if memo_status(_abs(repo, entry["memo"])) not in _LIVE_STATUSES:
            dropped.update(entry.get("lines", []))
    return [ln for ln in lines if ln not in dropped]


def _ledger_memos(manifest: dict) -> list:
    memos = [g["memo"] for g in manifest.get("guard", [])]
    for key in ("closed", "left_open"):
        for item in manifest.get(key, []) or []:
            memos.append(item["memo"] if isinstance(item, dict) and "memo" in item else item)
    seen = []
    for m in memos:
        posix = str(m).replace("\\", "/")
        if isinstance(m, str) and posix not in seen and posix.endswith(".md"):
            seen.append(posix)
    return seen


def _split_line(line: str) -> "list[str]":
    """Tokenize a manifest line; paths are forward-slash only, so a backslash is never an escape."""
    return shlex.split(line)


def _run_lines(tool: ToolRunner, lines: list, cwd: str, out=print) -> "list[int]":
    """One exit code per line; a line containing a backslash is refused (2) and never run."""
    codes = [2 if "\\" in ln else None for ln in lines]
    for ln, rc in zip(lines, codes):
        if rc is not None:
            out(f"gem-inbox-apply: refusing line, backslash in manifest line (use forward slashes): {ln}")
    ok = [ln for ln, rc in zip(lines, codes) if rc is None]
    ran = iter(tool([_split_line(ln) for ln in ok], cwd) if ok else [])
    return [rc if rc is not None else next(ran) for rc in codes]


def apply_manifest(
    manifest_path: str,
    *,
    git=run_git,
    tool: ToolRunner = run_tools,
    out=print,
) -> int:
    """Apply the manifest at `manifest_path`. 0 applied, 1 refused (baton lint), 3 commit failed."""
    mpath = Path(manifest_path)
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    repo = Path(manifest["repo"])
    cwd = str(repo)
    ledger = repo / LEDGER_DIR / f"{mpath.name}.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"memos": _ledger_memos(manifest)}), encoding="utf-8")

    created = []
    for w in manifest.get("writes", []):
        target = _abs(repo, w["path"])
        if not target.exists():
            created.append(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(w["content"], encoding="utf-8")

    batons = [_rel(repo, w["path"]) for w in manifest.get("writes", [])]
    batons = [b for b in batons if "state/handoffs/" in b]
    lint = tool([["lint-frontmatter", "--file", b] for b in batons], cwd) if batons else []
    failed = [b for b, rc in zip(batons, lint) if rc != 0]
    if failed:
        for f in created:
            f.unlink(missing_ok=True)
        ledger.unlink(missing_ok=True)
        out(f"gem-inbox-apply: baton lint failed for {failed[0]}; manifest refused, writes removed")
        return 1

    guard = manifest.get("guard", [])
    stamps = _live_lines(repo, guard, manifest.get("stamps", []))
    base = git(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()
    for line, rc in zip(stamps, _run_lines(tool, stamps, cwd, out)):
        if rc != 0:
            out(f"gem-inbox-apply: stamp exit {rc}: {line}")
    if stamps:
        subjects = git(["log", "--format=%s", f"{base}..HEAD"], cwd=cwd).stdout.split("\n")
        subjects = [s for s in subjects if s]
        if subjects and all(s.startswith(_STAMP_SUBJECT_PREFIXES) for s in subjects):
            git(["reset", "--mixed", base], cwd=cwd)

    for mv in manifest.get("moves", []):
        src, dst = _abs(repo, mv["from"]), _abs(repo, mv["to"])
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)

    sends = _live_lines(repo, guard, manifest.get("memos_to_send", []))
    for line, rc in zip(sends, _run_lines(tool, sends, cwd, out)):
        if rc != 0:
            out(f"gem-inbox-apply: send exit {rc}: {line}")

    commit = manifest["commit"]
    pathspec = [_rel(repo, p) for p in commit["pathspec"]]
    existing = [p for p in pathspec if (repo / p).exists()]
    tracked = set(git(["ls-files", "--", *existing], cwd=cwd).stdout.split("\n")) if existing else set()
    untracked = [p for p in existing if p not in tracked]
    if untracked:
        git(["add", "--intent-to-add", "--", *untracked], cwd=cwd)
    res = git(["commit", "-m", commit["subject"], "--", *pathspec], cwd=cwd)
    if res.returncode != 0:
        out(f"gem-inbox-apply: commit failed ({res.returncode}): {res.stderr.strip() or res.stdout.strip()}")
        return 3
    ledger.unlink(missing_ok=True)
    out(f"gem-inbox-apply: applied {mpath.name}")
    return 0


def main(argv: Sequence[str]) -> int:
    """CLI: `gem-inbox-apply <manifest.json>`."""
    if list(argv) in (["-h"], ["--help"]):
        print("usage: gem-inbox-apply <manifest.json>")
        return 0
    if len(argv) != 1:
        print("usage: gem-inbox-apply <manifest.json>", file=sys.stderr)
        return 2
    if not Path(argv[0]).is_file():
        print(f"gem-inbox-apply: manifest not found: {argv[0]}", file=sys.stderr)
        return 2
    return apply_manifest(argv[0])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

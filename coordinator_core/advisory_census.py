"""coordinator_core.advisory_census -- measure the PreToolUse advisory band.

Two views of the same band:

  --static           every allow_advisory / context_only / rewrite_input(context)
                     call site for event "PreToolUse" in the hooks/, bash_guards/
                     and write_guards/ modules reachable from the six
                     ``hooks/preuse_*_dispatch.py`` ops, keyed ``module:function``,
                     plus the registered guard-chain members.
  --replay <corpus>  feed each corpus line (a PreToolUse payload) through its
                     dispatcher in one fixed session, inside a sandbox repo whose
                     gitdir backs the dedupe store, and sum the bytes of every
                     advisory that reached the returned envelope, per site.

Corpus line: ``{"tool_name", "tool_input", optional "agent_id"/"transcript_path"}``;
the literal ``{cwd}`` inside any string is replaced with the sandbox repo path, so a
corpus carries no absolute path of this machine. A SendMessage line with an
``agent_id`` gets its workflow roster provisioned in the sandbox.

The replay forces the Windows host verdict for the bash chain: guards graded
``windows_cost_only`` are silent on any other OS, and the measured band is the
Windows one, so the figures are the same on every machine.

The doctrine content root is a minimal synthetic tree provisioned in the
sandbox (a three-agent roster and sandbox policy) and every reader of it is
pointed there, so the figures do not depend on what the machine has installed.

Guard-chain members (bash and write engines) build their envelopes by hand and
are keyed ``bash:<name>`` / ``write:<name>`` from the engine's own admission or
fire record; every other site is keyed ``module:function`` from the builder's
caller frame. An outer dispatcher re-wrapping a leg's text is dropped in favour
of the leg.

Hot-path footprint: none. This module is imported by tests and the CLI only.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import importlib
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from coordinator_core import _hook_envelope as _env

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
SESSION_ID = "00000000-0000-4000-8000-00000000c0c0"
EVENT = "PreToolUse"

DISPATCH_MODULES: Dict[str, str] = {
    "Bash": "coordinator_core.hooks.preuse_bash_dispatch",
    "PowerShell": "coordinator_core.hooks.preuse_bash_dispatch",
    "Write": "coordinator_core.hooks.preuse_write_dispatch",
    "Edit": "coordinator_core.hooks.preuse_write_dispatch",
    "MultiEdit": "coordinator_core.hooks.preuse_write_dispatch",
    "NotebookEdit": "coordinator_core.hooks.preuse_write_dispatch",
    "Agent": "coordinator_core.hooks.preuse_agent_dispatch",
    "Read": "coordinator_core.hooks.preuse_search_dispatch",
    "Grep": "coordinator_core.hooks.preuse_search_dispatch",
    "Glob": "coordinator_core.hooks.preuse_search_dispatch",
    "Skill": "coordinator_core.hooks.preuse_skill_dispatch",
    "SendMessage": "coordinator_core.hooks.preuse_sendmessage_dispatch",
}
_SCOPE_PACKAGES = ("hooks", "bash_guards", "write_guards")
_ALWAYS_REACHABLE_PACKAGES = ("bash_guards", "write_guards")
_BUILDERS = frozenset({"allow_advisory", "context_only", "rewrite_input"})
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
#: Modules that only forward text to an envelope builder; the site is their caller.
_TRANSPARENT_PREFIXES = (
    "coordinator_core._hook_envelope",
    "coordinator_core.hooks._envelope",
    "coordinator_core.guard_messages",
)
_DOTTED_RE = re.compile(r"^coordinator_core(\.\w+)+$")


# --------------------------------------------------------------------- static


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _module_path(modname: str) -> Optional[Path]:
    base = REPO_ROOT.joinpath(*modname.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _in_scope(modname: str) -> bool:
    parts = modname.split(".")
    return (
        len(parts) >= 3
        and parts[0] == "coordinator_core"
        and parts[1] in _SCOPE_PACKAGES
        and "tests" not in parts
    )


def _references(tree: ast.AST, modname: str) -> Set[str]:
    package = modname.rsplit(".", 1)[0]
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")
                base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            if base:
                found.add(base)
                found.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _DOTTED_RE.match(node.value):
                found.add(node.value)
    return found


def reachable_modules() -> List[str]:
    """In-scope modules reachable from the six dispatchers, sorted.

    Import and dotted-string references are followed inside hooks/, bash_guards/
    and write_guards/; the last two are added whole because their engines
    discover guards by directory listing.
    """
    seen: Set[str] = set()
    queue = sorted(set(DISPATCH_MODULES.values()))
    for pkg in _ALWAYS_REACHABLE_PACKAGES:
        for path in sorted((PACKAGE_ROOT / pkg).glob("*.py")):
            queue.append(_module_name(path))
    while queue:
        name = queue.pop()
        if name in seen or not _in_scope(name):
            continue
        path = _module_path(name)
        if path is None:
            continue
        seen.add(name)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        queue.extend(ref for ref in _references(tree, name) if ref not in seen)
    return sorted(seen)


class _CallCollector(ast.NodeVisitor):
    def __init__(self, modname: str, mentions_event: bool) -> None:
        self.modname = modname
        self.mentions_event = mentions_event
        self.stack: List[str] = []
        self.sites: Set[str] = set()

    def _visit_def(self, node: Any) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_def
    visit_AsyncFunctionDef = _visit_def

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name in _BUILDERS and self._is_pretooluse(name, node):
            short = self.modname.removeprefix("coordinator_core.")
            self.sites.add(f"{short}:{self.stack[-1] if self.stack else '<module>'}")
        self.generic_visit(node)

    def _is_pretooluse(self, name: str, node: ast.Call) -> bool:
        if name == "rewrite_input":
            if not (len(node.args) >= 3 or any(k.arg == "context" for k in node.keywords)):
                return False
        if not node.args:
            return False
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value == EVENT
        return self.mentions_event


def static_sites() -> List[str]:
    """Sorted ``module:function`` keys of PreToolUse advisory call sites."""
    sites: Set[str] = set()
    for modname in reachable_modules():
        path = _module_path(modname)
        if path is None:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        collector = _CallCollector(modname, EVENT in source)
        collector.visit(tree)
        sites |= collector.sites
    return sorted(sites)


def chain_guards() -> Dict[str, Dict[str, str]]:
    """Registered guard-chain members keyed ``bash:<name>`` / ``write:<name>``.

    The bash and write engines hand-build their advisory envelopes, so no
    builder call site exists for them; the registry is the inventory. ``runs``
    is the call inside a bash entry's lambda, or a write guard's ``check``
    function; write members also carry the engine's class.
    """
    out: Dict[str, Dict[str, str]] = {}
    tree = ast.parse((PACKAGE_ROOT / "bash_guards" / "dispatch.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "GuardEntry"):
            continue
        if not node.args or not (isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            continue
        runs = ""
        if len(node.args) > 1 and isinstance(node.args[1], ast.Lambda) and isinstance(node.args[1].body, ast.Call):
            runs = ast.unparse(node.args[1].body.func)
        out[f"bash:{node.args[0].value}"] = {"runs": runs}
    from coordinator_core.write_guards import engine

    guards, _failed = engine._discover_guards_full()
    for guard in guards:
        out[f"write:{guard.name}"] = {
            "runs": f"{getattr(guard.check, '__module__', '')}.{getattr(guard.check, '__name__', '')}",
            "class": guard.cls,
        }
    return {k: out[k] for k in sorted(out)}


# --------------------------------------------------------------------- replay


def _substitute(value: Any, cwd: str) -> Any:
    if isinstance(value, str):
        return value.replace("{cwd}", cwd)
    if isinstance(value, list):
        return [_substitute(v, cwd) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, cwd) for k, v in value.items()}
    return value


def _make_sandbox(root: Path) -> Path:
    repo = root / "repo"
    git = repo / ".git"
    for sub in ("objects", "refs"):
        (git / sub).mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / ".project-rag").mkdir()
    (repo / ".project-rag" / "graph.db").write_bytes(b"")
    (root / "home").mkdir()
    return repo


_SANDBOX_AGENTS = (("executor", "sonnet"), ("staff-eng", "opus"), ("plan-writer", "sonnet"))


def _provision_content_root(root: Path) -> str:
    """A minimal doctrine content root so the Agent roster does not depend on the machine's install."""
    content = root / "content" / "coordinator"
    (content / "agents").mkdir(parents=True)
    names = "".join(f"  coordinator:{name}: x\n" for name, _ in _SANDBOX_AGENTS)
    (content / "subagent-sandbox-policy.yaml").write_text(f"report_sidecar:\n{names}", encoding="utf-8")
    (content / "governed-authoring-surfaces.json").write_text("[]", encoding="utf-8")
    for name, model in _SANDBOX_AGENTS:
        (content / "agents" / f"{name}.md").write_text(
            f"---\nname: {name}\nmodel: {model}\n---\n\n{name}\n", encoding="utf-8"
        )
    return str(root / "content")


def _provision_roster(payload: Dict[str, Any]) -> None:
    agent_id = payload.get("agent_id")
    transcript = payload.get("transcript_path")
    if not (isinstance(agent_id, str) and isinstance(transcript, str) and transcript.endswith(".jsonl")):
        return
    run_dir = Path(transcript[: -len(".jsonl")]) / "subagents" / "workflows" / "w1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"agent-{agent_id}.jsonl").write_text("", encoding="utf-8")
    (run_dir / "roster.json").write_text(
        json.dumps({"members": [{"agent_id": agent_id}]}), encoding="utf-8"
    )


def _additional_context(envelope: Any) -> str:
    if not isinstance(envelope, dict):
        return ""
    hso = envelope.get("hookSpecificOutput")
    if isinstance(hso, dict):
        ctx = hso.get("additionalContext")
        if isinstance(ctx, str):
            return ctx
    return ""


def _invoke(payload: Dict[str, Any]) -> Any:
    module = importlib.import_module(DISPATCH_MODULES[payload["tool_name"]])
    out = module._handler(payload)
    if asyncio.iscoroutine(out):
        out = asyncio.run(out)
    return out


def _call_site(skip_depth: int) -> str:
    frame = sys._getframe(skip_depth)
    while frame is not None and str(frame.f_globals.get("__name__", "")).startswith(_TRANSPARENT_PREFIXES):
        frame = frame.f_back
    if frame is None:
        return "<unknown>:<unknown>"
    module = str(frame.f_globals.get("__name__", "<unknown>")).removeprefix("coordinator_core.")
    return f"{module}:{frame.f_code.co_name}"


def _path_scrubber(repo: Path) -> Any:
    """Replace the sandbox path with a fixed token so byte counts do not depend on the OS temp root."""
    variants = {str(repo), str(repo.resolve()), repo.as_posix(), repo.resolve().as_posix()}
    ordered = sorted(variants, key=len, reverse=True)

    def scrub(text: str) -> str:
        for variant in ordered:
            text = text.replace(variant, "<repo>")
        return text

    return scrub


def _emitted(
    calls: List[Tuple[str, str]], guard_fires: List[str], final: str, scrub: Any = str
) -> List[Tuple[str, int]]:
    """(site, bytes) for each advisory that reached ``final``.

    A guard-chain fire owns the whole final text and is keyed by guard name.
    Otherwise each recorded builder call whose text is inside ``final`` is a
    site; a record containing an earlier record's text is an outer re-wrap
    and is dropped in favour of the inner one. Text is path-scrubbed first.
    """
    final = scrub(final).strip()
    if not final:
        return []
    if guard_fires:
        return [(guard_fires[-1], len(final.encode("utf-8")))]
    kept: List[Tuple[str, str]] = []
    for site, text in calls:
        text = scrub(text).strip()
        if not text or text not in final:
            continue
        if any(prev_text in text for _, prev_text in kept):
            continue
        kept.append((site, text))
    return [(site, len(text.encode("utf-8"))) for site, text in kept]


class _Recorder:
    """Installs the attribution probes for one replay and removes them after."""

    def __init__(self, content_root: str = "") -> None:
        self.content_root = content_root
        self.calls: List[Tuple[str, str]] = []
        self.guard_fires: List[str] = []
        self.tool = ""

    def _on_record(self, original: Any):
        def recording(builder_name: str, envelope: dict) -> None:
            original(builder_name, envelope)
            if builder_name in _BUILDERS:
                self.calls.append((_call_site(3), _additional_context(envelope)))

        return recording

    def _on_fire(self, guard_name: str, *_args: Any, **_kwargs: Any) -> None:
        tool = self.tool
        kind = "bash" if tool in _SHELL_TOOLS else "write" if tool in _WRITE_TOOLS else tool.lower()
        self.guard_fires.append(f"{kind}:{guard_name}")

    def _on_admit(self, original: Any):
        def admitting(name: str, out: Any, payload: Any) -> Any:
            admitted = original(name, out, payload)
            if admitted is not None:
                self.guard_fires.append(f"bash:{name}")
            return admitted

        return admitting

    @contextlib.contextmanager
    def installed(self) -> Iterator[None]:
        from coordinator_core import guard_advisory_counter as counter
        from coordinator_core.bash_guards import _platform_verdict
        from coordinator_core.bash_guards import dispatch as bash_dispatch

        patches = [
            (_env, "_record", self._on_record(_env._record)),
            (counter, "record_advisory_fire", self._on_fire),
            (bash_dispatch, "_record_advisory_fire", self._on_fire),
            (bash_dispatch, "_admit_guard_result", self._on_admit(bash_dispatch._admit_guard_result)),
            (bash_dispatch, "_resolve_host_is_windows_public", lambda _host: True),
            (_platform_verdict, "_resolve_host_is_windows", lambda _host: True),
        ]
        from coordinator_core import content_root as content_root_module

        for modname in reachable_modules():
            try:
                importlib.import_module(modname)
            except Exception:  # noqa: BLE001 -- a guard that cannot import is not part of the band
                continue
        original_reader = content_root_module.read_content_root
        fixed_root = self.content_root
        patches.append((content_root_module, "read_content_root", lambda: fixed_root))
        patches.extend(
            (module, "read_content_root", lambda: fixed_root)
            for name, module in list(sys.modules.items())
            if name.startswith("coordinator_core.")
            and getattr(module, "read_content_root", None) is original_reader
        )
        originals = [(obj, attr, getattr(obj, attr)) for obj, attr, _ in patches]
        try:
            for obj, attr, replacement in patches:
                setattr(obj, attr, replacement)
            yield
        finally:
            for obj, attr, original in originals:
                setattr(obj, attr, original)


@contextlib.contextmanager
def _sandbox_env(root: Path, repo: Path) -> Iterator[None]:
    saved_env = dict(os.environ)
    saved_cwd = os.getcwd()
    home = str(root / "home")
    for key in [k for k in os.environ if k.startswith(("COORDINATOR_", "CLAUDE"))]:
        del os.environ[key]
    os.environ.update({"HOME": home, "USERPROFILE": home, "CLAUDE_HOME": home})
    os.chdir(repo)
    try:
        yield
    finally:
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved_env)


def replay(corpus: Path) -> Dict[str, Any]:
    """Replay ``corpus`` as one session; return the census report."""
    lines = [ln for ln in corpus.read_text(encoding="utf-8").splitlines() if ln.strip()]
    fires: List[Tuple[str, int]] = []
    root = Path(tempfile.mkdtemp(prefix="advisory-census-"))
    try:
        repo = _make_sandbox(root)
        recorder = _Recorder(_provision_content_root(root))
        scrub = _path_scrubber(repo)
        with recorder.installed(), _sandbox_env(root, repo):
            for line in lines:
                payload = _substitute(json.loads(line), str(repo))
                payload.setdefault("session_id", SESSION_ID)
                payload["cwd"] = str(repo)
                payload.setdefault("plugin_root", str(Path(recorder.content_root) / "coordinator"))
                _provision_roster(payload)
                recorder.calls.clear()
                recorder.guard_fires.clear()
                recorder.tool = payload["tool_name"]
                with _env.capture_session():
                    try:
                        result = _invoke(payload)
                    except Exception:
                        continue
                fires.extend(
                    _emitted(recorder.calls, recorder.guard_fires, _additional_context(result), scrub)
                )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    sites: Dict[str, Dict[str, int]] = {}
    for site, size in fires:
        entry = sites.setdefault(site, {"fires": 0, "bytes": 0})
        entry["fires"] += 1
        entry["bytes"] += size
    sizes = [size for _, size in fires]
    return {
        "advisory_sites": len(sites),
        "fires": len(fires),
        "total_bytes": sum(sizes),
        "median_bytes": int(statistics.median_low(sizes)) if sizes else 0,
        "sites": {k: sites[k] for k in sorted(sites)},
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m coordinator_core.advisory_census")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--static", action="store_true", help="list PreToolUse advisory call sites")
    group.add_argument("--replay", metavar="CORPUS", help="replay a JSONL corpus as one session")
    args = parser.parse_args(argv)
    if args.static:
        report: Any = {"static_sites": static_sites(), "chain_guards": chain_guards()}
    else:
        report = replay(Path(args.replay))
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

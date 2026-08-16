"""
coordinator_core.ops.probe_cwd_example_retrieval_repo_relevance — Workstream-start
gift-shape signal for example-retrieval-repo visibility.

Purpose: Detect whether the current working directory has a example-retrieval-repo
binding, assess MCP health, and apply a UE enrichment layer when a Unreal
Engine project is detected. Emits zero or more human-readable lines to
stdout. Exit 0 always — advisory, never gating.

Port of: probe-cwd-example-retrieval-repo-relevance.sh (DoE b5a4192c, 2026-07-20), 342 lines
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
Spec backlink (behavior): docs/plans/2026-05-20-portable-code-substrate.md § Chunk 3
DoE-side test oracle: coordinator/tests/test_probe_cwd_example_retrieval_repo_relevance.sh

Output format (zero or more lines):
    [example-retrieval-repo-relevance] <tag>: <message>

AC-9 visibility matrix:
    cwd type           | example-retrieval-repo bound? | MCP works? | Surface
    Any, no binding    | no                 | n/a        | (silent)
    Non-UE, bound      | yes                | yes        | healthy: ...
    Non-UE, bound      | yes                | no         | broken: ...
    UE, bound          | yes                | yes, corpus| healthy-ue: ... (2 lines)
    UE, bound          | yes                | yes, nocrp | healthy-ue: ... + suggest-engine-corpus:
    UE, bound          | yes                | no         | p0-broken-ue: (block-quoted)

Testability: respects COORDINATOR_TEST_HOME for the ~ base path (bridged to
COORDINATOR_SETTINGS_HOME the same way the bash oracle bridges it), and
COORDINATOR_TEST_PWD to override the directory scanned for .uproject / git
top-level detection — mirroring the bash script's test seams exactly so the
DoE-side bats-style test suite runs unchanged against this port.

Negative-spec (do NOT "fix" while porting — preserve byte-for-byte parity
with the bash oracle, INCLUDING its known quirk):
    - The registry.toml/registry.local.toml lookup resolves under
      `_coordinator_settings_home()/machine-local`, which — under the
      COORDINATOR_TEST_HOME bridge — is `${COORDINATOR_TEST_HOME}/
      .coordinator-claude-settings/machine-local`, NOT
      `${COORDINATOR_TEST_HOME}/.claude/machine-local` (where the DoE test's
      `write_registry_with_rag()` fixture actually writes). This mismatch is
      a PRE-EXISTING bash-oracle quirk (verified: the bash oracle itself
      fails its own Test 2 for exactly this reason) — reproduce it, don't
      patch it. A fix belongs to a separate bug ticket against the bash
      contract, not this port.
    - The whoami project_kind probe MUST go through a subprocess call to a
      resolved Python interpreter (COORDINATOR_PYTHON env var, else python3,
      else python) — NOT a direct in-process import — because the DoE test's
      Test 7 mocks this exact subprocess seam via COORDINATOR_PYTHON to
      inject project_kind=ue without requiring coordinator_whoami to be
      installed. An in-process import would silently break that test seam.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

from coordinator_core._settings_home import machine_local_dir
from coordinator_core.git.repo_root import show_toplevel

USAGE = """Usage: probe-cwd-example-retrieval-repo-relevance.sh

Reads:
  - PWD (or git rev-parse --show-toplevel if inside git)
  - $HOME/.claude.json (mcpServers — look for "example-retrieval-repo" key)
  - <settings-home>/machine-local/registry.local.toml (repos.example_retrieval_repo for bound-source check; per-machine values)
  - <settings-home>/machine-local/registry.toml (fallback if registry.local.toml absent)
    (settings-home defaults to ~/.coordinator-claude-settings; override via COORDINATOR_SETTINGS_HOME)
  - whoami output (project_kind probe, when example-retrieval-repo tools registered)
  - presence of *.uproject file at PWD top level (canonical UE signal)
  - ~/.claude/plugins/coordinator-claude/data/mcp-registration-last-check.json (sentinel cache, optional)
  - ~/.claude/plugins/example-retrieval-repo-ue-addon/data/doctor-last-run.json (engine-corpus heuristic)

Emits ZERO OR MORE lines following the AC-9 visibility matrix.
Exit 0 always.
"""

_ENGINE_CORPUS_RE = re.compile(r"engine.?corpus", re.IGNORECASE)


def _debug(msg: str) -> None:
    if os.environ.get("COORDINATOR_DEBUG", "0") == "1":
        print(f"[debug] probe-cwd-example-retrieval-repo-relevance: {msg}", file=sys.stderr)


def _resolve_home_base() -> Tuple[str, Optional[str]]:
    """Resolve HOME_BASE plus the COORDINATOR_SETTINGS_HOME value the
    COORDINATOR_TEST_HOME bridge implies, mirroring the bash oracle's bridge
    (see module negative-spec — the bridge reproduces a known pre-existing
    mismatch, not a bug in THIS port).

    Returns ``(home_base, settings_home_override)``. The override is ``None``
    when no bridging applies; the caller applies it for the duration of the
    settings-home read rather than exporting it process-wide.

    Pure resolver — does NOT mutate ``os.environ``. The bash oracle `export`s
    the bridged value, but an export in a spawn-per-call shell script dies with
    the process, whereas the same write from an imported Python module persists
    for the life of the interpreter. Under pytest that leaked a per-test
    tmp-dir settings-home into every later test in the session (including
    subprocess env inherited by the changelog/completion bash-oracle parity
    harnesses, which then failed CLAUDE_KLABAUTER_ROOT resolution). Mirrors the
    deliberate no-export choice documented in
    ``coordinator_core.claude_klabauter_root``'s negative-spec.
    """
    test_home = os.environ.get("COORDINATOR_TEST_HOME")
    if test_home:
        override = None
        if not os.environ.get("COORDINATOR_SETTINGS_HOME"):
            override = os.path.join(test_home, ".coordinator-claude-settings")
        return test_home, override
    return (
        os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or os.path.expanduser("~")
    ), None


@contextlib.contextmanager
def _settings_home_override(value: Optional[str]):
    """Apply ``COORDINATOR_SETTINGS_HOME`` for the duration of the block, then
    restore the prior state exactly. No-op when ``value`` is None."""
    if value is None:
        yield
        return
    previous = os.environ.get("COORDINATOR_SETTINGS_HOME")
    os.environ["COORDINATOR_SETTINGS_HOME"] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("COORDINATOR_SETTINGS_HOME", None)
        else:
            os.environ["COORDINATOR_SETTINGS_HOME"] = previous


def _resolve_effective_cwd() -> str:
    """Determine effective CWD — git top-level preferred, COORDINATOR_TEST_PWD
    overrides both (bypasses git top-level detection, mirroring the bash
    script's test seam)."""
    test_pwd = os.environ.get("COORDINATOR_TEST_PWD")
    if test_pwd:
        return test_pwd

    effective_cwd = os.environ.get("PWD", os.getcwd())
    if shutil.which("git"):
        top = show_toplevel(effective_cwd)
        if top:
            effective_cwd = top
    return effective_cwd


def _claude_json_has_example_retrieval_repo(claude_json_path: str) -> bool:
    if not os.path.isfile(claude_json_path):
        return False
    try:
        with open(claude_json_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        servers = data.get("mcpServers", {}) or {}
        return "example-retrieval-repo" in servers
    except Exception:
        print(f"skip: _claude_json_has_example_retrieval_repo: with open(claude_json_path, \"r\", encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False


def _parse_registry_toml(content: str) -> str:
    """Extract repos.example_retrieval_repo from TOML content. Mirrors the bash oracle's
    embedded Python: tries tomllib first, falls back to a manual line-scan
    parse (Python < 3.11 path — kept for parity even though this process is
    always >= 3.11 in practice, since the bash oracle's fallback path is part
    of the ported contract, not incidental)."""
    try:
        import tomllib

        d = tomllib.loads(content)
        val = d.get("repos", {}).get("example_retrieval_repo", "")
        return val.strip() if val else ""
    except (ImportError, AttributeError):
        print(f"skip: _parse_registry_toml: import tomllib failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass
    except Exception:
        print(f"skip: _parse_registry_toml: import tomllib failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""

    in_repos = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "[repos]":
            in_repos = True
        elif stripped.startswith("[") and stripped != "[repos]":
            in_repos = False
        elif in_repos and stripped.split("=", 1)[0].strip() == "example_retrieval_repo":
            parts = stripped.split("=", 1)
            if len(parts) == 2:
                return parts[1].strip().strip('"').strip("'")
    return ""


def _registry_bound(ml_dir: str) -> bool:
    registry_local = os.path.join(ml_dir, "registry.local.toml")
    registry = os.path.join(ml_dir, "registry.toml")

    registry_file: Optional[str] = None
    if os.path.isfile(registry_local):
        registry_file = registry_local
    elif os.path.isfile(registry):
        registry_file = registry

    if not registry_file:
        return False

    try:
        with open(registry_file, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        print(f"skip: _registry_bound: with open(registry_file, \"r\", encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False

    val = _parse_registry_toml(content)
    return bool(val)


def _resolve_py_interpreter() -> Optional[str]:
    """Resolve the interpreter used ONLY for the whoami subprocess probe —
    COORDINATOR_PYTHON env var wins (test-mock seam), else python3, else
    python. See module negative-spec for why this stays a subprocess call."""
    override = os.environ.get("COORDINATOR_PYTHON")
    if override:
        return override
    py3 = shutil.which("python3")
    if py3:
        return py3
    return shutil.which("python")


def _whoami_project_kind() -> str:
    """Run the coordinator_whoami.example_retrieval_repo probe via a resolved-Python
    subprocess. Returns the project_kind string, or '' on any failure
    (missing interpreter, missing package, bad JSON).

    Deliberate isolation boundary — do not convert to an in-process import.
    Mechanism: import-state isolation — runs a probe script under a
    resolved python whose `coordinator_whoami` package (or absence of it)
    must not land in this process's own `sys.modules`. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    py = _resolve_py_interpreter()
    if not py:
        return ""
    script = (
        "import json\n"
        "try:\n"
        "    m = __import__('coordinator_whoami.example_retrieval_repo', fromlist=[''])\n"
        "    d = json.loads(m.get_whoami_json())\n"
        "    print(d.get('project_kind', ''))\n"
        "except Exception:\n"
        "    print('')\n"
    )
    try:
        from coordinator_core.win_portability import no_console_creationflags

        result = subprocess.run(
            [py, "-c", script],
            capture_output=True,
            text=True,
            check=False,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _whoami_project_kind: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _detect_ue(effective_cwd: str) -> bool:
    is_ue = False
    uproject_signal = False
    whoami_signal = False

    uproject_files = glob.glob(os.path.join(effective_cwd, "*.uproject"))
    if uproject_files:
        is_ue = True
        uproject_signal = True
        _debug(f".uproject found at {effective_cwd} — UE confirmed")

    whoami_pk = _whoami_project_kind()

    if whoami_pk == "ue":
        whoami_signal = True
        if not is_ue:
            is_ue = True
            _debug("whoami project_kind=ue (no .uproject on disk)")

    if uproject_signal and not whoami_signal and whoami_pk and whoami_pk != "ue":
        _debug(f".uproject present but whoami project_kind='{whoami_pk}' — treating as UE")

    _debug(f"is_ue={1 if is_ue else 0}")
    return is_ue


def _mcp_healthy(mcp_sentinel_path: str) -> bool:
    if not os.path.isfile(mcp_sentinel_path):
        return True
    try:
        with open(mcp_sentinel_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        red = data.get("red_servers", []) or []
        return "example-retrieval-repo" not in red
    except Exception:
        print(f"skip: _mcp_healthy: with open(mcp_sentinel_path, \"r\", encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return True


def _engine_corpus_ok(ue_addon_sentinel_path: str) -> bool:
    if not os.path.isfile(ue_addon_sentinel_path):
        _debug("UE addon sentinel absent → engine corpus MISSING")
        return False
    try:
        with open(ue_addon_sentinel_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        verdict = str(data.get("verdict", "")).upper()
        red_probes = data.get("red_probes", []) or []
        probe_str = ",".join(str(p) for p in red_probes)
        engine_match = bool(_ENGINE_CORPUS_RE.search(probe_str))
        corpus_check = "missing" if (verdict in ("RED", "AMBER") and engine_match) else "present"
    except Exception:
        corpus_check = "missing"

    _debug(f"engine_corpus_check={corpus_check}")
    return corpus_check == "present"


def main(argv: List[str]) -> int:
    if argv and argv[0] == "--help":
        print(USAGE, end="")
        return 0

    home_base, settings_home_override = _resolve_home_base()
    effective_cwd = _resolve_effective_cwd()
    _debug(f"effective_cwd={effective_cwd}")

    claude_json = os.path.join(home_base, ".claude.json")
    with _settings_home_override(settings_home_override):
        ml_dir = str(machine_local_dir())

    rag_bound = _claude_json_has_example_retrieval_repo(claude_json) or _registry_bound(ml_dir)
    _debug(f"rag_bound={1 if rag_bound else 0}")

    if not rag_bound:
        return 0

    is_ue = _detect_ue(effective_cwd)

    mcp_sentinel = os.path.join(
        home_base, ".claude", "plugins", "coordinator-claude", "data",
        "mcp-registration-last-check.json",
    )
    mcp_healthy = _mcp_healthy(mcp_sentinel)
    _debug(f"mcp_healthy={1 if mcp_healthy else 0}")

    engine_corpus_ok = False
    if is_ue:
        ue_addon_sentinel = os.path.join(
            home_base, ".claude", "plugins", "example-retrieval-repo-ue-addon", "data",
            "doctor-last-run.json",
        )
        engine_corpus_ok = _engine_corpus_ok(ue_addon_sentinel)

    if not is_ue:
        if mcp_healthy:
            print("[example-retrieval-repo-relevance] healthy: example-retrieval-repo MCP up for this cwd.")
        else:
            print(
                "[example-retrieval-repo-relevance] broken: example-retrieval-repo MCP not responding "
                "for this cwd. Run /example-retrieval-repo:doctor to restore."
            )
    else:
        if not mcp_healthy:
            print(
                "[example-retrieval-repo-relevance] p0-broken-ue: > Going alone on a UE "
                "codebase: example-retrieval-repo MCP is the knowledge tool for this terrain."
            )
            print(
                "[example-retrieval-repo-relevance] p0-broken-ue: > Restore it first — "
                "/example-retrieval-repo:doctor. Without it you're grepping a 100K-file "
                "engine source by hand."
            )
        else:
            print(
                "[example-retrieval-repo-relevance] healthy-ue: example-retrieval-repo MCP up — "
                "subsystems, blueprint graph, referencers, C++ symbols ready."
            )
            print(
                "                                     Query the index; engine "
                "source is 100K files."
            )
            if not engine_corpus_ok:
                print(
                    "[example-retrieval-repo-relevance] suggest-engine-corpus: Engine "
                    "corpus not loaded. Run /example-retrieval-repo-ue-addon:doctor to bootstrap."
                )

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""
test_cross_repo_memo_roundtrip.py — normative round-trip conformance fixture for the
5 lockstep cross-repo-memo path sites.

Spec backlink: cross-repo/inbox/strang-03-fixture-executor-brief (claude-klabauter engine
`send` op validation — this fixture is the conformance bar it must clear).

Purpose: exercise each of the 5 coupled path-declaration sites end-to-end (a memo
written → schema-valid → own-inbox-guarded → surfaced → swept), independently, so a
deliberately-broken single site turns exactly that test red. Also encodes the pinned
Example-doctrine-repo collision contract (2 tests): cross-sender same-day-same-topic survives; same-sender
same-day-same-topic loud-fails via O_EXCL. AC9 adds an op-refusal regression case: a real
Claude-klabauter collision refusal must exit `send` non-zero, retain the outbox, and commit nothing.

Writer-agnostic → writer-REAL (strang-03 C3, post-DR-210 graduation): every assertion
keys on path + frontmatter + on-disk file shape, never on who wrote the file. Sites 1, 2,
4, 5 now dispatch a REAL memo via the claude-klabauter `memo.send` op — through
`coordinator/bin/lib/cc_invoke.route_mutation`, the exact seam `_cmd_send` uses post-C2 —
against an isolated, fixture-resolvable machine-local registry (never the developer's
ambient registry, never a hand-placed simulated file). Each real-op site asserts BOTH the
delivered memo's final on-disk state AND that the seam was PRESENT (not silently routed to
legacy) — see `_legacy_sentinel` / `_SeamNotPresent`. Site 3's own-inbox-guard hook test
still hand-builds Write-tool-shaped payloads directly (it exercises a PreToolUse hook, not
a memo write). If CLAUDE_KLABAUTER_ROOT is genuinely unresolvable on this machine, real-op sites SKIP
loud (never degrade silently to a legacy simulation — the Director of Engineering review, 2026-07-17).

The 5 sites (see docs/wiki/cross-repo-communication.md § Five coupled path declarations):
  1. CLI write target      — bin/cross-repo-memo (`send` verb → cc_invoke.route_mutation →
                              REAL claude-klabauter memo.send op → cross-repo/inbox/)
  2. Schema applies_to     — schemas/cross-repo-memo.schema.json
  3. Own-inbox guard regex — hooks/scripts/validate-frontmatter-schema.py
  4. Surface glob          — bin/workday-start-cross-repo-memo-surface.py
  5. Archival sweep        — bin/sweep-actioned-memos.py (fleet.archive_actioned_memos)

Run with: python3 -m pytest coordinator/bin/test_cross_repo_memo_roundtrip.py
(env: CLAUDE_KLABAUTER_ROOT may be pre-set to pin the claude-klabauter checkout used by real-op sites; if
unset, the ambient resolver — the same coordinator-claude-klabauter-root.sh ladder cc_invoke.py
uses — is tried, and real-op sites SKIP loud on failure.)
"""
# Review: code-reviewer — module docstring must be the first statement (PEP 236)
# to populate __doc__; `from __future__ import annotations` moved after it.
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap

# ---------------------------------------------------------------------------
# Test infrastructure — conventions copied from test_cross_repo_memo.py
# ---------------------------------------------------------------------------

TESTS_SKIPPED = 0
SKIPS: list[str] = []


def _bin_dir() -> str:
    """Return the absolute path to coordinator/bin (this file's directory)."""
    return os.path.dirname(os.path.abspath(__file__))


def _doe_bin_dir() -> str:
    """Return the absolute path to the example-doctrine-repo clone's coordinator/bin dir.

    Schemas (schemas/cross-repo-memo.schema.json) and hooks
    (hooks/scripts/validate-frontmatter-schema.py) are CONTRACT and stay
    example-doctrine-repo-side permanently (commit a0bb05a0, "21 hooks + dead schema guard") —
    they were deliberately NOT migrated with bin/lib/scripts/tests in the
    2026-07-21 executable-surface migration that relocated THIS fixture into
    claude-klabauter's own coordinator/bin/. Resolving these two artifacts off this
    file's own _bin_dir() (as pre-migration code did) now silently resolves
    inside claude-klabauter's checkout, where neither exists.

    Follows the same resolution pattern as
    coordinator_registry.doe_root()/percolate-preflight-scratch-publish.py's
    _resolve_coordinator_root(): DOE_ROOT env (legacy alias, wins first) ->
    REPO_EXAMPLE_DOCTRINE_REPO env -> machine-local `repos.example_doctrine_repo` key. Fails loud
    (RuntimeError naming repos.example_doctrine_repo) rather than silently skipping the
    two example-doctrine-repo-side sites -- a resolvable-but-wrong path would mask a real
    schema/hook regression as a spurious "file not found".
    """
    override = os.environ.get("DOE_ROOT", "").strip() or os.environ.get("REPO_EXAMPLE_DOCTRINE_REPO", "").strip()
    if override:
        return os.path.join(override, "coordinator", "bin")

    lib_dir = os.path.join(_bin_dir(), "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    from coordinator_registry import _DoeUnresolvable, doe_root  # noqa: E402

    try:
        root = doe_root()
    except _DoeUnresolvable as exc:
        raise RuntimeError(
            "test_cross_repo_memo_roundtrip.py: cannot resolve the example-doctrine-repo "
            f"repo root to locate its permanently-example-doctrine-repo-side schemas/ and hooks/ "
            f"({exc}). Set repos.example_doctrine_repo in the machine-local registry, or "
            "set the DOE_ROOT/REPO_EXAMPLE_DOCTRINE_REPO env var."
        ) from exc
    return os.path.join(root, "coordinator", "bin")


def _script_path() -> str:
    """Return the absolute path to cross-repo-memo."""
    return os.path.join(_bin_dir(), "cross-repo-memo")


def _load_dispatcher_module():
    """Import the extensionless cross-repo-memo script as a module.

    The script has no .py extension and is not directly executable on Windows,
    but it is valid Python — importlib loads it by path. Loaded under a name
    other than __main__ so the `if __name__ == "__main__"` guard does not fire.
    """
    import importlib.util
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("cross_repo_memo", _script_path())
    spec = importlib.util.spec_from_loader("cross_repo_memo", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _python() -> str:
    """Return the Python interpreter to use for subprocess invocations.

    Uses sys.executable directly — the interpreter running this test script is
    always a valid Python 3 interpreter; this process is a test harness driving
    other short-lived test subprocesses, not a hot-path spawn.
    """
    return sys.executable  # popup-safe-env-suppressed: test-harness subprocess, not hot-path


def _run_dispatcher(args: list[str], env: dict[str, str], stdin_text: str = "") -> subprocess.CompletedProcess:
    """Invoke the dispatcher CLI as a subprocess with the given environment."""
    return subprocess.run(
        [_python(), _script_path()] + args,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        input=stdin_text,
    )


def _parse_frontmatter(content: str) -> dict[str, str]:
    """Parse the YAML frontmatter block from a memo file.

    Minimal parser — handles simple key: value lines (quoted-string aware)
    within --- delimiters. Sufficient for round-trip assertions.
    """
    lines = content.splitlines()
    in_fm = False
    fm: dict[str, str] = {}
    for line in lines:
        if line.strip() == "---":
            if not in_fm:
                in_fm = True
                continue
            else:
                break
        if in_fm and ":" in line:
            key, _, rest = line.partition(":")
            v = rest.strip()
            if v.startswith('"'):
                raw_rest = line[len(key) + 1:].strip()
                if raw_rest.startswith('"'):
                    i = 1
                    chars = []
                    while i < len(raw_rest):
                        c = raw_rest[i]
                        if c == '\\' and i + 1 < len(raw_rest):
                            nc = raw_rest[i + 1]
                            if nc == '"':
                                chars.append('"')
                            elif nc == '\\':
                                chars.append('\\')
                            elif nc == 'n':
                                chars.append('\n')
                            elif nc == 't':
                                chars.append('\t')
                            else:
                                chars.append(nc)
                            i += 2
                            continue
                        if c == '"':
                            break
                        chars.append(c)
                        i += 1
                    v = ''.join(chars)
                else:
                    v = v.replace('\\"', '"').replace("\\\\", "\\")
            fm[key.strip()] = v
    return fm






def skip_test(name: str, reason: str) -> None:
    """Record a LOUD skip — printed and tallied separately from pass/fail, never
    silent. Used only when the real-op seam is genuinely unresolvable on this
    machine (CLAUDE_KLABAUTER_ROOT unresolvable) — per the Director of Engineering review (2026-07-17), a real-op
    fixture site must SKIP loud rather than silently degrade to a legacy
    simulation. A skip does NOT count as a failure (exit code unaffected by
    skips alone) but IS visible in the run summary."""
    global TESTS_SKIPPED
    TESTS_SKIPPED += 1
    msg = f"  SKIP: {name} — {reason}"
    SKIPS.append(msg)
    print(msg)


def _find_inbox_file(inbox_dir: str, topic: str) -> str | None:
    """Return the path of the first inbox file matching *-<topic>.md."""
    import glob
    pattern = os.path.join(inbox_dir, f"*-{topic}.md")
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def _make_mock_machine_local(tmpdir: str, return_value: str | None) -> str:
    """Create a stub machine-local Python script in tmpdir (see test_cross_repo_memo.py)."""
    stub_path = os.path.join(tmpdir, "_mock_machine_local.py")
    if return_value is None:
        script = textwrap.dedent("""\
            #!/usr/bin/env python3
            import sys
            print("machine-local: key not found", file=sys.stderr)
            sys.exit(1)
        """)
    else:
        escaped = return_value.replace("\\", "\\\\")
        script = textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import sys
            argv = sys.argv[1:]
            if argv and argv[0] == "keys":
                sys.exit(0)
            print("{escaped}")
            sys.exit(0)
        """)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(script)
    return stub_path


# ---------------------------------------------------------------------------
# Real-op dispatch machinery — sites 1, 2, 4, 5 and the AC9 collision case route
# a real memo through cc_invoke.route_mutation onto claude-klabauter's memo.send op,
# against an isolated, fixture-resolvable machine-local registry (never the
# developer's ambient registry, never a hand-placed simulated file).
# ---------------------------------------------------------------------------

def _cc_invoke_module():
    """Import coordinator/bin/lib/cc_invoke.py — the SAME Python transport module
    `_cmd_send` dispatches through post-C2 (mirrors that script's own late
    sys.path-manipulated `import cc_invoke`, so the module-cache identity matches
    what a real `send` subprocess uses)."""
    lib_dir = os.path.join(_bin_dir(), "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import cc_invoke  # noqa: E402 (late import after sys.path manipulation)
    return cc_invoke


def _resolve_ambient_claude_klabauter_root() -> str | None:
    """Resolve CLAUDE_KLABAUTER_ROOT via the SAME resolver cc_invoke.py uses (the
    coordinator-claude-klabauter-root.sh four-rung ladder, honouring an already-set
    CLAUDE_KLABAUTER_ROOT env var first). Returns None (never raises) when genuinely
    unresolvable on this machine, so callers SKIP loud instead of silently
    degrading to a legacy simulation (the Director of Engineering review, 2026-07-17)."""
    mod = _cc_invoke_module()
    try:
        return mod._resolve_claude_klabauter_root()
    except RuntimeError:
        return None


@contextlib.contextmanager
def _env_overrides(**kwargs: str):
    """Temporarily set/replace process environment variables for the duration of
    a real-op dispatch (restores prior values, including "was unset", on exit)."""
    _unset = object()
    saved = {k: os.environ.get(k, _unset) for k in kwargs}
    try:
        os.environ.update(kwargs)
        yield
    finally:
        for k, v in saved.items():
            if v is _unset:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _repo_key_for(to: str) -> str:
    """Mirror memo_send.py._receiver_em_to_repo_key's convention fallback
    (strip trailing '-em', dashes→underscores, prefix 'repos.') — the fixture
    never uses the manifest-alias rung (isolated CLAUDE_HOME has no .doe-root)."""
    suffix = to[:-3] if to.endswith("-em") else to
    return "repos." + suffix.replace("-", "_")


def _write_registry_toml(settings_home: str, repo_key: str, repo_path: str) -> None:
    """Write an ISOLATED machine-local registry.toml under settings_home mapping
    repo_key -> repo_path — the exact surface claude-klabauter's memo_send.py reads directly
    via stdlib tomllib (example-doctrine-repo-ratified resolver surface, DEC/Q1). Distinct from
    MACHINE_LOCAL_IMPL, which only affects example-doctrine-repo-side (cross-repo-memo CLI) lookups."""
    reg_dir = os.path.join(settings_home, "machine-local")
    os.makedirs(reg_dir, exist_ok=True)
    with open(os.path.join(reg_dir, "registry.toml"), "w", encoding="utf-8") as f:
        f.write(f'"{repo_key}" = {json.dumps(repo_path)}\n')


class _SeamNotPresent(RuntimeError):
    """Raised by `_legacy_sentinel` if a real-op fixture dispatch ever falls back
    to legacy_fn — proof the claude-klabauter seam was NOT present (AC6 seam-present
    assertion). A fixture-resolvable, ambient-verified CLAUDE_KLABAUTER_ROOT must never
    leave a fixture dispatch in State-1 (seam-absent); if this fires, it is a
    genuine fixture/environment bug, not an expected op-refusal."""


def _legacy_sentinel() -> None:
    raise _SeamNotPresent(
        "route_mutation fell back to legacy_fn during a real-op fixture dispatch "
        "— the claude-klabauter seam was NOT present despite a fixture-resolvable "
        "CLAUDE_KLABAUTER_ROOT. This must never happen in a real-op fixture site (AC6 "
        "seam-present assertion)."
    )


def _git_init(path: str, *, initial_commit: bool = False) -> None:
    """git-init a fixture repo (+ identity config), optionally seeding an
    --allow-empty initial commit so downstream commit machinery
    (`_commit_delivered_memo`'s work-branch creation) has a HEAD to build on."""
    subprocess.run(["git", "init", path], capture_output=True, check=False)
    subprocess.run(["git", "-C", path, "config", "user.email", "t@t.com"], capture_output=True, check=False)
    subprocess.run(["git", "-C", path, "config", "user.name", "T"], capture_output=True, check=False)
    if initial_commit:
        subprocess.run(["git", "-C", path, "commit", "--allow-empty", "-m", "seed"], capture_output=True, check=False)


def _write_outbox_draft(sender_root: str, *, topic: str, to: str, title: str,
                         body: str = "Round-trip fixture body.\n",
                         summary: str | None = None, kind: str | None = None,
                         supersedes: str | None = None,
                         scoped_to_artifact: str | None = None,
                         scoped_to_version: str | None = None,
                         scoped_to_sha: str | None = None,
                         scoped_to_seam: str | None = None) -> str:
    """Hand-place a valid state/memo-outbox/<topic>.md draft (status: draft) —
    mirrors _compose_outbox_frontmatter's shape. Bypasses the `draft` CLI verb's
    own receiver-registry validation ceremony (not needed by these fixtures: the
    `send` verb re-validates frontmatter and drives the real dispatch itself).

    scoped_to_* params mirror the CLI's flat scoped_to_artifact/scoped_to_version/
    scoped_to_sha/scoped_to_seam outbox keys (docs/plans/2026-07-21-cross-repo-
    decision-scoping-and-peer-read-reconciliation.md § C3) — `send` refuses
    (exit 2, draft preserved) any kind=ask/proposal draft (including absent/
    empty kind, which defaults to ask) lacking a well-formed scoped_to. Callers
    composing an ask/proposal draft that must round-trip through a real `send`
    MUST pass these; fyi/consult fixtures are exempt.

    Returns the written outbox path.
    """
    import datetime
    outbox_dir = os.path.join(sender_root, "state", "memo-outbox")
    os.makedirs(outbox_dir, exist_ok=True)
    path = os.path.join(outbox_dir, f"{topic}.md")
    today = datetime.date.today().isoformat()
    lines = [
        "---",
        f'title: "{title}"',
        "from: roundtrip-fixture-sender-em",
        f"to: {to}",
        f"created: {today}",
        "status: draft",
        "delivery_mode: receiver-repo",
        f'summary: "{summary or ""}"',
    ]
    if kind is not None:
        lines.append(f"kind: {kind}")
    if supersedes is not None:
        lines.append(f"supersedes: {supersedes}")
    if scoped_to_artifact is not None:
        lines.append(f"scoped_to_artifact: {scoped_to_artifact}")
    if scoped_to_version is not None:
        lines.append(f"scoped_to_version: {scoped_to_version}")
    if scoped_to_sha is not None:
        lines.append(f"scoped_to_sha: {scoped_to_sha}")
    if scoped_to_seam is not None:
        lines.append(f"scoped_to_seam: {scoped_to_seam}")
    lines.append("---")
    content = "\n".join(lines) + "\n\n" + body
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _dispatch_real_memo_send(*, claude_klabauter_root: str, sender_root: str, receiver_repo_path: str,
                              to: str, topic: str, title: str, from_id: str,
                              body: str = "Round-trip fixture body.\n", kind: str = "ask",
                              summary: str | None = None, supersedes: str | None = None) -> dict:
    """Deliver ONE memo via the REAL claude-klabauter memo.send op, through
    cc_invoke.route_mutation — the exact Python transport seam `_cmd_send`
    dispatches through post-C2 — against an isolated machine-local registry
    pointing `to`'s repo key at receiver_repo_path.

    Returns the bare op result dict (exit_code / acted / failed). Raises
    _SeamNotPresent if the dispatch silently fell back to legacy (seam-absent
    proof failure — see _legacy_sentinel); raises RuntimeError on a genuine
    op-level refusal (route_mutation's exit_code/failed inspection) or transport
    failure — callers that expect a refusal (AC9) catch RuntimeError explicitly
    and re-raise/ignore _SeamNotPresent, which must never fire.
    """
    mod = _cc_invoke_module()
    repo_key = _repo_key_for(to)
    with tempfile.TemporaryDirectory() as settings_home:
        _write_registry_toml(settings_home, repo_key, receiver_repo_path)
        with _env_overrides(
            CLAUDE_KLABAUTER_ROOT=claude_klabauter_root,
            COORDINATOR_SETTINGS_HOME=settings_home,
            CLAUDE_HOME=settings_home,
        ):
            params = {
                "dry_run": False,
                "topic": topic,
                "to": to,
                "title": title,
                "body": body,
                "from_id": from_id,
                "kind": kind,
                "summary": summary,
                "supersedes": supersedes,
            }
            return mod.route_mutation("memo.send", params, sender_root, _legacy_sentinel)


# ---------------------------------------------------------------------------
# Site 1 — CLI write target: bin/cross-repo-memo writes into
# <receiver>/cross-repo/inbox/YYYY-MM-DD-<from>-<topic>.md
# ---------------------------------------------------------------------------

def test_site1_cli_write_target() -> None:
    name = "Site 1 — CLI write target (`send` verb): repointed _cmd_send dispatches through cc_invoke.route_mutation onto the REAL claude-klabauter memo.send op, writing cross-repo/inbox/<date>-<from>-<topic>.md"
    # Note (divergence from the plan's framing): the flag-only invocation shape
    # this test previously used (`--to ... --topic ... --title ...`, no verb)
    # routes to the LEGACY flag-only `main()` path (argv[0] starts with '--'),
    # NOT `_cmd_send` — that legacy path is a SEPARATE, untouched-by-C2 direct-
    # write caller (AC3's "other caller"). "Site 1 already CLI-driven — exercises
    # the repointed _cmd_send for free" only holds via the `send` VERB
    # (`cross-repo-memo send <topic>`, reading state/memo-outbox/<topic>.md) —
    # confirmed against current disk (`main()`'s verb-detection truth table,
    # cross-repo-memo:2770-2789). This test now drives that verb.
    claude_klabauter_root = _resolve_ambient_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op (see cc_invoke._resolve_claude_klabauter_root)")
        return
    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as sender_tmpdir, \
         tempfile.TemporaryDirectory() as settings_home:
        _git_init(sender_tmpdir)
        _git_init(receiver_tmpdir, initial_commit=True)
        to = "roundtrip-site1-receiver-em"
        mock_impl = _make_mock_machine_local(sender_tmpdir, receiver_tmpdir)
        _write_registry_toml(settings_home, _repo_key_for(to), receiver_tmpdir)
        _write_outbox_draft(sender_tmpdir, topic="roundtrip-site1", to=to,
                             title="Site 1 Roundtrip", body="Site 1 body.\n",
                             summary="Site 1 roundtrip fixture memo.",
                             scoped_to_artifact="coordinator/bin/cross-repo-memo",
                             scoped_to_sha="abc1234", scoped_to_seam="site1-cli-write-target")
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": settings_home,
            "COORDINATOR_SETTINGS_HOME": settings_home,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }
        result = subprocess.run(
            [_python(), _script_path(), "send", "roundtrip-site1"],
            cwd=sender_tmpdir,
            env={**os.environ, **env},
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"send exited {result.returncode}: {result.stderr}"))
        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        receiver_file = _find_inbox_file(inbox_dir, "roundtrip-site1")
        if receiver_file is None:
            raise AssertionError(f"{name}: " + (f"expected file not found in {inbox_dir} (pattern *-roundtrip-site1.md)"))
        basename = os.path.basename(receiver_file)
        # Filename shape: YYYY-MM-DD-<from>-<topic>.md
        if not re.match(r"^\d{4}-\d{2}-\d{2}-.+-roundtrip-site1\.md$", basename):
            raise AssertionError(f"{name}: " + (f"filename does not match <date>-<from>-<topic>.md shape: {basename!r}"))
        # Seam-present proof: the outbox is removed ONLY on the claude-klabauter-op success
        # leg (DEC-2/AC2 — legacy_send always raises before any outbox removal).
        outbox_path = os.path.join(sender_tmpdir, "state", "memo-outbox", "roundtrip-site1.md")
        if os.path.isfile(outbox_path):
            raise AssertionError(f"{name}: " + ("outbox draft still present after a reported-successful send — seam-present proof failed (AC6)"))
        # scoped_to end-to-end survival proof (docs/plans/2026-07-21-cross-repo-
        # decision-scoping-and-peer-read-reconciliation.md § C3/C9): the outbox
        # draft's flat scoped_to_artifact/scoped_to_sha/scoped_to_seam keys must
        # arrive at the receiver as a nested `scoped_to:` mapping — _build_scoped_to
        # assembles it CLI-side, and claude-klabauter's memo.send op (C9) must not drop it.
        # _parse_frontmatter is a flat key:value parser and can't read a nested
        # block, so this checks the raw delivered content directly.
        with open(receiver_file, encoding="utf-8") as f:
            delivered_content = f.read()
        if not re.search(
            r'scoped_to:\s*\n\s*artifact:\s*"coordinator/bin/cross-repo-memo"\s*\n'
            r'\s*sha:\s*"abc1234"\s*\n\s*seam:\s*"site1-cli-write-target"',
            delivered_content,
        ):
            raise AssertionError(f"{name}: " + (f"delivered memo missing/malformed nested scoped_to block (expected artifact/sha/seam to survive real memo.send delivery): {delivered_content!r}"))


# ---------------------------------------------------------------------------
# Site 2 — Schema applies_to: schemas/cross-repo-memo.schema.json
# ---------------------------------------------------------------------------

def test_site2_schema_applies_to() -> None:
    name = "Site 2 — Schema applies_to glob matches cross-repo/inbox/<file> and frontmatter satisfies required fields (real memo.send delivery)"
    try:
        doe_bin_dir = _doe_bin_dir()
    except RuntimeError as exc:
        raise AssertionError(f"{name}: " + (str(exc)))
    schema_path = os.path.join(doe_bin_dir, "..", "schemas", "cross-repo-memo.schema.json")
    with open(schema_path, encoding="utf-8") as f:
        schema = json.load(f)

    applies_to = schema.get("applies_to")
    if not applies_to:
        raise AssertionError(f"{name}: " + ("schema has no applies_to field"))
    required = schema.get("required", [])
    if not required:
        raise AssertionError(f"{name}: " + ("schema has no required field list"))

    claude_klabauter_root = _resolve_ambient_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op")
        return

    with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as sender_root:
        _git_init(sender_root)
        try:
            result = _dispatch_real_memo_send(
                claude_klabauter_root=claude_klabauter_root, sender_root=sender_root, receiver_repo_path=tmpdir,
                to="roundtrip-site2-receiver-em", topic="roundtrip-site2", title="Site 2 Roundtrip",
                from_id="engine-writer-em", summary="Site 2 roundtrip fixture memo.",
            )
        except _SeamNotPresent as exc:
            raise AssertionError(f"{name}: " + (str(exc)))
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"real memo.send dispatch failed: {exc}"))

        acted = result.get("acted") if isinstance(result, dict) else None
        if not (isinstance(result, dict) and result.get("exit_code") == 0 and acted):
            raise AssertionError(f"{name}: " + (f"real op did not report success (seam-present but op-level failure): {result!r}"))
        memo_path = acted[0]["id"]
        if not os.path.isfile(memo_path):
            raise AssertionError(f"{name}: " + (f"op reported delivery to {memo_path!r} but no file found there"))
        # Seam-present proof: the written path is inside the claude-klabauter-registered
        # receiver (registry-derived target, not a legacy-simulated file).
        if os.path.commonpath([os.path.realpath(memo_path), os.path.realpath(tmpdir)]) != os.path.realpath(tmpdir):
            raise AssertionError(f"{name}: " + (f"delivered path {memo_path!r} is not under the registered receiver {tmpdir!r} — seam-present check failed"))

        rel_path = os.path.relpath(memo_path, tmpdir).replace("\\", "/")

        # applies_to is the literal glob "cross-repo/inbox/[0-9]*.md" — a POSIX character
        # class + star, not a regex; translate it directly rather than via re.escape
        # (re.escape would mangle the [0-9] character class).
        if applies_to != "cross-repo/inbox/[0-9]*.md":
            raise AssertionError(f"{name}: " + (f"applies_to glob changed shape unexpectedly ({applies_to!r}); update this test's translation"))
        glob_re = r"^cross-repo/inbox/[0-9][^/]*\.md$"
        if not re.match(glob_re, rel_path):
            raise AssertionError(f"{name}: " + (f"applies_to glob {applies_to!r} does not match written path {rel_path!r}"))

        # (b) required fields present in written frontmatter (final on-disk state).
        with open(memo_path, encoding="utf-8") as f:
            fm = _parse_frontmatter(f.read())
        missing = [field for field in required if not fm.get(field)]
        if missing:
            raise AssertionError(f"{name}: " + (f"written memo frontmatter missing required schema fields: {missing}"))


# ---------------------------------------------------------------------------
# Site 3 — Own-inbox guard regex: hooks/scripts/validate-frontmatter-schema.py
# ---------------------------------------------------------------------------

class _HookResult:
    """subprocess.CompletedProcess-shaped result (.returncode/.stdout/.stderr)
    so the two Site 3 tests' existing assertions (`result.returncode`,
    `json.loads(result.stdout)`) need no change now that the own-inbox guard
    dispatches IN-PROCESS rather than via subprocess — see
    `_run_own_inbox_hook`."""
    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, stdout: str) -> None:
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


def _write_guards_engine_module():
    """Import coordinator_core.write_guards.engine — the in-repo fan-in
    dispatch entrypoint (`engine.evaluate`) that runs every ported write-guard
    module in CLASS/PRIORITY order and resolves to the single envelope a
    PreToolUse hook may emit, reconstructing the reference hook's
    at-most-one-payload-per-write contract across the C10/C11 hard-deny/
    advisory split (see write_guards/INTERFACE.md and the module docstrings
    of validate_frontmatter_schema_deny / validate_frontmatter_schema_advisory
    for the split and its mutual-exclusivity invariant). The own-inbox
    misplacement guard specifically lives in the hard-deny sibling at
    PRIORITY 5 — the lowest of any hard-deny guard — so when it fires it is
    always the first (and thus only) hard-deny checked, never masked by an
    unrelated guard.

    Fails with the REAL cause (an import error) rather than folding a broken
    in-repo import into the `returncode != 0` assertion downstream — the same
    rationale the pre-port RuntimeError("hook not found at ...") existed for
    when the target lived in the example-doctrine-repo sibling checkout (2026-07-23: the
    .js oracle's deletion there manifested as a spurious claude-klabauter contract
    violation). Now that the target is IN-REPO, the equivalent failure mode is
    an import error in coordinator_core.write_guards.engine itself, surfaced
    here with its own cause instead of masquerading as a hook-exit failure.
    """
    try:
        from coordinator_core.write_guards import engine
    except Exception as exc:
        raise RuntimeError(
            "cannot import coordinator_core.write_guards.engine (the ported "
            f"own-inbox guard's in-repo dispatch entrypoint): {exc}"
        ) from exc
    return engine


def _run_own_inbox_hook(cwd: str, file_path: str, content: str) -> _HookResult:
    """Drive the ported own-inbox guard IN-PROCESS via
    coordinator_core.write_guards.engine.evaluate, piping a Write-shaped
    PreToolUse payload. Always "exits 0" (offer-shape hook contract) — the
    deny/advisory/no-fire outcome is carried entirely in the returned JSON
    payload, matching the reference hook's own always-exit-0 contract.

    Both cwd and file_path are realpath-resolved before dispatch: the ported
    guard modules' own resolve_repo_root() still shells out to `git rev-parse
    --show-toplevel`, which returns the REALPATH of the repo root. On macOS,
    tempfile.mkdtemp() returns a path under /var/... which is itself a symlink
    to /private/var/...; to_repo_relative() does a literal startsWith
    comparison between repoRoot and file_path, so an un-resolved /var/... path
    silently fails to match /private/var/... and the own-inbox branch never
    fires (confirmed by direct repro: identical empty-stdout/exit-0 symptom).
    Resolving both to realpath avoids the mismatch — unchanged by the port
    since the git-rev-parse shell-out and the literal-startsWith comparison
    both survived it intact.
    """
    cwd = os.path.realpath(cwd)
    file_path = os.path.realpath(file_path)
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": content},
        "cwd": cwd,
    }
    envelope = _write_guards_engine_module().evaluate(payload)
    return _HookResult(json.dumps(envelope) if envelope is not None else "")


def test_site3_own_inbox_guard_fires() -> None:
    name = "Site 3a — own-inbox guard regex fires on cross-repo/inbox/[0-9]... write with from==thisRepo"
    with tempfile.TemporaryDirectory() as repo_tmpdir:
        subprocess.run(["git", "init", repo_tmpdir], capture_output=True, check=False)
        subprocess.run(["git", "-C", repo_tmpdir, "config", "user.email", "t@t.com"], capture_output=True, check=False)
        subprocess.run(["git", "-C", repo_tmpdir, "config", "user.name", "T"], capture_output=True, check=False)

        repo_basename = os.path.basename(repo_tmpdir.rstrip("/"))
        this_em_id = repo_basename.replace("_", "-") + "-em"

        target = os.path.join(repo_tmpdir, "cross-repo", "inbox", "2026-07-09-own-inbox-test.md")
        content = (
            "---\n"
            'title: "Outbound memo misplaced in own inbox"\n'
            f"from: {this_em_id}\n"
            "to: some-other-repo-em\n"
            "created: 2026-07-09\n"
            "status: open\n"
            "delivery_mode: receiver-repo\n"
            'summary: "Test."\n'
            "---\n"
        )
        try:
            result = _run_own_inbox_hook(repo_tmpdir, target, content)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (str(exc)))
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"hook must always exit 0 (offer-shape); got {result.returncode}"))
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise AssertionError(f"{name}: " + (f"hook stdout not valid JSON: {result.stdout!r}"))
        reason = (
            payload.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")
            or payload.get("hookSpecificOutput", {}).get("additionalContext", "")
        )
        if "own cross-repo/inbox" not in reason and "own inbox" not in reason.lower():
            raise AssertionError(f"{name}: " + (f"expected own-inbox redirect offer in hook output; got: {result.stdout!r}"))


def test_site3_own_inbox_guard_does_not_fire_on_archive() -> None:
    name = "Site 3b — own-inbox guard regex does NOT fire on cross-repo/archive/ write (proves the regex gates, not over-fires)"
    with tempfile.TemporaryDirectory() as repo_tmpdir:
        subprocess.run(["git", "init", repo_tmpdir], capture_output=True, check=False)
        subprocess.run(["git", "-C", repo_tmpdir, "config", "user.email", "t@t.com"], capture_output=True, check=False)
        subprocess.run(["git", "-C", repo_tmpdir, "config", "user.name", "T"], capture_output=True, check=False)

        repo_basename = os.path.basename(repo_tmpdir.rstrip("/"))
        this_em_id = repo_basename.replace("_", "-") + "-em"

        # Same from==thisRepo, to!=thisRepo shape as the firing test — but landing under
        # cross-repo/archive/ instead of cross-repo/inbox/[0-9]. The regex is anchored to
        # inbox/, so this must NOT trigger the own-inbox deny.
        target = os.path.join(repo_tmpdir, "cross-repo", "archive", "2026-07-09-own-inbox-test.md")
        content = (
            "---\n"
            'title: "Closed memo in archive"\n'
            f"from: {this_em_id}\n"
            "to: some-other-repo-em\n"
            "created: 2026-07-09\n"
            "status: actioned\n"
            "delivery_mode: receiver-repo\n"
            'summary: "Test."\n'
            "---\n"
        )
        try:
            result = _run_own_inbox_hook(repo_tmpdir, target, content)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (str(exc)))
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"hook must always exit 0 (offer-shape); got {result.returncode}"))
        try:
            payload = json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            raise AssertionError(f"{name}: " + (f"hook stdout not valid JSON: {result.stdout!r}"))
        reason = (
            payload.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")
            or payload.get("hookSpecificOutput", {}).get("additionalContext", "")
        )
        if "own cross-repo/inbox" in reason or "own inbox" in reason.lower():
            raise AssertionError(f"{name}: " + (f"own-inbox deny incorrectly fired for cross-repo/archive/ write: {result.stdout!r}"))


# ---------------------------------------------------------------------------
# Site 4 — Surface glob: bin/workday-start-cross-repo-memo-surface.py
# ---------------------------------------------------------------------------

def test_site4_surface_glob() -> None:
    name = "Site 4 — surface script (CROSS_REPO_INBOX_DIR override) surfaces a real memo.send-delivered open memo"
    surface_script = os.path.join(_bin_dir(), "workday-start-cross-repo-memo-surface.py")

    claude_klabauter_root = _resolve_ambient_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op")
        return

    with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as sender_root:
        _git_init(sender_root)
        try:
            result = _dispatch_real_memo_send(
                claude_klabauter_root=claude_klabauter_root, sender_root=sender_root, receiver_repo_path=tmpdir,
                to="roundtrip-site4-receiver-em", topic="roundtrip-site4", title="Site 4 Roundtrip Topic",
                from_id="site4-sender-em", summary="Site 4 roundtrip fixture memo.",
            )
        except _SeamNotPresent as exc:
            raise AssertionError(f"{name}: " + (str(exc)))
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"real memo.send dispatch failed: {exc}"))

        acted = result.get("acted") if isinstance(result, dict) else None
        if not (isinstance(result, dict) and result.get("exit_code") == 0 and acted):
            raise AssertionError(f"{name}: " + (f"real op did not report success: {result!r}"))
        memo_path = acted[0]["id"]
        if not os.path.isfile(memo_path):
            raise AssertionError(f"{name}: " + (f"op reported delivery to {memo_path!r} but no file found there"))
        if os.path.commonpath([os.path.realpath(memo_path), os.path.realpath(tmpdir)]) != os.path.realpath(tmpdir):
            raise AssertionError(f"{name}: " + (f"delivered path {memo_path!r} is not under the registered receiver {tmpdir!r} — seam-present check failed"))

        inbox_dir = os.path.join(tmpdir, "cross-repo", "inbox")
        # No MOCK_TODAY override — the real op stamps the actual today, so the
        # surface script's real `date` command matches without faking.
        result_proc = subprocess.run(
            [sys.executable, surface_script],
            env={**os.environ, "CROSS_REPO_INBOX_DIR": inbox_dir},
            capture_output=True,
            text=True,
        )
        if result_proc.returncode != 0:
            raise AssertionError(f"{name}: " + (f"surface script exited {result_proc.returncode}: {result_proc.stderr}"))
        if "site4-sender-em" not in result_proc.stdout or "Site 4 Roundtrip Topic" not in result_proc.stdout:
            raise AssertionError(f"{name}: " + (f"surfaced memo's sender/title not found in stdout: {result_proc.stdout!r}"))


# ---------------------------------------------------------------------------
# Site 5 — Archival sweep: coordinator/bin/sweep-actioned-memos.py (fleet.archive_actioned_memos)
# ---------------------------------------------------------------------------

def test_site5_archival_sweep() -> None:
    name = "Site 5 — sweep-actioned-memos.py archives a status:actioned memo (real memo.send delivery, then actioned) from inbox/ to archive/ via fleet.archive_actioned_memos"
    sweep_script = os.path.join(_bin_dir(), "sweep-actioned-memos.py")
    if not os.path.isfile(sweep_script):
        raise AssertionError(f"{name}: " + (f"sweep-actioned-memos.py not found at {sweep_script}"))

    claude_klabauter_root = _resolve_ambient_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op")
        return

    with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as sender_root:
        _git_init(tmpdir)
        _git_init(sender_root)

        try:
            result = _dispatch_real_memo_send(
                claude_klabauter_root=claude_klabauter_root, sender_root=sender_root, receiver_repo_path=tmpdir,
                to="roundtrip-site5-receiver-em", topic="roundtrip-site5", title="Site 5 Roundtrip",
                from_id="site5-sender-em", summary="Site 5 roundtrip fixture memo.",
            )
        except _SeamNotPresent as exc:
            raise AssertionError(f"{name}: " + (str(exc)))
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"real memo.send dispatch failed: {exc}"))

        acted = result.get("acted") if isinstance(result, dict) else None
        if not (isinstance(result, dict) and result.get("exit_code") == 0 and acted):
            raise AssertionError(f"{name}: " + (f"real op did not report success: {result!r}"))
        memo_path = acted[0]["id"]
        if not os.path.isfile(memo_path):
            raise AssertionError(f"{name}: " + (f"op reported delivery to {memo_path!r} but no file found there"))
        fname = os.path.basename(memo_path)

        # Post-delivery lifecycle mutation — simulates the RECEIVING EM's later
        # "action" step (accept/decline), a genuinely separate write from
        # memo.send's delivery: memo.send ALWAYS composes status: open
        # (memo_send.py._compose_memo negative-spec — "never actioned/draft/
        # closed"), so a real delivery cannot itself land in the actioned state
        # Site 5 needs to test the SWEEP. This does not weaken the real-op
        # delivery assertion above — it is a distinct, clearly-marked step.
        with open(memo_path, encoding="utf-8") as f:
            delivered = f.read()
        if "status: open" not in delivered:
            raise AssertionError(f"{name}: " + (f"delivered memo does not carry status: open as expected pre-mutation: {delivered[:200]!r}"))
        actioned = delivered.replace("status: open", "status: actioned", 1)
        parts = actioned.split("---", 2)
        if len(parts) != 3:
            raise AssertionError(f"{name}: " + (f"delivered memo frontmatter shape unexpected, cannot inject actioned fields: {actioned[:200]!r}"))
        parts[1] = parts[1].rstrip("\n") + "\n" + "\n".join([
            'decision: "accepted"',
            'action_taken_at: "2026-07-02T10:00:00Z"',
            'realized_by: "inline"',
        ]) + "\n"
        with open(memo_path, "w", encoding="utf-8") as f:
            f.write("---".join(parts))

        subprocess.run(["git", "-C", tmpdir, "add", "cross-repo/inbox"], capture_output=True, check=False)
        subprocess.run(["git", "-C", tmpdir, "commit", "-m", "seed actioned memo"], capture_output=True, check=False)

        # Drive the real trampoline as a subprocess (the exact invocation
        # /workstream-complete Step 2.65 uses) — CLAUDE_KLABAUTER_ROOT pinned so the
        # op routes to this fixture's claude-klabauter checkout, not an ambient one.
        sweep_env = dict(os.environ, CLAUDE_KLABAUTER_ROOT=claude_klabauter_root)
        result_proc = subprocess.run(
            [sys.executable, sweep_script, tmpdir],
            capture_output=True,
            text=True,
            env=sweep_env,
        )
        if result_proc.returncode != 0:
            raise AssertionError(f"{name}: " + (f"sweep-actioned-memos.py invocation exited {result_proc.returncode}: {result_proc.stderr}"))
        try:
            swept_count = int(result_proc.stdout.strip())
        except ValueError:
            raise AssertionError(f"{name}: " + (f"sweep-actioned-memos.py did not print an integer count: stdout={result_proc.stdout!r} stderr={result_proc.stderr!r}"))
        if swept_count != 1:
            raise AssertionError(f"{name}: " + (f"sweep-actioned-memos.py reported {swept_count} archived (expected 1): stdout={result_proc.stdout!r} stderr={result_proc.stderr!r}"))

        archived_path = os.path.join(tmpdir, "cross-repo", "archive", fname)
        inbox_path_after = os.path.join(tmpdir, "cross-repo", "inbox", fname)
        if not os.path.isfile(archived_path):
            raise AssertionError(f"{name}: " + (f"memo not found at expected archive path {archived_path} after sweep. sweep stdout: {result_proc.stdout!r} stderr: {result_proc.stderr!r}"))
        if os.path.isfile(inbox_path_after):
            raise AssertionError(f"{name}: " + (f"memo still present at inbox path {inbox_path_after} after sweep — should have been moved"))


# ---------------------------------------------------------------------------
# Collision case 1 — cross-sender, same day, same topic → BOTH memos survive
# ---------------------------------------------------------------------------

def test_collision_cross_sender_both_survive() -> None:
    name = "Collision 1 — cross-sender same-day same-topic via the flag-only CLI form: BOTH memos survive (sender folded into filename)"
    # Post-strang-03b (Q-c hard): the flag-only `--to/--topic` CLI form no longer
    # direct-writes — it routes through cc_invoke.route_mutation onto the REAL
    # claude-klabauter memo.send op (mirrors _cmd_send's DR-210 graduation — see
    # test_site1_cli_write_target's docstring for the site-1 analog). An
    # unregistered temp sender/receiver is now refused by claude-klabauter's own registry
    # read (build_setup_error_result), so this fixture must register BOTH sender
    # repos and the receiver in an ISOLATED machine-local registry.toml (never
    # the developer's ambient registry) and pin CLAUDE_KLABAUTER_ROOT — same isolation
    # shape test_site1_cli_write_target and _dispatch_real_memo_send use, just
    # driven through the flag-only CLI form (not the `send <topic>` verb) so
    # this site's own coverage — the legacy flag-only invocation shape — is not
    # lost. If CLAUDE_KLABAUTER_ROOT is genuinely unresolvable, skip loud (never silently
    # degrade to a legacy simulation — the Director of Engineering review, 2026-07-17).
    claude_klabauter_root = _resolve_ambient_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op")
        return

    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_a, \
         tempfile.TemporaryDirectory() as claude_home_b, \
         tempfile.TemporaryDirectory() as settings_home:
        mock_impl = _make_mock_machine_local(receiver_tmpdir, receiver_tmpdir)

        # Simulate two distinct sender repos so em_id_for_root resolves two different
        # `from:` identities (both unregistered siblings → basename fallback), proving
        # the sender-folded-into-filename guarantee for the real N-repo-broadcast case.
        sender_a_root = os.path.join(claude_home_a, "sender-repo-alpha")
        sender_b_root = os.path.join(claude_home_b, "sender-repo-beta")
        os.makedirs(sender_a_root)
        os.makedirs(sender_b_root)
        _git_init(sender_a_root)
        _git_init(sender_b_root)
        _git_init(receiver_tmpdir, initial_commit=True)

        # Isolated claude-klabauter-side registry: the receiver must resolve to
        # receiver_tmpdir (matching the example-doctrine-repo-side mock above) so both sides agree
        # on the delivery target. Senders need no registry entry — memo.send's
        # own-inbox guard compares sender_worktree to the REGISTERED receiver
        # path, and neither sender root matches it; the earlier per-sender
        # "UNREGISTERED repo" message is a non-fatal example-doctrine-repo-side warning only.
        to = "roundtrip-collision-cli-receiver-em"
        _write_registry_toml(settings_home, _repo_key_for(to), receiver_tmpdir)

        env_common = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "COORDINATOR_SETTINGS_HOME": settings_home,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }
        env_a = {**env_common, "CLAUDE_HOME": claude_home_a}
        env_b = {**env_common, "CLAUDE_HOME": claude_home_b}

        result_a = subprocess.run(
            [_python(), _script_path(), "--to", to, "--topic", "roundtrip-collision",
             "--title", "From Alpha",
             "--summary", "Collision 1 roundtrip fixture memo (Alpha).",
             "--scoped-to-artifact", "coordinator/bin/cross-repo-memo",
             "--scoped-to-sha", "abc1234", "--scoped-to-seam", "collision1-flag-only-cli"],
            cwd=sender_a_root,
            env={**os.environ, **env_a},
            capture_output=True, text=True, input="Alpha body.\n",
        )
        result_b = subprocess.run(
            [_python(), _script_path(), "--to", to, "--topic", "roundtrip-collision",
             "--title", "From Beta",
             "--summary", "Collision 1 roundtrip fixture memo (Beta).",
             "--scoped-to-artifact", "coordinator/bin/cross-repo-memo",
             "--scoped-to-sha", "def5678", "--scoped-to-seam", "collision1-flag-only-cli"],
            cwd=sender_b_root,
            env={**os.environ, **env_b},
            capture_output=True, text=True, input="Beta body.\n",
        )
        if result_a.returncode != 0:
            raise AssertionError(f"{name}: " + (f"sender A dispatch failed: {result_a.returncode}: {result_a.stderr}"))
        if result_b.returncode != 0:
            raise AssertionError(f"{name}: " + (f"sender B dispatch failed: {result_b.returncode}: {result_b.stderr}"))

        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        import glob
        matches = sorted(glob.glob(os.path.join(inbox_dir, "*-roundtrip-collision.md")))
        if len(matches) != 2:
            raise AssertionError(f"{name}: " + (f"expected 2 distinct memo files (cross-sender, same day/topic); found {len(matches)}: {matches}"))
        titles = set()
        for m in matches:
            with open(m, encoding="utf-8") as f:
                fm = _parse_frontmatter(f.read())
            titles.add(fm.get("title"))
        if titles != {"From Alpha", "From Beta"}:
            raise AssertionError(f"{name}: " + (f"both memo contents should survive distinctly; got titles: {titles}"))


# ---------------------------------------------------------------------------
# Collision case 1b — cross-sender, same day, same topic, through the REAL
# claude-klabauter memo.send seam → BOTH memos survive (Review: code-reviewer Finding 3
# — the legacy-path variant above only proves the separate legacy writer
# preserves this guarantee; this variant proves the real op does too).
# ---------------------------------------------------------------------------

def test_collision_cross_sender_both_survive_real_op() -> None:
    name = "Collision 1b — cross-sender same-day same-topic through the REAL memo.send op: BOTH memos survive distinctly"
    claude_klabauter_root = _resolve_ambient_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op")
        return

    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as sender_a_root, \
         tempfile.TemporaryDirectory() as sender_b_root:
        _git_init(sender_a_root)
        _git_init(sender_b_root)
        to = "roundtrip-collision1b-receiver-em"

        try:
            result_a = _dispatch_real_memo_send(
                claude_klabauter_root=claude_klabauter_root, sender_root=sender_a_root, receiver_repo_path=receiver_tmpdir,
                to=to, topic="roundtrip-collision1b", title="From Alpha",
                from_id="roundtrip-collision1b-alpha-em", body="Alpha body.\n",
                summary="Collision 1b roundtrip fixture memo (Alpha).",
            )
        except _SeamNotPresent as exc:
            raise AssertionError(f"{name}: " + (str(exc)))
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"real memo.send dispatch (sender A) failed: {exc}"))

        try:
            result_b = _dispatch_real_memo_send(
                claude_klabauter_root=claude_klabauter_root, sender_root=sender_b_root, receiver_repo_path=receiver_tmpdir,
                to=to, topic="roundtrip-collision1b", title="From Beta",
                from_id="roundtrip-collision1b-beta-em", body="Beta body.\n",
                summary="Collision 1b roundtrip fixture memo (Beta).",
            )
        except _SeamNotPresent as exc:
            raise AssertionError(f"{name}: " + (str(exc)))
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"real memo.send dispatch (sender B) failed: {exc}"))

        for label, result in (("A", result_a), ("B", result_b)):
            acted = result.get("acted") if isinstance(result, dict) else None
            if not (isinstance(result, dict) and result.get("exit_code") == 0 and acted):
                raise AssertionError(f"{name}: " + (f"real op dispatch {label} did not report success: {result!r}"))

        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        import glob
        matches = sorted(glob.glob(os.path.join(inbox_dir, "*-roundtrip-collision1b.md")))
        if len(matches) != 2:
            raise AssertionError(f"{name}: " + (f"expected 2 distinct memo files (cross-sender, same day/topic, real op); found {len(matches)}: {matches}"))
        titles = set()
        for m in matches:
            with open(m, encoding="utf-8") as f:
                fm = _parse_frontmatter(f.read())
            titles.add(fm.get("title"))
        if titles != {"From Alpha", "From Beta"}:
            raise AssertionError(f"{name}: " + (f"both memo contents should survive distinctly through the real op; got titles: {titles}"))


# ---------------------------------------------------------------------------
# Collision case 2 — same-sender, same day, same topic → LOUD FAIL, no silent clobber
# ---------------------------------------------------------------------------

def test_collision_same_sender_loud_fail() -> None:
    name = "Collision 2 — same-sender same-day same-topic: second write LOUD FAILS (O_EXCL), first memo unchanged"
    mod = _load_dispatcher_module()
    with tempfile.TemporaryDirectory() as receiver_tmpdir:
        target_path = os.path.join(receiver_tmpdir, "cross-repo", "inbox", "2026-07-09-same-sender-em-roundtrip-collision2.md")
        first_content = "---\ntitle: \"First\"\n---\nFirst content.\n"
        second_content = "---\ntitle: \"Second\"\n---\nSecond content.\n"

        # First write succeeds.
        try:
            mod._write_file(target_path, first_content, receiver_tmpdir)
        except Exception as exc:
            raise AssertionError(f"{name}: " + (f"first write should succeed; raised {type(exc).__name__}: {exc}"))
        if not os.path.isfile(target_path):
            raise AssertionError(f"{name}: " + ("first write reported success but file is not present on disk"))
        with open(target_path, encoding="utf-8") as f:
            after_first = f.read()
        if after_first != first_content:
            raise AssertionError(f"{name}: " + ("first file content does not match what was written"))

        # Second write to the SAME path (same sender+date+topic) must LOUD FAIL.
        try:
            mod._write_file(target_path, second_content, receiver_tmpdir)
            raise AssertionError(f"{name}: " + ("second write should raise FileExistsError (O_EXCL guard); no exception raised"))
        except FileExistsError:
            pass
        except Exception as exc:
            raise AssertionError(f"{name}: " + (f"second write raised wrong exception type {type(exc).__name__}: {exc}"))

        # First file's content must be UNCHANGED after the rejected second write.
        with open(target_path, encoding="utf-8") as f:
            after_second_attempt = f.read()
        if after_second_attempt != first_content:
            raise AssertionError(f"{name}: " + (f"first file content changed after rejected second write — silent clobber occurred. Got: {after_second_attempt!r}"))


# ---------------------------------------------------------------------------
# AC9 — op-refusal regression: a real claude-klabauter collision refusal must exit
# `_cmd_send` non-zero, retain the outbox, and commit nothing. This is the
# regression net for the C1.5 route_mutation exit_code-inspection fix (the
# DR-215 exit_code trap the Director of Engineering's 2026-07-17 review flagged as reintroduced by an
# exit_code-blind transport — see cc_invoke.route_mutation).
# ---------------------------------------------------------------------------

def test_ac9_op_refusal_regression() -> None:
    name = "AC9 — op-refusal regression: real claude-klabauter collision refusal exits `send` non-zero, outbox retained, no phantom commit"
    claude_klabauter_root = _resolve_ambient_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real op-refusal path")
        return

    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as sender_tmpdir, \
         tempfile.TemporaryDirectory() as settings_home:
        _git_init(sender_tmpdir)
        _git_init(receiver_tmpdir, initial_commit=True)

        to = "roundtrip-ac9-receiver-em"
        mock_impl = _make_mock_machine_local(sender_tmpdir, receiver_tmpdir)
        _write_registry_toml(settings_home, _repo_key_for(to), receiver_tmpdir)
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": settings_home,
            "COORDINATOR_SETTINGS_HOME": settings_home,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }

        # First send: succeeds, delivers the collision TARGET, and removes its outbox.
        _write_outbox_draft(sender_tmpdir, topic="roundtrip-ac9-collision", to=to,
                             title="AC9 First", body="First body.\n",
                             summary="AC9 first roundtrip fixture memo.",
                             scoped_to_artifact="coordinator/bin/cross-repo-memo",
                             scoped_to_sha="abc1234", scoped_to_seam="ac9-op-refusal-regression")
        first = subprocess.run(
            [_python(), _script_path(), "send", "roundtrip-ac9-collision"],
            cwd=sender_tmpdir, env={**os.environ, **env}, capture_output=True, text=True,
        )
        if first.returncode != 0:
            raise AssertionError(f"{name}: " + (f"first send should succeed (sets up the collision target); exited {first.returncode}: {first.stderr}"))

        # Snapshot the receiver's commit count BEFORE the second (colliding) attempt —
        # proves no phantom commit happens on the refused leg.
        log_before = subprocess.run(
            ["git", "-C", receiver_tmpdir, "rev-list", "--count", "HEAD"],
            capture_output=True, text=True,
        )
        if log_before.returncode != 0:
            # Review: code-reviewer (Finding 1) — a failed commit-count read must not
            # silently no-op the phantom-commit check; fail loud rather than skip.
            raise AssertionError(f"{name}: " + (f"could not read receiver commit count before second send: {log_before.stderr!r}"))
        commits_before = log_before.stdout.strip()

        # Second send: re-draft the SAME topic (same day, same sender) — claude-klabauter's
        # O_EXCL target-path collision refuses (act result: exit_code:2, failed
        # non-empty) — route_mutation raises — _cmd_send must exit non-zero,
        # retain THIS outbox, and commit nothing new.
        second_outbox = _write_outbox_draft(sender_tmpdir, topic="roundtrip-ac9-collision", to=to,
                                             title="AC9 Second", body="Second body.\n",
                                             summary="AC9 second roundtrip fixture memo.",
                                             scoped_to_artifact="coordinator/bin/cross-repo-memo",
                                             scoped_to_sha="abc1234", scoped_to_seam="ac9-op-refusal-regression")
        second = subprocess.run(
            [_python(), _script_path(), "send", "roundtrip-ac9-collision"],
            cwd=sender_tmpdir, env={**os.environ, **env}, capture_output=True, text=True,
        )
        if second.returncode == 0:
            raise AssertionError(f"{name}: " + ("second send (same-day same-sender same-topic collision) should have exited non-zero; exited 0"))
        if not os.path.isfile(second_outbox):
            raise AssertionError(f"{name}: " + ("second send's outbox draft was REMOVED despite the op refusing — must be retained on failure (AC9)"))
        # Review: code-reviewer (Finding 2) — confirm this is the intended O_EXCL
        # collision refusal, not an unrelated failure, before trusting the rest
        # of the assertion chain.
        refusal_text = (second.stderr or "") + (second.stdout or "")
        if not re.search(r"exists|collision|exit_code[:= ]*2", refusal_text, re.IGNORECASE):
            raise AssertionError(f"{name}: " + (f"second send failed, but stderr/stdout doesn't confirm an O_EXCL collision refusal (AC9 requires the specific refusal, not any failure): {refusal_text!r}"))

        log_after = subprocess.run(
            ["git", "-C", receiver_tmpdir, "rev-list", "--count", "HEAD"],
            capture_output=True, text=True,
        )
        if log_after.returncode != 0:
            raise AssertionError(f"{name}: " + (f"could not read receiver commit count after second send: {log_after.stderr!r}"))
        commits_after = log_after.stdout.strip()
        if commits_before != commits_after:
            raise AssertionError(f"{name}: " + (f"receiver commit count changed after the refused second send ({commits_before} -> {commits_after}) — phantom commit (AC9)"))



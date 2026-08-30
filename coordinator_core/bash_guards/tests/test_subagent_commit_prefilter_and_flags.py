"""Tests for the C2 fix (2026-08-01) to
``coordinator_core.bash_guards.block_subagent_commit``: closing the two
no-obfuscation holes named in
``docs/plans/2026-08-01-confine-subagent-bash-by-allowlist.md`` chunk C2.

Hole (a) -- ``_prefilter_mentions_commit`` was a bare ``"commit" in cmd``,
so it short-circuited ``check()`` to ALLOW, before identity resolution ever
ran, for committing ops whose names contain no ``commit`` substring at all
(``session.boot_sweep``, ``distill.apply_disposal``, ``memo.send``; a
fourth, ``ceremony.wsc_tail``, was pinned here too until the op itself was
killed 2026-08-23 -- see ``block_subagent_commit.py``'s own
``_COMMITTING_OP_NAMES`` removal note -- and its denial case below removed
with it, denying an unregistered op name being no longer a meaningful
case). Pinned below: one denial case per surviving op name.

Hole (b) -- the invoke-matcher required the ``<op>`` token IMMEDIATELY
after ``coordinator_core.invoke``, so the documented ``--repo``-before-
positional CLI spelling (``--repo`` is effectively required per AC-5's
no-implicit-cwd-fallback rule) walked straight past it. Pinned below:
``--repo`` (and ``--params-file``) preceding the op, in bare and
``--flag=value`` forms.

Also re-pins the pre-existing matched shapes (plain ``git commit``,
``coordinator-safe-commit``, op-immediately-after-module) to prove no
regression from either fix.

Pure Python -- no shell spawns, no filesystem writes (Windows+macOS
first-class). Same seam-patching pattern as
``test_block_subagent_commit.py``.

Spec backlink: coordinator_core/bash_guards/block_subagent_commit.py
Spec backlink: pln-confine-subagent-bash-by-allow-7c2901
  chunk C2
"""

from __future__ import annotations

from coordinator_core.bash_guards import block_subagent_commit as guard

_SUBAGENT_TYPE = "coordinator:executor"


def _payload(command, agent_id="deadbeef0123", agent_type=None, session_id="sess1"):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": session_id,
        "cwd": None,
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    if agent_type is not None:
        p["agent_type"] = agent_type
    return p


def _subagent(monkeypatch, subagent_type=_SUBAGENT_TYPE):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id: subagent_type,
    )


def _denies(monkeypatch, cmd):
    _subagent(monkeypatch)
    result = guard.check(_payload(cmd, agent_type=_SUBAGENT_TYPE))
    assert result is not None, f"expected DENY for: {cmd!r}"
    assert (
        result["hookSpecificOutput"]["permissionDecision"] == "deny"
    ), f"expected DENY for: {cmd!r}"


def _allows(monkeypatch, cmd):
    _subagent(monkeypatch)
    result = guard.check(_payload(cmd, agent_type=_SUBAGENT_TYPE))
    assert result is None, f"expected ALLOW for: {cmd!r}, got {result!r}"


# ---------------------------------------------------------------------------
# Hole (a) -- prefilter short-circuit for committing ops without "commit"
# in their name. One case per op name in _COMMITTING_OP_NAMES that lacks
# the substring.
# ---------------------------------------------------------------------------


def test_prefilter_admits_session_boot_sweep(monkeypatch):
    """``session.boot_sweep`` contains no ``commit`` substring -- the OLD
    prefilter returned ALLOW here before identity resolution ever ran.
    """
    _denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke session.boot_sweep '{}'",
    )


def test_prefilter_admits_distill_apply_disposal(monkeypatch):
    _denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke distill.apply_disposal '{}'",
    )


def test_prefilter_admits_memo_send(monkeypatch):
    _denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke memo.send '{}'",
    )


def test_committing_op_em_main_loop_still_allows():
    """No ``agent_id`` in the payload -> EM main-loop -> allowed exactly
    like every other committing shape this gate denies for a subagent.
    """
    payload = _payload(
        "python3 -m coordinator_core.invoke session.boot_sweep '{}'",
        agent_id=None,
    )
    assert guard.check(payload) is None


def test_non_committing_op_still_allows(monkeypatch):
    """A DIFFERENT op dispatched through the same generic CLI (not in
    ``_COMMITTING_OP_NAMES``) must not be denied -- the widened prefilter
    must not become an unbounded "any op" denial.
    """
    _allows(
        monkeypatch,
        "python3 -m coordinator_core.invoke coverage.gate '{}'",
    )


# ---------------------------------------------------------------------------
# Hole (b) -- --repo (and --params-file) BEFORE the op positional.
# ---------------------------------------------------------------------------


def test_repo_flag_before_positional_op_denies(monkeypatch):
    """The documented CLI spelling -- ``--repo`` before the op -- must
    deny. This is the exact shape named in the C2 brief as walking
    straight past the OLD positional-adjacency matcher.
    """
    _denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke --repo /path ceremony.scoped_git_commit '{}'",
    )


def test_repo_flag_equals_form_before_positional_op_denies(monkeypatch):
    """``--repo=/path`` (attached flag=value form) before the op."""
    _denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke --repo=/path ceremony.scoped_git_commit '{}'",
    )


def test_params_file_flag_before_positional_op_denies(monkeypatch):
    """``--params-file`` (the OTHER value-taking flag) before the op."""
    _denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke --params-file /tmp/p.json ceremony.scoped_git_commit",
    )


def test_bare_flag_before_positional_op_denies(monkeypatch):
    """A no-value flag (``--bare``) before the op must not desync the
    flag-skip walk's token accounting.
    """
    _denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke --bare ceremony.scoped_git_commit '{}'",
    )


def test_multiple_flags_before_positional_op_denies(monkeypatch):
    """A value-taking flag AND a no-value flag stacked before the op, in
    combination -- the flag-skip walk must correctly account for both.
    """
    _denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke --repo /path --bare session.boot_sweep '{}'",
    )


def test_repo_flag_before_positional_non_committing_op_allows(monkeypatch):
    """``--repo``-before-positional for a non-committing op must still
    allow -- the flag-tolerant walk must not become an unbounded
    "anything after coordinator_core.invoke" denial.
    """
    _allows(
        monkeypatch,
        "python3 -m coordinator_core.invoke --repo /path coverage.gate '{}'",
    )


# ---------------------------------------------------------------------------
# Regression: pre-existing matched shapes must still match after both fixes.
# ---------------------------------------------------------------------------


def test_plain_git_commit_still_denies(monkeypatch):
    _denies(monkeypatch, 'git commit -m "msg"')


def test_coordinator_safe_commit_still_denies(monkeypatch):
    _denies(monkeypatch, 'coordinator-safe-commit -m "msg"')


def test_ceremony_scoped_git_commit_op_immediately_after_module_still_denies(
    monkeypatch,
):
    """The pre-existing matched shape -- op immediately after the module
    name, no intervening flags -- must still deny.
    """
    _denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke ceremony.scoped_git_commit '{}'",
    )


def test_ceremony_scoped_git_commit_with_repo_flag_after_op_still_denies(
    monkeypatch,
):
    """``--repo`` AFTER the op (the in-repo callers' natural spelling,
    already pinned in the pre-existing suite) must still deny.
    """
    _denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke ceremony.scoped_git_commit '{}' --repo /x",
    )


def test_no_agent_id_em_main_loop_allows():
    payload = _payload("git commit -m x", agent_id=None)
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# C5 -- coordinator/bin/scoped-git-commit trampoline. This helper spawns
# `python3 -m coordinator_core.invoke ceremony.scoped_git_commit '<json>'`
# as a raw subprocess from inside an already-permitted Bash-tool process, so
# the PreToolUse chain never re-inspects the real commit. Verified against
# HEAD to match none of the three prior matchers before the C5 fix.
# ---------------------------------------------------------------------------


def test_bare_scoped_git_commit_denies(monkeypatch):
    """The bare helper name, exactly like ``coordinator-safe-commit``, must
    deny for a subagent identity.
    """
    _denies(monkeypatch, 'scoped-git-commit -m "subject" -- path')


def test_path_prefixed_scoped_git_commit_denies(monkeypatch):
    """A path-prefixed spelling (e.g. installed under a settings-home bin
    directory) must still deny -- same boundary discipline as
    ``coordinator-safe-commit``'s path-prefixed forms.
    """
    _denies(
        monkeypatch,
        'bin/scoped-git-commit -m "subject" -- path',
    )


def test_scoped_git_commit_cmd_windows_twin_denies(monkeypatch):
    """The Windows ``.cmd`` launcher twin (this project's
    ``gen-launcher-shim.py`` generates one for every POSIX helper) must
    deny, matching ``coordinator-safe-commit.cmd``'s existing coverage.
    """
    fixture = "C:" + "\\" + "tools" + "\\" + 'scoped-git-commit.cmd -m "subject" -- path'
    _denies(monkeypatch, fixture)


def test_no_agent_id_em_main_loop_allows_scoped_git_commit():
    """No ``agent_id`` in the payload -> EM main-loop -> allowed exactly
    like every other committing shape this gate denies for a subagent.
    """
    payload = _payload('scoped-git-commit -m "subject" -- path', agent_id=None)
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# B-commit-matchers Finding 1 (P0, BLOCKED verdict) -- fourteen registered
# committing ops missing from _COMMITTING_OP_NAMES, several bypassing even
# the widened prefilter with no obfuscation (the exact "closed hole reopens
# on a different name" pattern this module's history keeps producing).
# One denial case per op name added by this fix.
# ---------------------------------------------------------------------------

_NEWLY_ADDED_COMMITTING_OPS = (
    "commit.exec_bit_change",
    "ceremony.post_commit_tail",
    # "fleet.archive_shipped_handoffs" REMOVED -- op key SUBSUMED (not
    # renamed), module deleted 2026-08-25 (C1b, docs/plans/2026-08-25-the-
    # handoff-auto-archive-comes-back-capped.md) -- see guard.py's own
    # _COMMITTING_OP_NAMES comment.
    "fleet.archive_release_accumulator",
    "fleet.reap_unintegrated_findings",
    "fleet.reap_integrated_findings",
    # "fleet.archive_actioned_memos" was REMOVED here for a period -- op
    # KILLED outright by PM ruling (ops/ceremony/tail_ops.py), between its
    # 2026-08-23 kill and its b8795931a rebuild; during that window it was a
    # dead allowlist entry that made the archival caller census read as nine
    # instead of eight. It is a RETURN now -- registered again, live. See
    # guard.py's own _COMMITTING_OP_NAMES comment.
    "fleet.archive_completed_handoffs",
    "fleet.archive_paper_trail",
    "fleet.archive_queue_entry",
    # "fleet.prune_closed_bugs", "handoff.archive_transition", and
    # "ceremony.commit" REMOVED (C3, docs/plans/2026-08-29-the-push-
    # subsystem-leaves-and-then-the-pipeline-can-go.md): all three are among
    # the six dead entries the `d20d56893` 200ms sweep left in
    # `_COMMITTING_OP_NAMES` -- unregistered names this fixture must not
    # assert membership for once the real set drops them. See
    # `block_subagent_commit.py`'s own allowlist comment for the removal
    # record.
    "handoff.ship_and_archive",
)


def test_newly_added_committing_ops_all_deny(monkeypatch):
    """Every op name added by the B-commit-matchers Finding 1 fix must be
    denied for a subagent identity, exactly like ``ceremony.scoped_git_
    commit`` already was -- including the three (``fleet.archive_release_
    accumulator``, ``fleet.reap_unintegrated_findings``, ``fleet.reap_
    integrated_findings``) whose names contain no ``commit`` substring at
    all and therefore exercise the prefilter path, not just the full
    invoke-matcher. (A fourth, ``fleet.archive_shipped_handoffs``, used to
    live here too -- removed 2026-08-25, op key SUBSUMED, module deleted.)
    """
    for op_name in _NEWLY_ADDED_COMMITTING_OPS:
        _denies(monkeypatch, f"python3 -m coordinator_core.invoke {op_name} '{{}}'")


def test_newly_added_committing_ops_all_members_of_the_set():
    """Sanity pin: the fixture list above is a subset of the real set, so a
    future edit to either can't silently desync the two.
    """
    missing = set(_NEWLY_ADDED_COMMITTING_OPS) - guard._COMMITTING_OP_NAMES
    assert not missing, f"fixture list references names not in the real set: {missing}"


# ---------------------------------------------------------------------------
# B-commit-matchers Finding 1b -- durability. A hand-maintained
# _COMMITTING_OP_NAMES drifts (it just did, three times in this file's
# history). This derives the population of committing ops from the real op
# registry + a static source scan for known commit-sink call sites, and
# asserts _COMMITTING_OP_NAMES covers it.
#
# STATED LIMIT (see the module docstring's 2026-08-02 part-8 entry): this is
# a single-module static source scan for a DIRECT call to a known sink
# helper. It does NOT catch an op that reaches a commit only by delegating
# to another op module's handler function (e.g. ``handoff.ship_and_archive``
# routes through ``fleet.archive_shipped_handoffs``'s own ``_handle_act``,
# which this scan cannot see without also statically tracing call graphs
# across modules) -- that class of gap needs a human re-grep, same as the
# one that found this op by hand. An honest partial guard, not a full one.
#
# AST, not substring (coordinator:code-reviewer, 2026-08-17): the scan used
# to be ``any(marker in source for marker in _COMMIT_SINK_CALL_MARKERS)`` --
# a whole-module substring test that can't tell a real call site from the
# same text sitting in a comment or a string. It produced exactly one false
# positive live: ``repo_setup.validate_target_root`` was flagged and added
# to ``_COMMITTING_OP_NAMES`` because ``bootstrap_repo.py`` has the literal
# text ``commit_scoped(`` inside a comment explaining why that module does
# NOT call it; the handler itself is read-only. The scan below instead
# parses each module and looks for an actual ``ast.Call`` node whose callee
# name matches a sink -- comments, strings, and docstrings can no longer
# match.
# ---------------------------------------------------------------------------

_COMMIT_SINK_CALL_MARKERS = (
    "archive_and_commit(",
    "rm_and_commit(",
    "commit_scoped(",
    "commit_with_message_file(",
    # Sixth pass (2026-08-27): the marker list is the ratchet's real reach,
    # and it was one name short of the op every commit now routes through.
    # `ceremony.commit` (ops/ceremony/commit_op.py :: _handler) called
    # `run_commit_pipeline(...)` directly -- a genuine sink, absent from the
    # four markers above -- so the scan walked that module, found no Call it
    # recognized, and passed while `ceremony.commit` sat outside
    # _COMMITTING_OP_NAMES. The guard meant to deny a subagent's committing-op
    # invoke therefore granted the live committer by name. Found by plan
    # triage, not by this test, which is the tell: a hand-maintained marker
    # tuple bounds a mechanical scan, so the scan is only ever as complete as
    # its least-recently-updated name.
    #
    # "run_commit_pipeline(" RETIRED (C3, docs/plans/2026-08-29-the-push-
    # subsystem-leaves-and-then-the-pipeline-can-go.md): its subject,
    # `commit_pipeline.run_commit_pipeline`, is deleted (C4 of the same
    # plan) and its one op-registered caller this marker existed to catch,
    # `ceremony.commit`, was already removed from `_COMMITTING_OP_NAMES` by
    # this same pass as one of the six dead entries the `d20d56893` sweep
    # left behind -- see `block_subagent_commit.py`'s own allowlist comment.
    # No live registered op reaches git via `run_commit_pipeline(...)` any
    # more, so the marker's stated justification evaporated before the
    # delete did; retiring it here is the stronger of the two reasons to
    # act, not merely a consequence of the delete.
    #
    # C3 (docs/plans/2026-08-27-something-must-commit-ceremony-commit-v2.md):
    # `ceremony.commit_v2` (ops/ceremony/commit_v2.py :: _handler) reaches git
    # via `commit_paths(...)` directly -- not `run_commit_pipeline` -- so it
    # needed its own marker, same lesson as the sixth-pass entry above.
    "commit_paths(",
)

#: Bare callee names derived from ``_COMMIT_SINK_CALL_MARKERS`` by stripping
#: the trailing ``(`` -- one source of truth for both the marker strings
#: (kept for their doc value in the frozenset above) and the AST scan below.
_COMMIT_SINK_CALL_NAMES = frozenset(
    marker[:-1] for marker in _COMMIT_SINK_CALL_MARKERS
)

#: Ops confirmed (by hand, this review) to reach a real commit only via
#: cross-module delegation -- outside what the static single-module source
#: scan below can see. Named here, not silently absorbed, so the scan's
#: stated limit stays checkable: removing an entry here without the scan
#: independently finding it would be a real regression in this test's own
#: coverage, not just a passing test.
_KNOWN_DELEGATION_ONLY_COMMITTING_OPS = frozenset({"handoff.ship_and_archive"})


def _source_calls_a_commit_sink(source: str) -> bool:
    """True iff ``source`` contains a real call site (``ast.Call``) whose
    callee resolves to one of ``_COMMIT_SINK_CALL_NAMES`` -- either a bare
    name (``commit_scoped(...)``) or an attribute access
    (``module.commit_scoped(...)``). Text inside a comment, string, or
    docstring is invisible to this walk by construction: ``ast.parse``
    never emits a ``Call`` node for it.
    """
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            callee = func.id
        elif isinstance(func, ast.Attribute):
            callee = func.attr
        else:
            continue
        if callee in _COMMIT_SINK_CALL_NAMES:
            return True
    return False


def test_committing_op_names_covers_registry_sink_scan():
    """Every op registered in ``OP_MODULE_MAP`` whose handler module's
    source directly calls a known commit-sink helper must be a member of
    ``_COMMITTING_OP_NAMES`` -- so a new committing op added anywhere in the
    ops tree that reaches a sink DIRECTLY fails this test instead of
    silently reopening the gate Finding 1 closed.
    """
    import importlib
    import inspect

    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    missing = []
    seen_modules: dict = {}
    for op_name, module_path in OP_MODULE_MAP.items():
        if op_name in guard._COMMITTING_OP_NAMES:
            continue
        if op_name in _KNOWN_DELEGATION_ONLY_COMMITTING_OPS:
            continue
        source = seen_modules.get(module_path)
        if source is None:
            try:
                module = importlib.import_module(module_path)
                source = inspect.getsource(module)
            except Exception:
                source = ""
            seen_modules[module_path] = source
        if _source_calls_a_commit_sink(source):
            missing.append(op_name)

    assert not missing, (
        "op(s) registered with a direct commit-sink call but absent from "
        f"_COMMITTING_OP_NAMES: {missing}"
    )


def test_commit_sink_scan_ignores_comment_and_string_occurrences():
    """Regression pin for the false positive this scan produced live
    (``repo_setup.validate_target_root``, coordinator:code-reviewer,
    2026-08-17): a sink name sitting in a comment or a string literal must
    not be mistaken for a real call site.
    """
    commented = "# this module deliberately does not call commit_scoped(...)\n"
    stringed = 'DOC = "see commit_scoped() in git_native for the real thing"\n'
    docstringed = (
        'def handler():\n'
        '    """Does not call commit_scoped() -- read only."""\n'
        "    return {}\n"
    )
    assert not _source_calls_a_commit_sink(commented)
    assert not _source_calls_a_commit_sink(stringed)
    assert not _source_calls_a_commit_sink(docstringed)

    real_call = "def handler():\n    return commit_scoped(paths, msg_path, root)\n"
    assert _source_calls_a_commit_sink(real_call)


# ---------------------------------------------------------------------------
# B-commit-matchers Finding 2 (P1) -- the invoke-CLI flag classification
# sets have no mechanism keeping them in sync with the real parser. Derive
# the real flag surface from `_build_arg_parser()` and assert the guard's
# two sets still match it exactly.
# ---------------------------------------------------------------------------


def test_invoke_flag_sets_match_real_arg_parser():
    """A future value-taking flag added to ``_build_arg_parser`` without a
    corresponding update to ``_INVOKE_FLAGS_WITH_VALUE`` would silently fail
    this guard's flag-tolerant op-resolution walk OPEN (the unrecognized
    flag's VALUE token gets treated as the op name) -- this test fails the
    moment the two surfaces diverge, in either direction.
    """
    from coordinator_core.invoke.__main__ import _build_arg_parser

    parser = _build_arg_parser()
    with_value = set()
    no_value = set()
    for action in parser._actions:
        for option_string in action.option_strings:
            if action.nargs == 0:
                no_value.add(option_string)
            else:
                with_value.add(option_string)

    assert with_value == guard._INVOKE_FLAGS_WITH_VALUE, (
        f"parser's value-taking flags {with_value} != guard's "
        f"_INVOKE_FLAGS_WITH_VALUE {guard._INVOKE_FLAGS_WITH_VALUE}"
    )
    assert no_value == guard._INVOKE_FLAGS_NO_VALUE, (
        f"parser's no-value flags {no_value} != guard's "
        f"_INVOKE_FLAGS_NO_VALUE {guard._INVOKE_FLAGS_NO_VALUE}"
    )

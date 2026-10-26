"""P-F17 replay: once the XS lane budget is spent, no heredoc shape writes a product path unseen.

Every shape below is driven through `guard_xs_lane_shell_write.check`; a shape the guard cannot
project must deny as unmeasurable, never admit.
"""

from __future__ import annotations

import pytest

from coordinator_core.session import xs_lane_budget as xb
from coordinator_core.bash_guards.tests.test_guard_xs_lane_shell_write import (  # noqa: F401
    SID,
    _run,
    _spend_the_budget,
    env,
)

_BODY = "x = 1\ny = 2\n"

SHAPES = {
    "quoted-single": "cat > {p} <<'EOF'\n" + _BODY + "EOF",
    "quoted-double": 'cat > {p} <<"EOF"\n' + _BODY + "EOF",
    "unquoted": "cat > {p} <<EOF\n" + _BODY + "EOF",
    "backslash-delim": "cat > {p} <<\\EOF\n" + _BODY + "EOF",
    "dash-tabs": "cat > {p} <<-'EOF'\n\t" + _BODY.replace("\n", "\n\t", 1) + "EOF",
    "here-string": "cat > {p} <<< 'x = 1'",
    "piped-to-python": "python3 - <<'PY' | tee {p}\nprint('x = 1')\nPY",
    "heredoc-to-tee": "tee {p} <<'EOF'\n" + _BODY + "EOF",
    "heredoc-to-tee-append": "tee -a {p} <<'EOF'\n" + _BODY + "EOF",
    "python-writes": "python3 - <<'PY'\nopen('{p}', 'w').write('x')\nPY",
    "cat-then-trailing-cmd": "cat > {p} <<'EOF'\n" + _BODY + "EOF\necho done",
    "two-heredocs": "cat > {p} <<'A'\na\nA\ncat > {p}2 <<'B'\nb\nB",
    "pipe-into-cat": "printf 'x = 1\\n' | cat > {p}",
    "echo-redirect": "echo 'x = 1' > {p}",
    "cat-stdin-heredoc-to-dd": "dd of={p} <<'EOF'\nx\nEOF",
    "bash-c-heredoc": "bash -c \"cat > {p} <<'EOF'\nx = 1\nEOF\"",
    "fd-redirect": "cat 1> {p} <<'EOF'\nx = 1\nEOF",
    "noclobber-override": "cat >| {p} <<'EOF'\nx = 1\nEOF",
    "both-redirect": "cat &> {p} <<'EOF'\nx = 1\nEOF",
    "redirect-first": "> {p} cat <<'EOF'\nx = 1\nEOF",
    "redirect-no-space": "cat >{p} <<'EOF'\nx = 1\nEOF",
    "quoted-target": "cat > '{p}' <<'EOF'\nx = 1\nEOF",
    "subshell": "( cat > {p} <<'EOF'\nx = 1\nEOF\n)",
    "brace-group": "{{ cat > {p} <<'EOF'\nx = 1\nEOF\n}}",
    "env-prefix": "FOO=1 cat > {p} <<'EOF'\nx = 1\nEOF",
    "heredoc-in-and-chain": "true && cat > {p} <<'EOF'\nx = 1\nEOF",
    "tee-pipeline": "cat <<'EOF' | tee {p}\nx = 1\nEOF",
    "tee-pipeline-sudo": "cat <<'EOF' | sudo tee {p} >/dev/null\nx = 1\nEOF",
    "sh-stdin": "sh <<'EOF'\necho x > {p}\nEOF",
    "node-stdin": "node - <<'EOF'\nrequire('fs').writeFileSync('{p}','x')\nEOF",
    "perl-stdin": "perl <<'EOF'\nopen F, '>{p}'; print F 'x';\nEOF",
    "ruby-stdin": "ruby <<'EOF'\nFile.write('{p}','x')\nEOF",
}


@pytest.mark.parametrize("target", ["notes.md", "scratch/probe.py"])
def test_interpreter_heredoc_on_non_product_path_is_still_admitted(env, target):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    _spend_the_budget(root)
    cmd = f"sh <<'EOF'\necho x > {root / target}\nEOF"
    assert _run(root, cmd) is None


def test_literal_heredoc_body_mentioning_another_path_is_not_rescanned(env):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    cmd = f"cat > {root / 'notes.md'} <<'EOF'\nsee {root / 'pkg' / 'other.py'}\nEOF"
    assert _run(root, cmd) is None


@pytest.mark.parametrize("name", ["quoted-single", "heredoc-to-tee", "python-writes"])
def test_unfired_s_plus_session_denies_heredoc_to_product_path(env, monkeypatch, name):
    from coordinator_core import warp_live
    from coordinator_core.hooks import flag_em_poll_in_flight as fl

    root, _sha = env
    monkeypatch.setattr(xb, "touched_s_or_larger", lambda sid, repo_root: True)
    monkeypatch.setattr(fl, "session_workflow_run_ids", lambda sid: [])
    monkeypatch.setattr(warp_live, "live_run_for_session", lambda *a, **k: None)
    cmd = SHAPES[name].format(p=str(root / "pkg" / "mod.py"))
    record = _run(root, cmd)
    assert record is not None and record.verdict_kind == "deny", name
    assert "pkg/mod.py" not in (xb.read_lane(root, SID) or {}).get("paths", [])


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_spent_budget_denies_every_heredoc_shape(env, name):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    _spend_the_budget(root)
    cmd = SHAPES[name].format(p=str(root / "pkg" / "mod.py"))
    record = _run(root, cmd)
    assert record is not None and record.verdict_kind == "deny", name
    assert "pkg/mod.py" not in xb.read_lane(root, SID)["paths"]

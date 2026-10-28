"""A state/sizings path inside a quoted argument or a pure ``python -c`` string
literal is a mention, not a write; every real write sink into it still denies."""
import pytest

from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import (
    _is_pure_python_c_body,
    _sizing_write_denied,
)

S = "state/sizings/2026-10-23-081e95-2-x.yaml"
REVISE = "coordinator-invoke workflow.revise"


def _py(body: str) -> str:
    return f"{REVISE} \"$(python3 -c '{body}')\""


ALLOWED = [
    _py(f'import json;print(json.dumps({{"run_id":"r","note":"reads {S} now"}}))'),
    _py(f'import json,sys;print(json.dumps({{"note":"see {S}"}}))'),
    f"{REVISE} '{{\"run_id\":\"r\",\"note\":\"reads {S}\"}}'",
    f'{REVISE} "$(cat note.json)" --note "reads {S}"',
    f'echo "see {S}"',
]

DENIED = [
    f"echo x > {S}",
    f"echo x >> {S}",
    f"printf x | tee {S}",
    f"sed -i 's/a/b/' {S}",
    f"cp /tmp/a.yaml {S}",
    f"mv /tmp/a.yaml {S}",
    f"python3 -c \"open('{S}','w').write('x')\"",
    _py(f"open('{S}','w').write('x')"),
    _py(f"from pathlib import Path;Path('{S}').write_text('x')"),
    _py(f"import os;os.system('echo x > {S}')"),
    _py(f"import subprocess;subprocess.run('tee {S}',shell=True)"),
    _py(f"print('{S}')") + f" > {S}",
    _py(f"print('{S}')") + f" && echo x > {S}",
    f'{REVISE} "$(python3 -c \'print(1)\')" > {S}',
]


P = "state/sizings/2026-10-26-603728-2-slice-2.yaml"
_DIRECT_BODY = "import json; print(json.dumps({'run_id':'r','note':'see %s now'}))" % P
_REVISE_TAIL = f'{REVISE} "$(cat $S/rev.json)"'

DIRECT_ALLOWED = [
    f'S=/tmp/x; python3 -c "{_DIRECT_BODY}" > $S/rev.json\n{_REVISE_TAIL}',
    f'python3 -c "{_DIRECT_BODY}" > /tmp/rev.json; {REVISE} "$(cat /tmp/rev.json)"',
    f"python3 -c 'import json;print(json.dumps({{\"n\":\"{P}\"}}))' > /tmp/rev.json",
]

DIRECT_DENIED = [
    f'python3 -c "{_DIRECT_BODY}" > {P}',
    f'python3 -c "print(1)" > {P}',
    f"python3 -c \"open('{P}','w').write('x')\" > /tmp/o",
    f'python3 -c "import os; print(1)" /dev/null; echo x > {P}',
]


@pytest.mark.parametrize("cmd", DIRECT_ALLOWED)
def test_direct_python_c_mention_with_scratch_redirect_is_allowed(cmd):
    assert not _sizing_write_denied(cmd)


@pytest.mark.parametrize("cmd", DIRECT_DENIED)
def test_direct_python_c_with_real_sizings_write_still_denies(cmd):
    assert _sizing_write_denied(cmd)


IMPURE_BODIES = [
    "__import__('os').system('x')",
    "import json;print(1,file=open('x'))",
    "from json import dumps;print(dumps(1))",
    "import json;getattr(json,'dumps')",
    "import base64;print(1)",
    "import json as j;print(1)",
    "import sys;print(sys.modules['x'])",
]


@pytest.mark.parametrize("body", IMPURE_BODIES)
def test_impure_python_c_body_is_not_neutralized(body):
    assert not _is_pure_python_c_body(body)


def test_json_only_body_is_pure():
    assert _is_pure_python_c_body("import json;print(json.dumps({'a':1}))")


@pytest.mark.parametrize("cmd", ALLOWED)
def test_path_mention_inside_a_string_is_allowed(cmd):
    assert not _sizing_write_denied(cmd)


@pytest.mark.parametrize("cmd", DENIED)
def test_real_write_into_sizings_still_denies(cmd):
    assert _sizing_write_denied(cmd)


def _heredoc(body: str, quote: str = "'") -> str:
    return f"python3 - <<{quote}EOF2{quote}\n{body}\nEOF2"


HEREDOC_READS = [
    _heredoc(f"from pathlib import Path\nplan = Path('docs/plans/x.md').read_text()\nprint('sizing: {S}')"),
    _heredoc(f"import json\nopen('scratch/out.json', 'w').write(json.dumps({{'s': '{S}'}}))"),
    _heredoc(f"import yaml\nd = yaml.safe_load(open('{S}'))\nopen('scratch/o.json', 'w').write(str(d))"),
    _heredoc(f"print(open('docs/plans/x.md').read())\nprint('{S}')", quote=""),
    f"python3 - <<EOF2 > scratch/out.txt\nprint('{S}')\nEOF2",
    f"cat > scratch/p.json <<'EOF'\n{{\"p\": \"{S}\"}}\nEOF",
]


@pytest.mark.parametrize("cmd", HEREDOC_READS)
def test_python_heredoc_or_scratch_json_naming_a_sizing_is_not_a_write(cmd):
    assert not _sizing_write_denied(cmd)


def test_heredoc_that_writes_the_sizing_still_detected():
    assert _sizing_write_denied(_heredoc(f"open('{S}', 'w').write('x')"))


def test_check_returns_none_for_a_sizing_write_because_the_deny_is_clamped_to_a_log():
    from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import check

    payload = {"tool_name": "Bash", "tool_input": {"command": f"echo x > {S}"}, "session_id": "s"}
    assert check(payload, ["CLAUDE.md"]) is None

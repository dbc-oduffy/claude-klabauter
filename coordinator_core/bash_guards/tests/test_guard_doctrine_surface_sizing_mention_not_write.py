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

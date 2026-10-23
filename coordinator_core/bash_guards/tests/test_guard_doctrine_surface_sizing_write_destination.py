"""The sizing-store leg judges the real write destination: a variable-path
redirect, a read-only pipeline and quoted store text are not writes into it;
literal redirect, tee, sed -i, mv and cp into the store still deny."""
import pytest

from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import (
    _sizing_write_denied,
    check,
)

Z = "state/sizings"
GREP_PIPE = f'grep -l "^status: superseded" {Z}/*.yaml | head -1 | xargs grep -n -i "superseded_by\\|supersed" | head -5'

ADMITTED = [
    f'L=/tmp/l && sizing-assemble --write {Z}/a.yaml > "$L/log.txt"',
    f"L=/tmp/l; sizing-assemble --write {Z}/a.yaml > $L/log.txt 2>&1",
    f"sizing-assemble --write {Z}/a.yaml > /tmp/log.txt",
    GREP_PIPE,
    f"sizing-assemble --write {Z}/a.yaml --intent '{GREP_PIPE.replace(chr(39), '')}'",
    f'sizing-assemble --write {Z}/a.yaml --intent "find: grep -l x {Z}/*.yaml | head -1 | xargs grep -n y"',
    f"S=/tmp/s && cat > $S/f.md <<'EOF'\nsee {Z}/a.yaml and {Z}/b.yaml\nEOF",
    f"S=/tmp/s\ncat > $S/f.md <<'EOF'\nsee {Z}/a.yaml\nEOF",
    f"sizing-assemble --write {Z}/a.yaml",
    f"sizing-assemble --write {Z}/a.yaml > /tmp/out.txt",
    f"sizing-assemble --write {Z}/a.yaml --intent \"$(cat note.txt)\"",
    f"sizing-assemble --write {Z}/a.yaml && cat {Z}/a.yaml",
    f"cat {Z}/a.yaml | head -3 > /tmp/head.txt",
    f"ls {Z}/*.yaml | xargs -I{{}} grep -l x {{}}",
    "sort -o /tmp/out.txt /tmp/in.txt",
    "uniq /tmp/in.txt /tmp/out.txt",
    f"cd {Z} && grep -l x *.yaml 2>/dev/null",
    f"cd {Z} && cat a.yaml > /tmp/copy.txt",
    f"sort {Z}/a.yaml | uniq",
    f"cd {Z} && cd /tmp && echo x > a.txt",
]

DENIED = [
    f"echo x > {Z}/a.yaml",
    f"echo x >> {Z}/a.yaml",
    f"echo x | tee {Z}/a.yaml",
    f"sed -i s/a/b/ {Z}/a.yaml",
    f"mv a.yaml {Z}/a.yaml",
    f"cp a.yaml {Z}/a.yaml",
    f"S={Z} && echo x > $S/a.yaml",
    f"S=/tmp/s && cat > {Z}/f.md <<'EOF'\nhi\nEOF",
    f'L=/tmp/l && sizing-assemble --write {Z}/a.yaml > "$L/log.txt"; echo x > {Z}/b.yaml',
    f'D={Z} && sizing-assemble --write {Z}/a.yaml > "$D/log.txt"',
    f"sizing-assemble --write {Z}/a.yaml > {Z}/log.txt",
    f"sizing-assemble --write {Z}/a.yaml > $UNSET/log.txt",
    f"grep x {Z}/*.yaml | xargs sed -i s/a/b/",
    f"grep -l x {Z}/*.yaml | tee {Z}/o.txt",
    f"cat {Z}/a.yaml | head -1 | xargs -I{{}} cp {{}} {Z}/z.yaml",
    f"grep x {Z}/*.yaml | xargs rm",
    f"cat {Z}/a.yaml | xargs python3 s.py",
    f"cat {Z}/a.yaml > /tmp/x; echo y > {Z}/a.yaml",
    f"cd {Z} && echo x > a.yaml",
    f"sort -o {Z}/a.yaml /tmp/in.txt",
    f"uniq /tmp/in.txt {Z}/a.yaml",
    f"echo x &> {Z}/a.yaml",
    f"sort --output={Z}/a.yaml /tmp/in.txt",
    f"sort -o{Z}/a.yaml /tmp/in.txt",
    f"pushd {Z} && echo x > a.yaml",
    f"cd {Z} && sed -i s/a/b/ a.yaml",
    f"cd {Z} && echo x | tee a.yaml",
    f"cd {Z} && uniq /tmp/in.txt a.yaml",
    f"cd {Z} && sort -o a.yaml /tmp/in.txt",
    f"sort -uo {Z}/a.yaml /tmp/in.txt",
    f"uniq -f 1 /tmp/in.txt {Z}/a.yaml",
]


@pytest.mark.parametrize("cmd", ADMITTED)
def test_command_writing_outside_the_store_is_admitted(cmd):
    assert not _sizing_write_denied(cmd)


@pytest.mark.parametrize("cmd", DENIED)
def test_command_writing_into_the_store_still_denies(cmd):
    assert _sizing_write_denied(cmd)


@pytest.mark.parametrize("cmd", [DENIED[0], DENIED[2], DENIED[3]])
def test_check_entrypoint_denies_store_write(cmd):
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert check(payload, None) is not None

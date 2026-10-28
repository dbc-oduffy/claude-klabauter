"""A state/sizings path passed as a flag argument to an interpreter-run script is
a read, not a write, under Bash and PowerShell; a real redirect, tee, cp/mv or
heredoc into the store still denies."""
import pytest

from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import check

S = "state/sizings/2026-10-27-x.yaml"
SURFACES = ["CLAUDE.md"]
EMIT = "coordinator/skills/roadmap-blitz/emit-roadmap-fire.py"


def _denied(cmd: str, tool: str = "Bash") -> bool:
    out = check({"tool_name": tool, "tool_input": {"command": cmd}}, SURFACES)
    return out is not None


ALLOWED = [
    f"python3 {EMIT} --sizing {S} --interaction-mode hands-on",
    f"python3 {EMIT} --out scratch/roadmap-blitz/fire.json --sizing {S} --interaction-mode hands-on",
    f"python3 {EMIT} --sizing={S} --interaction-mode hands-on",
    f"python3 {EMIT} --sizing '{S}' --interaction-mode hands-on",
    f"python3 {EMIT} --sizing {S} --interaction-mode hands-on > scratch/roadmap-blitz/out.txt",
    f"python {EMIT} --sizing {S}",
]

DENIED = [
    f"python3 {EMIT} --sizing {S} > {S}",
    f"python3 {EMIT} --sizing {S} --interaction-mode hands-on | tee {S}",
    f"python3 {EMIT} --sizing {S} && cp scratch/a.yaml {S}",
    f"python3 {EMIT} --sizing {S} && mv scratch/a.yaml {S}",
    f"python3 {EMIT} --sizing {S}\ncat > {S} <<'EOF'\nbody\nEOF",
    f"python3 {EMIT} --sizing {S} >> {S}",
    f"python3 {EMIT} --out {S}",
    f"python3 {EMIT} --output-file={S}",
]


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("cmd", ALLOWED)
def test_flag_arg_store_path_is_allowed(cmd, tool):
    assert not _denied(cmd, tool)


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("cmd", DENIED)
def test_real_store_write_still_denies(cmd, tool):
    assert _denied(cmd, tool)

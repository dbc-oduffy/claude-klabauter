"""Registered worker mode shares the coordinator-owned --worker implementation."""
from pathlib import Path

from coordinator_core.codex_runner.contract import RunResult, STATUS_FAILED, STATUS_OK
from coordinator_core.worker_contracts.worker import launch
from coordinator_core.codex_runner.output import read_output


def run(req, run_dir, on_spawn):
    run_dir = Path(run_dir)
    pids = []
    def spawned(pid):
        pids.append(pid)
        on_spawn(pid)
    output_path = run_dir / "worker.stdout"
    with output_path.open("w", encoding="utf-8") as output:
        code = launch(Path(req.repo_root), Path(req.task_file).read_text(encoding="utf-8"),
                      req.assignment, stdout=output, on_spawn=spawned)
    text, output_usage = read_output(output_path)
    return RunResult(run_id=run_dir.name, status=STATUS_OK if code == 0 else STATUS_FAILED,
                     codex_pid=pids[0] if pids else None, output=text, usage=output_usage,
                     error=None if code == 0 else f"Codex worker exited {code}")

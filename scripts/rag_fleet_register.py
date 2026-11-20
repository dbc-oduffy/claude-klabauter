#!/usr/bin/env python3
"""Detached worker: register every fleet repo with example-retrieval-repo, start its index, install extractors.

Usage: rag_fleet_register.py <plan.json>

The plan is `coordinator_core.install.rag_fleet_registration.registration_plan(...)` plus
`status_path` and `log_path`. Every example-retrieval-repo call goes through example-retrieval-repo's own CLI under its
pinned interpreter; this worker never writes rag's store. Progress lands in the readiness JSON at
`status_path` (verdict: indexing | ready | not_ready). Bulk background work, started only by setup.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

RUN_TIMEOUT_S = 3600.0
PROBE_TIMEOUT_S = 30.0
_TIMEOUT_RC = 124
_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def write_status(status: dict, path: Path) -> None:
    status["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    states = {t.get("state") for t in status["targets"].values()}
    if status.get("phase") != "done":
        status["verdict"] = "indexing"
    elif states <= {"ready"} and status.get("extractor", {}).get("rc", 0) == 0:
        status["verdict"] = "ready"
    else:
        status["verdict"] = "not_ready"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(status, indent=2), encoding="utf-8", newline="\n")
    tmp.replace(path)


def _run(argv: list[str], log, cwd: str | None = None) -> int:
    log.write(f"\n$ {' '.join(argv)}\n".encode())
    log.flush()
    try:
        return subprocess.run(
            argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            timeout=RUN_TIMEOUT_S, cwd=cwd, **_NO_CONSOLE,
        ).returncode
    except subprocess.TimeoutExpired:
        log.write(f"timed out after {RUN_TIMEOUT_S:.0f}s\n".encode())
        log.flush()
        return _TIMEOUT_RC
    except OSError as exc:
        log.write(f"spawn failed: {exc!r}\n".encode())
        log.flush()
        return 127


def detect_kind(interpreter: str, rag_root: str, root: str) -> str:
    """example-retrieval-repo's own kind detection for `root`; `index --non-interactive` refuses any other."""
    probe = (
        "import sys;from pathlib import Path;"
        "from example_retrieval_repo_core.project_type import detect;print(detect(Path(sys.argv[1])))"
    )
    try:
        out = subprocess.run(
            [interpreter, "-c", probe, root], cwd=rag_root, capture_output=True, text=True,
            timeout=PROBE_TIMEOUT_S, **_NO_CONSOLE,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "generic"
    return out.stdout.strip() or "generic" if out.returncode == 0 else "generic"


def run(plan: dict) -> dict:
    status_path = Path(plan["status_path"])
    log_path = Path(plan.get("log_path") or status_path.with_name("worker.log"))
    status = {
        "rag_root": plan["rag_root"],
        "log": str(log_path),
        "pid": os.getpid(),
        "phase": "register",
        "targets": {slug: {"root": root, "state": "queued"} for slug, root in plan["roots"].items()},
    }
    write_status(status, status_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        for slug, entry in status["targets"].items():
            started = time.monotonic()
            entry["state"] = "registering"
            write_status(status, status_path)
            rc = _run(plan["register_argv"][slug], log)
            entry["registered"] = rc == 0
            if rc != 0:
                entry.update(state="failed", rc=rc, reason="register-only failed; see log")
                write_status(status, status_path)
                continue
            entry["state"] = "indexing"
            write_status(status, status_path)
            argv = list(plan["index_argv"][slug])
            if plan.get("index_needs_kind"):
                argv += ["--kind", detect_kind(plan["interpreter"], plan["rag_root"], entry["root"])]
            rc = _run(argv, log)
            entry.update(state="ready" if rc == 0 else "failed", rc=rc,
                         elapsed_s=round(time.monotonic() - started, 1))
            write_status(status, status_path)
        extractor = plan.get("extractor_argv")
        if extractor:
            status["phase"] = "extractor"
            write_status(status, status_path)
            status["extractor"] = {"rc": _run(extractor, log)}
        status["phase"] = "done"
        write_status(status, status_path)
    return status


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: rag_fleet_register.py <plan.json>", file=sys.stderr)
        return 2
    try:
        plan = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[rag_fleet_register] unreadable plan {argv[0]}: {exc}", file=sys.stderr)
        return 2
    return 0 if run(plan)["verdict"] == "ready" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

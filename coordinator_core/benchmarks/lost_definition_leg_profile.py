"""Process time and spawns of the landing integrity import-closure leg and the lost-definition leg.

Fixture: a tmp git repo whose HEAD tracks `MODULES` modules; each is then modified in the worktree
to define a new name. `IMPORTERS` untracked `.py` files, each importing such a name, are the
landing. `import_closure_findings` is the base leg (present at d320785cd5); `lost_definition_findings`
is the end leg (absent at the base, reported `present: false` there). Process time is
`time.process_time` (this process); spawns count `subprocess.Popen` constructions.

    python -m coordinator_core.benchmarks.lost_definition_leg_profile [--json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402
from coordinator_core.git.run import run_git  # noqa: E402

BAR_MS = 500.0
IMPORTERS = 20
MODULES = 10
PKG = "lostdefpkg"


def _git(root: Path, *args: str) -> None:
    res = run_git(["-C", str(root), *args])
    if res.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {res.stderr.strip()}")


def build_fixture(root: Path, importers: int = IMPORTERS, modules: int = MODULES) -> list[str]:
    """Init a repo at `root`; return the landed (untracked importer) paths. The targets are modified, not landed."""
    (root / PKG).mkdir(parents=True)
    (root / PKG / "__init__.py").write_text("")
    for m in range(modules):
        (root / PKG / f"m{m}.py").write_text(f"def base{m}():\n    return {m}\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    for m in range(modules):
        with (root / PKG / f"m{m}.py").open("a") as fh:
            fh.write(f"\n\ndef added{m}():\n    return {m}\n")
    paths = []
    for i in range(importers):
        m = i % modules
        rel = f"{PKG}/user{i}.py"
        (root / rel).write_text(f"from {PKG}.m{m} import added{m}\n\n\ndef run():\n    return added{m}()\n")
        paths.append(rel)
    return paths


class _SpawnCounter:
    """Counts `subprocess.Popen` constructions while active."""

    def __init__(self) -> None:
        self.count = 0
        self._orig = subprocess.Popen.__init__

    def __enter__(self) -> "_SpawnCounter":
        counter, orig = self, self._orig

        def counted(popen, *a, **kw):
            counter.count += 1
            return orig(popen, *a, **kw)

        subprocess.Popen.__init__ = counted  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc) -> None:
        subprocess.Popen.__init__ = self._orig  # type: ignore[method-assign]


def _measure(label: str, fn, root: Path, paths: list[str]) -> dict:
    fn(root, paths, [])  # warm imports so the row measures the leg, not module load
    with _SpawnCounter() as sc:
        t0 = time.process_time()
        found = fn(root, paths, [])
        ms = (time.process_time() - t0) * 1000.0
    return {"leg": label, "present": True, "process_time_ms": round(ms, 2), "spawns": sc.count, "findings": len(found)}


def profile(root: Path) -> list[dict]:
    """Rows for the base leg and the end leg over a fresh fixture under `root`."""
    from coordinator_core.ops.dispatch_emit import landing_integrity as li

    paths = build_fixture(root)
    rows = [_measure("import_closure_findings", li.import_closure_findings, root, paths)]
    if hasattr(li, "lost_definition_findings"):
        rows.append(_measure("lost_definition_findings", li.lost_definition_findings, root, paths))
    else:
        rows.append({"leg": "lost_definition_findings", "present": False})
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    declare_benchmark_origin()
    with tempfile.TemporaryDirectory(prefix="lostdef-profile-") as tmp:
        rows = profile(Path(tmp))
    if args.json:
        print(json.dumps(rows))
    else:
        for r in rows:
            print(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

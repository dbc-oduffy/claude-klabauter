"""Measure one unimatrix-autolink run() on a healthy box: subprocess spawns and process time.

Usage: python probe_autolink_healthy_path.py
Prints `spawns=<n> process_ms=<f>`; process_ms is self CPU time plus reaped children's (POSIX).
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]
ALIAS = "probebox"

FAKE_READER = '''import sys
vals = {"unimatrix.far_boxes": "%s", "unimatrix.keys.%s": %r}
if len(sys.argv) > 2 and sys.argv[1] == "get" and sys.argv[2] in vals:
    print(vals[sys.argv[2]])
'''


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    with tempfile.TemporaryDirectory(prefix="autolink-probe-") as tmp:
        home = Path(tmp) / "settings"
        (home / "bin").mkdir(parents=True)
        (home / "unimatrix").mkdir()
        key = home / "probe.key"
        key.write_text("not-a-real-key\n")
        (home / "bin" / "_machine_local.py").write_text(FAKE_READER % (ALIAS, ALIAS, str(key)))
        os.environ["COORDINATOR_SETTINGS_HOME"] = str(home)
        os.environ.pop("UNIMATRIX_TUNNEL_REMOTE", None)

        autolink = load("unimatrix-autolink")
        roster = autolink._roster()
        pid = os.getpid()
        stamp = roster.proc_start(pid)
        rec = {role: {"pid": pid, "procStart": stamp} for role, _ in autolink.SUPERVISORS}
        (home / "unimatrix" / f"autolink-{autolink.slug(ALIAS)}.json").write_text(json.dumps(rec))

        spawns = [0]
        real_popen = subprocess.Popen

        class CountingPopen(real_popen):
            def __init__(self, *a, **kw):
                spawns[0] += 1
                super().__init__(*a, **kw)

        try:
            import resource
        except ImportError:
            resource = None

        def children_cpu():
            if resource is None:
                return 0.0
            ru = resource.getrusage(resource.RUSAGE_CHILDREN)
            return ru.ru_utime + ru.ru_stime

        autolink.run()  # warm run: writes the far-box cache the measured steady-state run reads
        subprocess.Popen = CountingPopen
        try:
            c0, t0 = children_cpu(), time.process_time()
            autolink.run()
            t1, c1 = time.process_time(), children_cpu()
        finally:
            subprocess.Popen = real_popen
        print(f"spawns={spawns[0]} process_ms={((t1 - t0) + (c1 - c0)) * 1000:.1f}")


if __name__ == "__main__":
    main()

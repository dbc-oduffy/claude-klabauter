"""Child-process launch template that reads the fixture repo as onboarded, shared by the
terminal_commit benchmarks and their standing gate."""

# The conftest pin of `hosts_coordinator_state` does not cross the process boundary, and an
# unonboarded repo withholds every run artifact (`repo-not-opted-in`).
ONBOARDED_PIN = (
    "from coordinator_core.ops.dispatch_emit import process_artifact_policy as p\n"
    "p.hosts_coordinator_state = lambda root: True\n"
)

_ONBOARDED_LAUNCH = (
    "import runpy, sys\n"
    + ONBOARDED_PIN
    + "driver = sys.argv[1]\n"
    "sys.argv = [driver, *sys.argv[2:]]\n"
    "runpy.run_module(driver, run_name='__main__', alter_sys=True)\n"
)

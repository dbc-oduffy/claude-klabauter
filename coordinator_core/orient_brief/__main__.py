"""`python -m coordinator_core.orient_brief` entry; SW retargets the CLI route to the same `main`."""
import sys

from coordinator_core.orient_brief import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

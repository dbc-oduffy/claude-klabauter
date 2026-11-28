"""Worker half of a host-issued landing: wait for the host's terminal reply."""
import json
from pathlib import Path
import sys
import time


def main(path):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            data = Path(path).read_bytes()
            result = json.loads(data)
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(.05)
            continue
        print(json.dumps(result))
        return 0 if result.get('committed') else 1
    raise SystemExit('host landing receipt timed out')


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))

"""Stand-in for `grok -p <prompt> --output-format streaming-json ...` used by codex_runner tests.

Run as `[sys.executable, fake_grok.py, ...grok flags]`; tests pass that argv prefix to
grok.review_run so the real CLI is never touched. The fake replays a fixture to stdout and exits.

Env:
  FAKE_GROK_JSONL     fixture replayed to stdout line by line
  FAKE_GROK_ARGV_OUT  path receiving the argv as JSON
  FAKE_GROK_STDERR    text written to stderr at start
  FAKE_GROK_EXIT      exit code after the replay (default 0)
  FAKE_GROK_HANG      if set, print nothing and sleep (silent agent)
"""
import json
import os
import sys
import time


def main(argv):
    out = os.environ.get("FAKE_GROK_ARGV_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(argv, f)
    err = os.environ.get("FAKE_GROK_STDERR")
    if err:
        sys.stderr.write(err)
        sys.stderr.flush()
    if os.environ.get("FAKE_GROK_HANG"):
        time.sleep(60)
        return 0
    jsonl = os.environ.get("FAKE_GROK_JSONL")
    if jsonl:
        with open(jsonl, encoding="utf-8") as f:
            for line in f:
                sys.stdout.write(line if line.endswith("\n") else line + "\n")
                sys.stdout.flush()
    return int(os.environ.get("FAKE_GROK_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

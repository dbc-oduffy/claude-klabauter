"""Stand-in for `codex exec --json ... [-o last.json]` used by codex_runner tests.

Run as `[sys.executable, fake_codex.py, exec, ...codex flags]`; tests monkeypatch
resolve_codex to return that argv prefix so the real CLI is never touched.

Env:
  FAKE_CODEX_JSONL      path of a JSONL fixture replayed line by line to stdout
  FAKE_CODEX_LAST_JSON  path whose bytes are written to the file named by -o/--output-last-message
  FAKE_CODEX_EXIT       exit code (default 0)
  FAKE_CODEX_STDERR     text written to stderr
  FAKE_CODEX_ARGV_OUT   path receiving the argv as JSON (lets tests assert the spawn form)
"""
import json
import os
import sys


def _out_path(argv):
    for i, a in enumerate(argv):
        if a in ("-o", "--output-last-message") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--output-last-message="):
            return a.split("=", 1)[1]
    return None


def main(argv):
    argv_out = os.environ.get("FAKE_CODEX_ARGV_OUT")
    if argv_out:
        with open(argv_out, "w", encoding="utf-8") as f:
            json.dump(argv, f)
    jsonl = os.environ.get("FAKE_CODEX_JSONL")
    if jsonl:
        with open(jsonl, encoding="utf-8") as f:
            for line in f:
                sys.stdout.write(line if line.endswith("\n") else line + "\n")
        sys.stdout.flush()
    last_src = os.environ.get("FAKE_CODEX_LAST_JSON")
    dest = _out_path(argv)
    if last_src and dest:
        with open(last_src, "rb") as f:
            data = f.read()
        with open(dest, "wb") as f:
            f.write(data)
    err = os.environ.get("FAKE_CODEX_STDERR")
    if err:
        sys.stderr.write(err)
    return int(os.environ.get("FAKE_CODEX_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

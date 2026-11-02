"""Stand-in for `agy --input-format stream-json --output-format stream-json -p= ...` used by codex_runner tests.

Run as `[sys.executable, fake_agy.py, ...agy flags]`; tests monkeypatch agy.resolve_agy to
return that argv prefix so the real CLI is never touched. Every stdin line is one user turn;
the fake replays fixture lines up to and including the next {"event":"result"} line.

Env:
  FAKE_AGY_JSONL      fixture replayed turn by turn (a result line ends a turn)
  FAKE_AGY_ARGV_OUT   path receiving the argv as JSON
  FAKE_AGY_STDIN_OUT  path receiving every stdin line as received (appended)
  FAKE_AGY_PID_OUT    path receiving the pid (appended, one per line)
  FAKE_AGY_STDERR     text written to stderr at start
  FAKE_AGY_EXIT       exit code once stdin closes, or immediately when FAKE_AGY_DIE_AFTER=0
  FAKE_AGY_DIE_AFTER  die (exit FAKE_AGY_EXIT) after answering this many turns
  FAKE_AGY_HANG       if set, read the first turn and never answer
"""
import json
import os
import sys
import time


def _turns(path):
    turns, cur = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            cur.append(line)
            try:
                if json.loads(line).get("event") == "result":
                    turns.append(cur)
                    cur = []
            except ValueError:
                pass
    return turns


def main(argv):
    out = os.environ.get("FAKE_AGY_ARGV_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(argv, f)
    pid_out = os.environ.get("FAKE_AGY_PID_OUT")
    if pid_out:
        with open(pid_out, "a", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")
    err = os.environ.get("FAKE_AGY_STDERR")
    if err:
        sys.stderr.write(err)
        sys.stderr.flush()
    code = int(os.environ.get("FAKE_AGY_EXIT", "0"))
    die_after = os.environ.get("FAKE_AGY_DIE_AFTER")
    if die_after == "0":
        return code
    jsonl = os.environ.get("FAKE_AGY_JSONL")
    turns = _turns(jsonl) if jsonl else []
    stdin_out = os.environ.get("FAKE_AGY_STDIN_OUT")
    answered = 0
    for line in sys.stdin:
        if stdin_out:
            with open(stdin_out, "a", encoding="utf-8") as f:
                f.write(line if line.endswith("\n") else line + "\n")
        if os.environ.get("FAKE_AGY_HANG"):
            time.sleep(60)
        if answered < len(turns):
            for out_line in turns[answered]:
                sys.stdout.write(out_line + "\n")
            sys.stdout.flush()
        answered += 1
        if die_after is not None and answered >= int(die_after):
            return code
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

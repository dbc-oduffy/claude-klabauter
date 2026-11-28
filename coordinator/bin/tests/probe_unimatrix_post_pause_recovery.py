"""Print one `box=... link=... stop_req_age=... disabled=...` line per far box, then one `autolink ...` line; read-only."""
import calendar
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
PASS_ARTIFACTS = ("autolink-boxes.json", "installed-copies-stamp.json", "pull-sources-stamp.json")
LOG_TAIL_BYTES = 512 * 1024
LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (.*)$")


def load(rel):
    path = BIN / rel
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def key(name):
    user, at, host = str(name).strip().rpartition("@")
    host = host.strip().lower()
    host = host[:-len(".local")] if host.endswith(".local") else host
    return f"{user.strip().lower()}@{host}" if at and user.strip() else host


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def age(seconds):
    if seconds is None:
        return "none"
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def log_events(sdir):
    """[(epoch, text)] of the autolink.log tail."""
    path = sdir / "autolink.log"
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(fh.tell() - LOG_TAIL_BYTES, 0))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        m = LOG_LINE.match(line)
        if m:
            out.append((float(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ"))), m.group(2)))
    return out


def log_subject(text):
    m = re.match(r"far box (\S+)", text)
    return key(m.group(1).rstrip(":")) if m else None


def box_aliases(sdir):
    """{key: alias}: the membership boxes cache plus every supervisor record's alias."""
    out = {}
    for alias in read_json(sdir / "autolink-boxes.json").get("aliases") or []:
        out[key(alias)] = alias
    for p in sorted(sdir.glob("autolink-*.json")):
        stem = p.stem[len("autolink-"):]
        if stem in ("boxes", "router") or stem.endswith(".backoff"):
            continue
        alias = stem.replace("_at_", "@")
        out.setdefault(key(alias), alias)
    return out


def link_states(sdir):
    out = {}
    for p in (sdir / "link-state").glob("*.json"):
        rec = read_json(p)
        if rec.get("alias"):
            out[key(rec["alias"])] = rec
    return out


def stop_requests(sdir):
    """{pid: requestedAt} for each stop-<pid>.json present."""
    out = {}
    for p in sdir.glob("stop-*.json"):
        rec = read_json(p)
        if isinstance(rec.get("pid"), int):
            out[rec["pid"]] = float(rec.get("requestedAt") or mtime(p) or 0)
    return out


def main():
    off_switch = load("_unimatrix/off_switch.py")
    proc = load("_unimatrix/proc.py")
    marker = off_switch.marker()
    sdir = marker.parent
    now = time.time()
    events = log_events(sdir)
    states = link_states(sdir)
    stops = stop_requests(sdir)
    disabled = marker.exists()
    disabled_txt = "present" if disabled else "absent"
    if disabled:
        disabled_txt += f"({age(now - mtime(marker))} old)"

    artifact_times = [t for t in (mtime(sdir / n) for n in PASS_ARTIFACTS) if t is not None]
    pass_age = now - max(artifact_times) if artifact_times else None

    for k, alias in sorted(box_aliases(sdir).items()):
        link = states.get(k) or {}
        link_txt = link.get("state", "unrecorded")
        if link.get("since"):
            link_txt += f"@{link['since']}"
        rec_path = next((p for p in sdir.glob("autolink-*.json")
                         if key(p.stem[len("autolink-"):].replace("_at_", "@")) == k
                         and p.stem[len("autolink-"):] not in ("boxes", "router")
                         and not p.stem.endswith(".backoff")), None)
        rec = read_json(rec_path) if rec_path else {}
        roles = {role: rec[role] for role in ("bridge", "inbox") if isinstance(rec.get(role), dict)}
        sup = ",".join(f"{r}:{'live' if proc.supervisor_live(e) else 'dead'}" for r, e in roles.items()) or "norecord"
        pending = [stops[e["pid"]] for e in roles.values() if e.get("pid") in stops]
        mine = [(t, text) for t, text in events if log_subject(text) == k]
        asked = [t for t, text in mine if "asked to stop" in text]
        if pending:
            stop_txt = f"{age(now - min(pending))}(pending)"
        elif asked:
            stop_txt = f"{age(now - asked[-1])}(logged,no-pending-request)"
        else:
            stop_txt = "none"
        last_log = f"{age(now - mine[-1][0])}:{mine[-1][1][:90]!r}" if mine else "none"
        print(f"box={alias} link={link_txt} supervisors={sup} last_pass_age={age(pass_age)} "
              f"stop_req_age={stop_txt} disabled={disabled_txt} last_log={last_log}")

    log_age = age(now - events[-1][0]) if events else "none"
    print(f"autolink settings_unimatrix={sdir} last_pass_age={age(pass_age)} last_log_line_age={log_age} "
          f"disabled={disabled_txt} marker={marker}")


if __name__ == "__main__":
    main()

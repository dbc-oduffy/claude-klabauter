#!/usr/bin/env python3
"""Unimatrix installer: Claude Code version-range gate plus copy of the vendored scripts.

Default mode checks `claude --version` against VERIFIED_RANGE; `--install` copies
VENDORED_SCRIPTS into <settings home>/unimatrix. Outside the range the gate refuses;
`--force` downgrades the refusal to a warning. Stdlib only, portable across
macOS, Windows and Linux.
"""
import argparse
import hashlib
import importlib.util
import json
import ntpath
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import threading
import tomllib
import tempfile
import types
from pathlib import Path

INSTALL_CLASS = True  # copies vendored scripts into the settings home; see door_install.declared_install_class

# Half-open: lower bound inclusive, upper bound exclusive.
VERIFIED_RANGE = ((2, 1, 300), (2, 2, 0))

VENDORED_SCRIPTS = (
    "unimatrix-bridge.py",
    "unimatrix-relay.py",
    "unimatrix-rekey.py",
    "unimatrix-inbox.py",
    "unimatrix-roster.py",
    "unimatrix-peer-contract.py",
    "unimatrix-local-transport.py",
    "unimatrix-peer-codex.py",
    "unimatrix-peer-agy.py",
    "unimatrix-peer-grok.py",
    "unimatrix-agy-leg.py",
    "unimatrix-peer-router.py",
    "unimatrix-agy-hook-pretool.py",
    "unimatrix-agy-hook-launch.py",
    "unimatrix-agy-hook-sessionstart.py",
    "unimatrix-codex-hook-sessionstart.py",
    "unimatrix-host-guard.py",
    "coordinator-external-session.py",
    "unimatrix-doctrine-seed.py",
    "unimatrix-autolink.py",
    "unimatrix-liveness.py",
    "unimatrix-mirror-all.py",
    "unimatrix-reshape.py",
    "unimatrix-membership.py",
    "unimatrix-doctor.py",
    "unimatrix-pair.py",
    "unimatrix-file.py",
    "unimatrix-file-relay.py",
    "unimatrix-passphrase.py",
    "unimatrix-control.py",
    "headless-em-launch.py",
)

AUTOLINK_SCRIPT = "unimatrix-autolink.py"
PASSPHRASE_SCRIPT = "unimatrix-passphrase.py"
STAGING_SUFFIX = ".new"
_SCRIPT_REF = re.compile(r"unimatrix-[a-z0-9-]+\.py")

AGY_HOOK_KEY = "unimatrix-agy-leg"
AGY_HOOK_SCRIPT = "unimatrix-agy-hook-pretool.py"
AGY_SESSIONSTART_SCRIPT = "unimatrix-agy-hook-sessionstart.py"
CODEX_SESSIONSTART_SCRIPT = "unimatrix-codex-hook-sessionstart.py"
GUARD_SCRIPT = "unimatrix-host-guard.py"
EXTERNAL_SESSION_SCRIPT = "coordinator-external-session.py"
DOCTRINE_SEED_SCRIPT = "unimatrix-doctrine-seed.py"
PLUGIN_GEN_SCRIPT = "unimatrix-plugin-gen.py"
AGY_PLUGIN_TREE_DIR = "agy-plugin"
CODEX_SHELL_MATCHER = ".*"
HOST_NAMES = ("claude", "codex", "agy", "grok")

_BIN_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = _BIN_DIR.parent
SKILLS_ROOT = PLUGIN_ROOT / "skills"

# Copied when present beside this installer; the peer doctrine names its installed path.
OPTIONAL_SCRIPTS = ("unimatrix-send.py",)
SEND_SCRIPT = "unimatrix-send.py"

PEER_DOCTRINE_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "unimatrix" / "peer-doctrine.md"
DOCTRINE_BEGIN = "<!-- BEGIN unimatrix-peer-doctrine (managed by unimatrix-install; edits inside are overwritten) -->"
DOCTRINE_END = "<!-- END unimatrix-peer-doctrine -->"

SEMVER = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\b")

NATIVE_RC_NOTICE = (
    "NOTE: the native same-account /rc route is the first choice for cross-box SendMessage; "
    "Unimatrix is the option for cross-account boxes."
)


def parse_version(text):
    """Return (major, minor, patch) from `claude --version` output; RuntimeError if none."""
    m = SEMVER.search(text or "")
    if not m:
        raise RuntimeError(f"no semver in `claude --version` output: {text!r}")
    return tuple(int(g) for g in m.groups())


def _fmt(v):
    return ".".join(str(p) for p in v)


def check_version(text, force=False):
    """Return ("ok"|"warn"|"refuse", message). `force` turns a refusal into a warning."""
    version = parse_version(text)
    lo, hi = VERIFIED_RANGE
    rng = f">={_fmt(lo)},<{_fmt(hi)}"
    if lo <= version < hi:
        return "ok", f"claude {_fmt(version)} in verified range ({rng})"
    msg = f"claude {_fmt(version)} outside verified range ({rng})"
    if force:
        return "warn", f"WARN: {msg}; continuing because --force"
    return "refuse", f"REFUSE: {msg}; pass --force to override"


def resolve_claude():
    """PATH first, then the default install dirs a non-login ssh PATH lacks; None if absent."""
    exe = shutil.which("claude")
    if exe:
        return exe
    home = Path.home()
    names = ("claude.exe", "claude.cmd") if sys.platform == "win32" else ("claude",)
    dirs = [home / ".local" / "bin", home / ".claude" / "local", home / ".npm-global" / "bin",
            home / ".bun" / "bin", home / ".volta" / "bin"]
    if sys.platform == "win32":
        dirs.append(Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "npm")
    else:
        dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    for d in dirs:
        for n in names:
            cand = d / n
            if cand.is_file() and _is_executable(cand):
                return str(cand)
    return None


def _is_executable(path):
    """Windows has no exec bit: any file counts; POSIX asks the mode."""
    if sys.platform == "win32":
        return True
    return os.access(path, os.X_OK)


def probe_claude_version():
    """Run `claude --version`; shutil.which resolves claude.cmd on Windows."""
    exe = resolve_claude()
    if not exe:
        raise RuntimeError("claude not found on PATH or in default install dirs; cannot read the harness version")
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30, **kwargs)
    return out.stdout or ""


def settings_home():
    env = os.environ.get("COORDINATOR_SETTINGS_HOME")
    return Path(env) if env else Path.home() / ".coordinator-claude-settings"


def script_closure(src_dir, root=AUTOLINK_SCRIPT):
    """Every unimatrix-*.py script named, transitively, from `root` in src_dir (root included)."""
    # Trap: the scan is textual, so any vendored script whose prose names this installer would drag the
    # installer and everything it names (plugin-gen) into the runtime closure; the installer is never one.
    seen, todo = set(), [root]
    while todo:
        name = todo.pop()
        if name in seen or name == Path(__file__).name:
            continue
        seen.add(name)
        path = Path(src_dir) / name
        if path.is_file():
            todo.extend(_SCRIPT_REF.findall(path.read_text(encoding="utf-8")))
    return seen


ROUTER_SCRIPT = "unimatrix-peer-router.py"
PIPE_MARKER = "PipeServer"
ROUTER_RESTART_REFUSED_RC = 8


def router_closure_digest(src_dir):
    """sha256 over the sorted (name, bytes) of the router's script closure in src_dir; None without the router script."""
    root = Path(src_dir)
    if not (root / ROUTER_SCRIPT).is_file():
        return None
    h = hashlib.sha256()
    for name in sorted(script_closure(root, ROUTER_SCRIPT)):
        try:
            data = (root / name).read_bytes()
        except OSError:
            continue
        h.update(name.encode("utf-8") + b"\0" + data + b"\0")
    return h.hexdigest()


def _installed_autolink(dest):
    """The INSTALLED unimatrix-autolink module, so a restart spawns the installed router; None when absent."""
    path = Path(dest) / AUTOLINK_SCRIPT
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("unimatrix_autolink_installed", path)
    mod = importlib.util.module_from_spec(spec)
    keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = keep
    return mod


def router_is_live(dest):
    """True while a router holds the lease under `dest` (the installed autolink's own check)."""
    try:
        mod = _installed_autolink(dest)
        check = getattr(mod, "router_lock_held", None)
        return bool(check and check(Path(dest)))
    except Exception:
        return False


def restart_installed_router(dest, say):
    """Hand the box over to the installed router through the installed autolink's `restart_router`."""
    mod = _installed_autolink(dest)
    restart = getattr(mod, "restart_router", None)
    if restart is None:
        raise RuntimeError(f"installed {AUTOLINK_SCRIPT} has no restart_router")
    keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        return restart(mod._roster(), Path(dest), say)
    finally:
        sys.dont_write_bytecode = keep


class PipeCapabilityRefused(Exception):
    """A win32 install that would ship or keep a router without the Win32 named-pipe transport."""


def _has_pipe_marker(path):
    try:
        return PIPE_MARKER in Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def check_win32_pipe_capability(src_dir, dest_dir):
    """On win32, refuse a source router lacking the pipe transport (which also covers downgrading an
    installed pipe-capable router). No-op elsewhere."""
    if sys.platform != "win32":
        return
    if _has_pipe_marker(Path(src_dir) / ROUTER_SCRIPT):
        return
    installed = _has_pipe_marker(Path(dest_dir) / ROUTER_SCRIPT)
    what = "would downgrade the installed pipe-capable router" if installed else "carries no pipe transport"
    raise PipeCapabilityRefused(
        f"source {ROUTER_SCRIPT} {what}: missing Win32 named-pipe capability ({PIPE_MARKER}); "
        f"install from the klabauter published mirror tree")


def install_scripts(src_dir, dest_dir):
    """Copy VENDORED_SCRIPTS plus the autolink closure from src_dir into dest_dir; return the copied names.
    Raises PipeCapabilityRefused on win32 before any copy when the source router lacks the pipe transport."""
    wanted = list(VENDORED_SCRIPTS) + sorted(script_closure(src_dir) - set(VENDORED_SCRIPTS))
    missing = [n for n in wanted if not (Path(src_dir) / n).is_file()]
    if missing:
        raise FileNotFoundError(f"vendored scripts missing in {src_dir}: {', '.join(missing)}")
    check_win32_pipe_capability(src_dir, dest_dir)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    for name in wanted:
        _copy_if_changed(Path(src_dir) / name, dest / name)
    for name in VENDORED_LIB:
        if (Path(src_dir) / "lib" / name).is_file():
            (dest / "lib").mkdir(exist_ok=True)
            _copy_if_changed(Path(src_dir) / "lib" / name, dest / "lib" / name)
    extra = [n for n in OPTIONAL_SCRIPTS if n not in wanted and (Path(src_dir) / n).is_file()]
    for name in extra:
        _copy_if_changed(Path(src_dir) / name, dest / name)
    return wanted + extra


def _copy_if_changed(src, dst):
    """copy2 unless dst already holds the same bytes: an unchanged script keeps its st_ctime, which the
    relay reap reads as the moment the code last changed."""
    try:
        if dst.read_bytes() == src.read_bytes():
            return
    except OSError:
        pass
    shutil.copy2(src, dst)


VENDORED_LIB = ("python_interp.py", "unimatrix_box_mux.py", "unimatrix_relay_sessions.py", "session_detach.py")  # sibling lib/ modules the installed scripts load by path


def _lib(name):
    """A sibling lib/ module loaded once by path (the installed layout has no package)."""
    if name not in _LIBS:
        spec = importlib.util.spec_from_file_location(f"unimatrix_lib_{name}", _BIN_DIR / "lib" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LIBS[name] = mod
    return _LIBS[name]


_LIBS = {}
HOMEBREW_PYTHONS = ("/opt/homebrew/bin/python3", "/usr/local/bin/python3")  # literal: the installer must import without lib/ beside it
_PIN_UNREAD = object()
_PIN_CACHE = {}


def _pin_value():
    """The machine-local `unimatrix.python` value autolink reads, when it names a file; else None. One read per settings home."""
    roster = _sibling("unimatrix-roster.py")
    home = str(roster.settings_home())
    if home not in _PIN_CACHE:
        try:
            val = _sibling(AUTOLINK_SCRIPT).pin_key_value(roster)
        except (OSError, subprocess.SubprocessError):
            val = None
        _PIN_CACHE[home] = val if val and os.path.isfile(val) else None
    return _PIN_CACHE[home]


def forced_command_interpreter(pin_value=_PIN_UNREAD):
    """Absolute interpreter for the forced command: the one autolink's mirror-all runs (`unimatrix.python`
    pin, else a Homebrew python3 on macOS where sshd's PATH python3 is the CLT stub, else the console python)."""
    if pin_value is _PIN_UNREAD:
        pin_value = _pin_value()
    return _lib("python_interp").unimatrix_python(pin_value, homebrew=HOMEBREW_PYTHONS) or sys.executable


def authorized_keys_line(dest_dir, interpreter=None, key_body="<key-body>", file_peer=None, control=False):
    """Paste-ready `restrict,command=...` authorized_keys line; `key_body` defaults to the <key-body> placeholder.
    A path with a space is quoted inside the command and each inner quote escaped for sshd.
    `file_peer` (the calling box) makes it the file key's line: the separate unimatrix-file-relay.py
    forced command with a fixed `--peer <box>`. `control` makes it the control key's line: the separate
    unimatrix-control.py forced command (launch/wake only), comment `unimatrix-control`."""
    if control and file_peer is not None:
        raise ValueError("a key is the control key or the file key, not both")
    interp = interpreter or forced_command_interpreter()
    script = "unimatrix-control.py" if control else (
        "unimatrix-file-relay.py" if file_peer is not None else "unimatrix-relay.py")
    parts = [f'\\"{p}\\"' if " " in p else p for p in (interp, str(Path(dest_dir) / script))]
    if file_peer is not None:
        parts += ["--peer", file_peer]
    return f'restrict,command="{" ".join(parts)}" ssh-ed25519 {key_body} {"unimatrix-control" if control else "unimatrix"}'


FAR_DEST_DIR = "~/.coordinator-claude-settings/unimatrix"
FAR_INTERPRETER = "python3"
_ALIAS_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_SIBLINGS = {}


def _sibling(script):
    """A sibling unimatrix script loaded once from beside this file."""
    if script not in _SIBLINGS:
        spec = importlib.util.spec_from_file_location("unimatrix_" + script[9:-3].replace("-", "_"), _BIN_DIR / script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SIBLINGS[script] = mod
    return _SIBLINGS[script]


def _pub_body(pub_path):
    """The base64 body of an ssh public key file; ValueError when it is not an ssh-ed25519 line."""
    fields = Path(pub_path).read_text(encoding="utf-8").split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise ValueError(f"{pub_path} is not an ssh-ed25519 public key")
    return fields[1]


def _item_alias(priv):
    """The 1Password item alias of a key file: its name less `unimatrix-` (the rule ensure_agent applies)."""
    name = Path(priv).name
    return name[len("unimatrix-"):] if name.startswith("unimatrix-") else name


def _mint(priv, comment, run, unencrypted, remint, on_mint=None):
    """Mint the key at `priv`: passphrase-encrypted with the 1Password item's passphrase unless `unencrypted`
    (then the plain `-N ""` ssh-keygen mint). A `remint` writes the new key (sibling path) and its item (staging
    title) first, replaces the superseded files, then deletes the superseded item and retitles the new one; a failure
    before the replace leaves the superseded key and item untouched. PassphraseSourceMissing / OnePasswordLocked
    propagate."""
    roster = _sibling("unimatrix-roster.py")
    alias = _item_alias(priv)
    mod = _sibling(PASSPHRASE_SCRIPT)
    stage = alias + STAGING_SUFFIX if remint and not unencrypted else alias
    target = Path(str(priv) + ".new") if remint else priv
    side = [target, Path(str(target) + ".pub")]
    if remint:
        for stale in side:
            stale.unlink(missing_ok=True)
    priv.parent.mkdir(parents=True, exist_ok=True)
    passphrase = None
    try:
        if not unencrypted:
            passphrase = mod.read_passphrase(mod.ensure_item(stage, run), run)
            try:
                mod.mint_key(target, comment, passphrase)
            except ImportError as exc:
                raise RuntimeError(f"minting an encrypted key needs the `cryptography` package ({exc}); run "
                                   f"`{sys.executable} -m pip install cryptography`, or pass --unencrypted-key") from exc
        else:
            kg = run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(target)],
                     capture_output=True, text=True, timeout=30, creationflags=roster.no_window())
            if kg.returncode or not target.is_file():
                raise RuntimeError(f"ssh-keygen failed: {(kg.stderr or kg.stdout or '').strip()[:200]}")
    except BaseException:
        if remint:
            for stale in side:
                stale.unlink(missing_ok=True)
            if not unencrypted:
                try:
                    mod.delete_item(stage, run)
                except RuntimeError:
                    pass
        raise
    if remint:
        for part in reversed(side):
            os.replace(part, priv if part == side[0] else Path(str(priv) + ".pub"))
        try:
            mod.delete_item(alias, run)
        except mod.PassphraseSourceMissing:
            if not unencrypted:
                raise
        except RuntimeError as exc:
            if not unencrypted:
                raise RuntimeError(f"the new key is in place but its passphrase item {mod.item_name(stage)} is not "
                                   f"yet titled {mod.item_name(alias)} (the superseded item could not be deleted: "
                                   f"{exc}); re-run --remint") from exc
        if not unencrypted:
            _retitle_item(mod, stage, alias, run)
    if not unencrypted and on_mint:
        on_mint(mod.op_ref(alias, run))


def _retitle_item(mod, stage, alias, run):
    """Rename the staging passphrase item to the alias's title; the new key is already in place, so a failure
    names the item that holds its passphrase."""
    r = mod._op(["item", "edit", mod.item_name(stage), f"--vault={mod._vault(run)}",
                 f"--title={mod.item_name(alias)}"], run)
    if r.returncode:
        raise RuntimeError(f"the new key is in place but its passphrase item {mod.item_name(stage)} could not be "
                           f"renamed to {mod.item_name(alias)}: {mod._tail(r)}")


def pair_key(alias, run=subprocess.run, unencrypted=False, remint=False, on_mint=None):
    """Return (private key path, public key body) for this box's pair with `alias`. Reads the key autolink's
    resolve_key finds when it is alias-specific and present; otherwise mints (or adopts an unrecorded file at)
    <settings home>/.secrets/ssh/unimatrix-<alias> and records unimatrix.keys.<alias>. Never overwrites a key
    except under `remint`, which deletes the old pair and its 1Password item first. The mint is passphrase-encrypted
    with the item's passphrase (`on_mint(ref)` receives the op reference) unless `unencrypted`.
    RuntimeError when the alias reduces to nothing, the mint fails, or the machine-local record cannot be made;
    PassphraseSourceMissing / OnePasswordLocked (RuntimeError) when the passphrase source cannot be reached."""
    autolink, roster = _sibling(AUTOLINK_SCRIPT), _sibling("unimatrix-roster.py")
    found = autolink.resolve_key(
        roster, alias,
        local_fn=lambda r, a: autolink.machine_local_key(r, a, run=run),
        ssh_fn=lambda r, a: autolink.ssh_identity_file(r, a, run=run))
    alias_key = found != roster.default_key() and Path(found).is_file()
    if alias_key and not remint:
        priv = Path(found)
    else:
        safe = _ALIAS_UNSAFE.sub("-", alias)
        if not safe.strip("-"):
            raise RuntimeError(f"alias {alias!r} has no usable characters for a key file name")
        priv = Path(found) if alias_key else roster.settings_home() / ".secrets" / "ssh" / f"unimatrix-{safe}"
        reader = roster.settings_home() / "bin" / "_machine_local.py"
        if not reader.is_file():
            raise RuntimeError(f"{reader} is missing; an unrecorded pair key would not be found next run")
        if remint or not priv.is_file():
            _mint(priv, "unimatrix", run, unencrypted, remint, on_mint)
        rec = run([sys.executable, str(reader), "set", f"{autolink.KEYS_PREFIX}{alias}", str(priv)],
                  capture_output=True, text=True, timeout=30, creationflags=roster.no_window())
        if rec.returncode:
            raise RuntimeError(f"recording {autolink.KEYS_PREFIX}{alias} failed: "
                               f"{(rec.stderr or rec.stdout or '').strip()[:200]}")
    pub = Path(str(priv) + ".pub")
    if not pub.is_file():
        raise RuntimeError(f"{pub} is missing; cannot read the public key of {priv}")
    return priv, _pub_body(pub)


FILE_KEY_NAME = "unimatrix-file"


def file_key(run=subprocess.run, unencrypted=False, remint=False, on_mint=None):
    """(private key path, public key line, fingerprint) of this box's file-transfer key, minted at
    <settings home>/.secrets/ssh/unimatrix-file when absent; never overwrites (bar `remint`, which deletes the old
    pair and its 1Password item first) and never touches the relay key. The mint is passphrase-encrypted unless
    `unencrypted` (`on_mint(ref)` receives the op reference).
    RuntimeError when the mint fails or the minted key does not read back as ssh-ed25519;
    PassphraseSourceMissing / OnePasswordLocked (RuntimeError) when the passphrase source cannot be reached."""
    roster = _sibling("unimatrix-roster.py")
    priv = roster.settings_home() / ".secrets" / "ssh" / FILE_KEY_NAME
    pub = Path(str(priv) + ".pub")
    if remint or not priv.is_file():
        _mint(priv, "unimatrix-file", run, unencrypted, remint, on_mint)
    if not pub.is_file():
        raise RuntimeError(f"{pub} is missing; cannot read the public key of {priv}")
    body = _pub_body(pub)
    fp = run(["ssh-keygen", "-lf", str(pub)], capture_output=True, text=True, timeout=30,
             creationflags=roster.no_window())
    if fp.returncode or not (fp.stdout or "").strip():
        raise RuntimeError(f"ssh-keygen -lf failed: {(fp.stderr or fp.stdout or '').strip()[:200]}")
    return priv, f"ssh-ed25519 {body}", fp.stdout.split()[1]


CONTROL_ALIAS_PREFIX = "unimatrix-control-"


def control_key(box, run=subprocess.run, unencrypted=False, remint=False, on_mint=None):
    """(private key path, public key body) of this box's control key for far box `box`, minted at
    ~/.ssh/unimatrix_control_<box> when absent; never overwrites (bar `remint`) and never touches the relay or file key.
    RuntimeError when `box` has no usable characters, the mint fails, or the public key is unreadable;
    PassphraseSourceMissing / OnePasswordLocked (RuntimeError) when the passphrase source cannot be reached."""
    safe = _ALIAS_UNSAFE.sub("-", box)
    if not safe.strip("-"):
        raise RuntimeError(f"box {box!r} has no usable characters for a key file name")
    priv = Path.home() / ".ssh" / f"unimatrix_control_{safe}"
    if remint or not priv.is_file():
        _mint(priv, "unimatrix-control", run, unencrypted, remint, on_mint)
    pub = Path(str(priv) + ".pub")
    if not pub.is_file():
        raise RuntimeError(f"{pub} is missing; cannot read the public key of {priv}")
    return priv, _pub_body(pub)


def render_ssh_config_block(alias, key_path, host_alias=None):
    """The near-side `~/.ssh/config` block for the pair; printed, never written. The peer is named by
    hostname (`<name>.local` unless the name is already dotted), never an IP literal. `host_alias` names the
    `Host` entry when it differs from the peer (the control key's `unimatrix-control-<box>`)."""
    name = alias.rsplit("@", 1)[-1]
    host = name if "." in name else f"{name}.local"
    return (f"Host {host_alias or alias}\n"
            f"    HostName {host}\n"
            f"    AddressFamily inet\n"
            f"    IdentityFile {Path(key_path).as_posix()}\n"
            f"    IdentitiesOnly yes\n")


_PROBE_PROGRAM = (
    "import os, sys\n"
    "h = os.environ.get('COORDINATOR_SETTINGS_HOME') or "
    "os.path.join(os.path.expanduser('~'), '.coordinator-claude-settings')\n"
    "print(sys.platform, sys.executable, h, sep='\\t')\n")

_APPEND_PROGRAM = '''import os, sys
body, line = {body!r}, {line!r}
if sys.platform == "win32":
    import ctypes
    if ctypes.windll.shell32.IsUserAnAdmin():
        print("admin")
        raise SystemExit(0)
ssh = os.path.join(os.path.expanduser("~"), ".ssh")
path = os.path.join(ssh, "authorized_keys")
if not os.path.isdir(ssh):
    os.makedirs(ssh)
    if os.name == "posix":
        os.chmod(ssh, 0o700)
text = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
if any(body in l.split() for l in text.splitlines()):
    print("present")
else:
    with open(path, "a", encoding="utf-8") as f:
        f.write(("" if not text or text.endswith("\\n") else "\\n") + line + "\\n")
    if os.name == "posix":
        os.chmod(path, 0o600)
    print("added")
'''


def _ssh_batch(alias, interp, program, run):
    """Run `interp -` on the far box with `program` on stdin, over the user's own credentials only."""
    return run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", alias, interp, "-"],
               input=program, capture_output=True, text=True, timeout=30,
               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def probe_far(alias, run=subprocess.run):
    """(interpreter name that answered, platform, absolute interpreter, far settings home), or None when no
    authorized link answers with that triple (a relay key's forced command answers with something else)."""
    for py in ("python3", "python"):
        try:
            r = _ssh_batch(alias, py, _PROBE_PROGRAM, run)
        except (OSError, subprocess.SubprocessError):
            continue
        lines = (r.stdout or "").strip().splitlines()
        fields = lines[-1].split("\t") if r.returncode == 0 and lines else []
        if len(fields) == 3 and all(f.strip() for f in fields) and " " not in fields[0]:
            return py, fields[0], fields[1], fields[2]
    return None


def install_over_link(alias, body, run=subprocess.run, control=False):
    """Append the pair's authorized_keys line on the far box over an already-authorized BatchMode link.
    Returns (status, line): status is added | present | admin | no-link | failed; `line` is the line with the far
    box's own interpreter and relay directory (None for no-link); `control` makes it the control key's line.
    Never prompts, never sudo, never the relay key."""
    far = probe_far(alias, run)
    if far is None:
        return "no-link", None
    py, plat, interp, home = far
    join = ntpath.join if plat == "win32" else posixpath.join
    line = authorized_keys_line(join(home, "unimatrix"), interp, body, control=control)
    try:
        r = _ssh_batch(alias, py, _APPEND_PROGRAM.format(body=body, line=line), run)
    except (OSError, subprocess.SubprocessError):
        return "failed", line
    out = (r.stdout or "").strip().splitlines()
    status = out[-1] if r.returncode == 0 and out else ""
    return (status if status in ("added", "present", "admin") else "failed"), line


_RELAY_FORCED_SCRIPT = "unimatrix-relay.py"
_FORCED_COMMAND = re.compile(r'command="((?:\\"|[^"])*)"')
_FORCED_INTERP = re.compile(r'^(\s*)(\\"(.*?)\\"|\S+)')


def _same_interpreter(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def rewrite_forced_interpreters(path, interpreter):
    """Rewrite, in the local authorized_keys at `path`, the absolute interpreter of every uncommented line forcing
    unimatrix-relay.py that names a different interpreter than `interpreter` (compared by realpath + normcase).
    Key body, options, other lines, line endings and file mode are untouched; a bare interpreter is never judged.
    Returns the number of lines rewritten (0 when the file is absent or unreadable)."""
    path = Path(path).resolve()
    try:
        text = path.read_text(encoding="utf-8", newline="")
    except OSError:
        return 0
    token = f'\\"{interpreter}\\"' if " " in interpreter else interpreter
    changed = 0
    lines = []
    for line in text.splitlines(keepends=True):
        m = _FORCED_COMMAND.search(line)
        if m and not line.lstrip().startswith("#") and _RELAY_FORCED_SCRIPT in line:
            im = _FORCED_INTERP.match(m.group(1))
            old = im and (im.group(3) if im.group(3) is not None else im.group(2))
            if old and (os.path.isabs(old) or old.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", old)) \
                    and not _same_interpreter(old, interpreter):
                inner = m.group(1)[:im.start(2)] + token + m.group(1)[im.end(2):]
                line = line[:m.start(1)] + inner + line[m.end(1):]
                changed += 1
        lines.append(line)
    if changed:
        tmp = path.with_name(path.name + ".unimatrix-tmp")
        tmp.write_text("".join(lines), encoding="utf-8", newline="")
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    return changed


def agy_hooks_path():
    """agy's global hook discovery file."""
    return Path.home() / ".gemini" / "config" / "hooks.json"


def agy_plugins_path():
    """agy's global plugin registration file (sibling of hooks.json)."""
    return agy_hooks_path().with_name("plugins.json")


def _plugin_entry_matches(entry, tree):
    if not isinstance(entry, dict) or entry.get("include_only") != [tree.name]:
        return False
    return os.path.normcase(os.path.normpath(str(entry.get("path", "")))) == os.path.normcase(os.path.normpath(str(tree.parent)))


def seed_agy_plugin(path, tree):
    """Register the generated agy plugin tree in agy's plugins.json; return "created", "added" or "present".

    agy scans each `entries` path one level deep, so the entry names the tree's parent and
    `include_only` narrows it to the tree. Other entries and keys are preserved; a re-run adds nothing.
    """
    path, tree = Path(path), Path(tree)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise ValueError(f"{path} is not valid JSON ({exc}); fix or remove it") from exc
        if not isinstance(data, dict) or not isinstance(data.get("entries", []), list):
            raise ValueError(f"{path} has an unexpected shape (want an object with an `entries` list)")
    entries = data.setdefault("entries", [])
    if any(_plugin_entry_matches(e, tree) for e in entries):
        return "present"
    entries.append({"path": str(tree.parent), "include_only": [tree.name]})
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return "added" if existed else "created"


def unseed_agy_plugin(path, tree):
    """Remove only our registration from plugins.json; return "removed" or "absent"."""
    path, tree = Path(path), Path(tree)
    if not path.exists():
        return "absent"
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); fix or remove it") from exc
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return "absent"
    kept = [e for e in entries if not _plugin_entry_matches(e, tree)]
    if len(kept) == len(entries):
        return "absent"
    data["entries"] = kept
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return "removed"


def codex_hooks_path():
    """Codex's user hook discovery file: $CODEX_HOME/hooks.json, default ~/.codex."""
    env = os.environ.get("CODEX_HOME")
    return (Path(env) if env else Path.home() / ".codex") / "hooks.json"


def _hook_command(dest_dir, script_name, *args):
    argv = [sys.executable, str(Path(dest_dir) / script_name), *args]
    return subprocess.list2cmdline(argv) if os.name == "nt" else " ".join(shlex.quote(a) for a in argv)


def _engine_args(engine_root):
    return ("--engine-root", str(engine_root)) if engine_root else ()


def _hook_handler(dest_dir, script_name, *args):
    return {"type": "command", "command": _hook_command(dest_dir, script_name, *args), "timeout": 10}


def _hook_group(dest_dir, script_name, matcher=None, args=()):
    group = {"hooks": [_hook_handler(dest_dir, script_name, *args)]}
    if matcher:
        group = {"matcher": matcher, **group}
    return group


def agy_hook_entry(dest_dir, engine_root=None):
    """Our hooks.json value: a run_command PreToolUse hook and a SessionStart hook running installed scripts.

    agy groups only tool events under matcher/hooks; SessionStart takes bare handlers, and agy
    rejects the whole file ("command hook must specify 'command'") when it is grouped. The
    PreToolUse group is the single merged hook; `--engine-root` is how it finds the Klabauter guard.
    """
    return {
        "PreToolUse": [_hook_group(dest_dir, AGY_HOOK_SCRIPT, "run_command", _engine_args(engine_root))],
        "SessionStart": [_hook_handler(dest_dir, AGY_SESSIONSTART_SCRIPT)],
    }


def external_session_capability(engine_root):
    """Check the published lifecycle surface without importing ambient engine code."""
    if not engine_root:
        return False, "published engine root unresolved"
    root = Path(engine_root)
    try:
        if not (root / "coordinator_core" / "_engine_stamp").read_bytes():
            return False, "published engine stamp is empty"
        module = root / "coordinator_core" / "session" / "external_registry.py"
        if not module.is_file():
            return False, "published engine lacks session.external_registry; update the engine"
    except OSError as exc:
        return False, f"published engine unavailable: {exc}"
    if not (root / "coordinator_core" / "host_hooks" / "session_context.py").is_file():
        return False, "published engine lacks Codex session context; update the engine"
    if not (root / "coordinator_core" / "host_hooks" / "codex_lifecycle.py").is_file():
        return False, "published engine lacks Codex advisory lifecycle; update the engine"
    if not (root / "coordinator_core" / "codex_status" / "hook.py").is_file():
        return False, "published engine lacks Codex usage lifecycle; update the engine"
    return True, "published registration, context, advisory and usage lifecycle present"


def codex_hook_events(dest_dir, engine_root=None):
    """Transport discovery plus engine lifecycle registration and shell guard."""
    start = _hook_group(dest_dir, CODEX_SESSIONSTART_SCRIPT)
    start["hooks"].append({**_hook_handler(
        dest_dir, EXTERNAL_SESSION_SCRIPT, "--event", "SessionStart", *_engine_args(engine_root)), "timeout": 3})
    end = _hook_group(dest_dir, EXTERNAL_SESSION_SCRIPT,
                      args=("--event", "SessionEnd", *_engine_args(engine_root)))
    end["hooks"][0]["timeout"] = 3
    events = {
        "SessionStart": start,
        "SessionEnd": end,
        "SubagentStart": {"hooks": [{**_hook_handler(
            dest_dir, EXTERNAL_SESSION_SCRIPT, "--event", "SubagentStart", *_engine_args(engine_root)), "timeout": 3}]},
        "PreToolUse": _hook_group(
            dest_dir, GUARD_SCRIPT, CODEX_SHELL_MATCHER, ("--host", "codex", *_engine_args(engine_root))),
    }
    for event in ("UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop", "PreCompact", "PostCompact"):
        events[event] = {"hooks":[{**_hook_handler(dest_dir, EXTERNAL_SESSION_SCRIPT,
            "--event", event, *_engine_args(engine_root)), "timeout":3}]}
    return events



def _load_hooks(path):
    """Return the parsed hooks.json object ({} when absent); ValueError when unreadable or not an object."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is not parseable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def seed_agy_hook(hooks_path, dest_dir, engine_root=None):
    """Point our key at this install; return "created", "added", "updated" or "present".

    A value we wrote earlier (it carries PreToolUse or SessionStart) is replaced when it differs,
    which heals a missing event, a script path or the engine root from an older install. A
    hand-customised value and every other key in the file are left byte-identical.
    """
    path = Path(hooks_path)
    existed = path.exists()
    data = _load_hooks(path)
    want = agy_hook_entry(dest_dir, engine_root)
    if AGY_HOOK_KEY in data:
        entry = data[AGY_HOOK_KEY]
        ours = isinstance(entry, dict) and ("PreToolUse" in entry or "SessionStart" in entry)
        if entry == want or not ours:
            return "present"
        data[AGY_HOOK_KEY] = want
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return "updated"
    data[AGY_HOOK_KEY] = want
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return "added" if existed else "created"


def unseed_agy_hook(hooks_path):
    """Remove only our key; return "removed" or "absent"."""
    path = Path(hooks_path)
    data = _load_hooks(path)
    if AGY_HOOK_KEY not in data:
        return "absent"
    del data[AGY_HOOK_KEY]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return "removed"


_CODEX_OWNED_SCRIPTS = (CODEX_SESSIONSTART_SCRIPT, GUARD_SCRIPT, EXTERNAL_SESSION_SCRIPT)


def _codex_ours(group):
    """True for a group holding a handler that runs the SessionStart script or the guard."""
    return isinstance(group, dict) and any(
        isinstance(h, dict) and any(s in str(h.get("command", "")) for s in _CODEX_OWNED_SCRIPTS)
        for h in group.get("hooks") or []
    )


def _codex_events(data, path, event="SessionStart"):
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path} has a non-object `hooks` value")
    events = hooks.setdefault(event, [])
    if not isinstance(events, list):
        raise ValueError(f"{path} has a non-list `hooks.{event}` value")
    return events


def _codex_owned_handler(handler):
    return isinstance(handler, dict) and any(
        name in str(handler.get("command", "")) for name in _CODEX_OWNED_SCRIPTS)


def _without_codex_handlers(group):
    if not _codex_ours(group):
        return group
    foreign = [handler for handler in group.get("hooks") or [] if not _codex_owned_handler(handler)]
    return {**group, "hooks": foreign} if foreign else None


def seed_codex_hook(hooks_path, dest_dir, engine_root=None):
    """Install owned handlers idempotently, retaining every foreign handler/group."""
    path = Path(hooks_path)
    existed = path.exists()
    data = _load_hooks(path)
    changed = False
    added = False
    for event, want in codex_hook_events(dest_dir, engine_root).items():
        events = _codex_events(data, path, event)
        found = False
        had_owned = any(_codex_ours(group) for group in events)
        updated = []
        for group in events:
            if group == want and not found:
                updated.append(group)
                found = True
            else:
                cleaned = _without_codex_handlers(group)
                if cleaned is not None:
                    updated.append(cleaned)
        if not found:
            updated.append(want)
            added = added or not had_owned
        if events != updated:
            events[:] = updated
            changed = True
    if not changed:
        return "present"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return "created" if not existed else "added" if added else "updated"


def unseed_codex_hook(hooks_path):
    """Remove owned handlers individually; mixed foreign groups retain their data."""
    path = Path(hooks_path)
    data = _load_hooks(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return "absent"
    removed = False
    for event in tuple(hooks):
        events = hooks[event]
        if not isinstance(events, list) or not any(_codex_ours(group) for group in events):
            continue
        removed = True
        events[:] = [cleaned for group in events if (cleaned := _without_codex_handlers(group)) is not None]
        if not events:
            del hooks[event]
    if not removed:
        return "absent"
    if not hooks:
        del data["hooks"]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return "removed"


def _event_id(name):
    """Event name normalised so `preToolUse`, `PreToolUse` and `pre_tool_use` compare equal."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _codex_expected_handlers(hooks_path):
    """Exact (event, command) identities; multiple owned handlers may share an event."""
    hooks = _load_hooks(hooks_path).get("hooks")
    expected = {}
    required = {
        ("SessionStart", CODEX_SESSIONSTART_SCRIPT): 1,
        ("SessionStart", EXTERNAL_SESSION_SCRIPT): 1,
        ("SessionEnd", EXTERNAL_SESSION_SCRIPT): 1,
        ("SubagentStart", EXTERNAL_SESSION_SCRIPT): 1,
        ("PreToolUse", GUARD_SCRIPT): 1,
    }
    required.update({(event, EXTERNAL_SESSION_SCRIPT):1 for event in ("UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop", "PreCompact", "PostCompact")})
    found = dict.fromkeys(required, 0)
    for event, groups in (hooks.items() if isinstance(hooks, dict) else ()):
        for group in groups if isinstance(groups, list) else ():
            for handler in (group.get("hooks") or []) if isinstance(group, dict) else ():
                if not _codex_owned_handler(handler):
                    continue
                command = str(handler.get("command", ""))
                script = next(name for name in _CODEX_OWNED_SCRIPTS if name in command)
                identity = (event, script)
                if identity not in required:
                    raise ValueError(f"{script} is on the wrong event {event}")
                found[identity] += 1
                expected[(_event_id(event), command)] = command
    if found != required:
        raise ValueError("hooks.json must hold exactly one owned transport, guard, SessionStart registration and SessionEnd unregister handler")
    return expected


_STATUS_DEFAULT = ["thread-name", "model-with-reasoning", "context-remaining"]
_STATUS_PATHS = {"current-dir", "project-root", "project", "project-name"}


def _status_profile(current):
    if current is None or current == []:
        return list(_STATUS_DEFAULT)
    if not isinstance(current, list) or not all(isinstance(item, str) for item in current):
        raise ValueError("tui.status_line must be a string array")
    result = []
    for item in current:
        item = "thread-name" if item in _STATUS_PATHS else item
        if item != "thread-name" or item not in result:
            result.append(item)
    if "thread-name" not in result:
        result.insert(0, "thread-name")
    if "model-with-reasoning" not in result and not ("reasoning" in result and any(item in result for item in ("model", "model-name"))):
        result.append("model-with-reasoning")
    if "context-remaining" not in result:
        result.append("context-remaining")
    return result


def _toml_chunks(text):
    """Complete TOML statements, so fake headers inside multiline strings are data."""
    pending = ""
    offset = 0
    for line in text.splitlines(keepends=True):
        pending += line
        try:
            parsed = tomllib.loads(pending)
        except tomllib.TOMLDecodeError:
            continue
        yield offset, offset + len(pending), pending, parsed
        offset += len(pending)
        pending = ""
    if pending:
        raise ValueError("unsupported or incomplete TOML statement")


def _toml_inline(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(key) + " = " + _toml_inline(item) for key,item in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_inline(item) for item in value) + "]"
    if isinstance(value, (str, bool, int, float)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    raise ValueError("unsupported inline TUI value; configuration unchanged")


def _status_edit(text, value):
    scope = ()
    tui_header = None
    for start,end,chunk,parsed in _toml_chunks(text):
        stripped = chunk.lstrip()
        if stripped.startswith("["):
            scope = ()
            branch = parsed
            while isinstance(branch, dict) and len(branch) == 1:
                key,branch = next(iter(branch.items()))
                scope += (key,)
            if scope == ("tui",):
                tui_header = end
            continue
        if scope == ("tui",) and "status_line" in parsed:
            replacement = "" if value is None else "status_line = " + _toml_inline(value) + "\n"
            return text[:start] + replacement + text[end:]
        if not scope and isinstance(parsed.get("tui"), dict):
            # Root dotted keys have only the managed field; root inline tables
            # are rewritten as one inline statement with all other values intact.
            fields = dict(parsed["tui"])
            inline = chunk.split("=", 1)[0].strip() in ("tui", '"tui"', "'tui'")
            if "status_line" in fields or inline:
                if value is None:
                    fields.pop("status_line", None)
                else:
                    fields["status_line"] = value
                prefix = "tui = " if inline else "tui.status_line = "
                replacement = (prefix + _toml_inline(fields if prefix == "tui = " else value) + "\n") if fields else ""
                return text[:start] + replacement + text[end:]
    if value is None:
        return text
    assignment = "status_line = " + _toml_inline(value) + "\n"
    return text[:tui_header] + assignment + text[tui_header:] if tui_header is not None else "tui." + assignment + text


def _footer_write(path, text):
    """Atomic replacement keeps existing config permissions and unrelated bytes."""
    descriptor, temporary = tempfile.mkstemp(prefix=".coordinator-footer-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _footer_state(path):
    state = json.loads(path.read_text(encoding="utf-8"))
    valid_array = lambda value: isinstance(value, list) and all(isinstance(item, str) for item in value)
    if (not isinstance(state, dict) or state.get("version") != 1 or
            type(state.get("present")) is not bool or not valid_array(state.get("managed")) or
            (state["present"] and not valid_array(state.get("original")))):
        raise ValueError("invalid footer ownership record")
    return state


def seed_codex_status_line(config_path, *, uninstall=False):
    """Own only the footer value; restore only while it remains our exact value."""
    path = Path(config_path)
    state_path = path.with_name(".coordinator-status-line.json")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    parsed = tomllib.loads(text)
    tui = parsed.get("tui", {})
    if not isinstance(tui, dict):
        raise ValueError("tui must be a table")
    present = "status_line" in tui
    current = tui.get("status_line")
    if uninstall:
        if not state_path.exists():
            return "absent"
        state = _footer_state(state_path)
        if current != state["managed"]:
            return "retained-customized"
        replacement = _status_edit(text, state["original"] if state["present"] else None)
        tomllib.loads(replacement)
        _footer_write(path, replacement)
        state_path.unlink()
        return "restored"
    wanted = _status_profile(current)
    if wanted == current:
        return "present"
    replacement = _status_edit(text, wanted)
    check = tomllib.loads(replacement)
    if check.get("tui", {}).get("status_line") != wanted:
        raise ValueError("footer edit did not address global tui.status_line")
    # Preserve the first pre-install value across subsequent managed updates.
    state = _footer_state(state_path) if state_path.exists() else None
    if not isinstance(state, dict) or state.get("version") != 1 or current != state.get("managed"):
        state = {"version":1, "present":present, "original":current}
    state["managed"] = wanted
    path.parent.mkdir(parents=True, exist_ok=True)
    _footer_write(state_path, json.dumps(state, ensure_ascii=False) + "\n")
    _footer_write(path, replacement)
    return "updated"


def agy_settings_path():
    """agy's user settings file, where the `statusLine` command lives."""
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def grok_config_path():
    """Grok's user config: $GROK_HOME/config.toml, default ~/.grok."""
    env = os.environ.get("GROK_HOME")
    return (Path(env) if env else Path.home() / ".grok") / "config.toml"


def grok_rules_path():
    """Grok's managed peer-doctrine file: $GROK_HOME/rules/unimatrix-peer-doctrine.md (grok loads every *.md there)."""
    return grok_config_path().parent / "rules" / "unimatrix-peer-doctrine.md"


def _tui_status_command(host):
    argv = [sys.executable, "-m", "coordinator_core.tui_status", host]
    return subprocess.list2cmdline(argv) if os.name == "nt" else " ".join(shlex.quote(a) for a in argv)


def _host_status_value(host, text, command):
    """(current status-line value or None, the value we install) for one host's config text."""
    if host == "agy":
        data = json.loads(text or "{}")
        if not isinstance(data, dict):
            raise ValueError("agy settings must be a JSON object")
        return data.get("statusLine"), {"command": command}
    ui_table = tomllib.loads(text or "").get("ui", {})
    if not isinstance(ui_table, dict):
        raise ValueError("ui must be a table")
    return ui_table.get("status_line"), {"type": "command", "command": command}


def _host_status_apply(host, base, command):
    if host == "agy":
        data = json.loads(base or "{}")
        data["statusLine"] = {"command": command}
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    head = base if not base or base.endswith("\n") else base + "\n"
    sep = "\n" if head and not head.endswith("\n\n") else ""
    return head + sep + "[ui.status_line]\ntype = \"command\"\ncommand = " + json.dumps(command, ensure_ascii=False) + "\n"


def _host_status_state(path):
    state = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(state, dict) or state.get("version") != 1 or type(state.get("existed")) is not bool or
            not isinstance(state.get("before"), str) or not isinstance(state.get("after"), str) or
            not isinstance(state.get("managed"), dict)):
        raise ValueError("invalid status-line ownership record")
    return state


def seed_host_status_line(config_path, host, *, uninstall=False):
    """Own only the agy `statusLine` / Grok `[ui.status_line]` value; restore only while it remains our exact value.

    The ownership record keeps the pre-install text, so an untouched file returns byte for byte; a file
    edited since is repaired structurally. A status line the user set is never overwritten.
    """
    path = Path(config_path)
    state_path = path.with_name(f".coordinator-status-line-{host}.json")
    text = path.read_bytes().decode("utf-8") if path.exists() else None
    command = _tui_status_command(host)
    if uninstall:
        if not state_path.exists():
            return "absent"
        state = _host_status_state(state_path)
        if text is None:
            state_path.unlink()
            return "absent"
        if text == state["after"]:
            if state["existed"]:
                _footer_write(path, state["before"])
            else:
                path.unlink()
        elif host == "agy":
            data = json.loads(text)
            if not isinstance(data, dict) or data.get("statusLine") != state["managed"]:
                return "retained-customized"
            original = json.loads(state["before"] or "{}").get("statusLine")
            if original is None:
                data.pop("statusLine")
            else:
                data["statusLine"] = original
            _footer_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        else:
            appended = state["after"][len(state["before"]):] if state["after"].startswith(state["before"]) else ""
            if not appended or appended not in text:
                return "retained-customized"
            replacement = text.replace(appended, "", 1)
            tomllib.loads(replacement)
            _footer_write(path, replacement)
        state_path.unlink()
        return "restored"
    current, wanted = _host_status_value(host, text, command)
    if current == wanted:
        return "present"
    state = _host_status_state(state_path) if state_path.exists() else None
    if current is not None:
        if state is None or text != state["after"]:
            return "retained-customized"
        base, existed = state["before"], state["existed"]
    else:
        base, existed = text or "", text is not None
    replacement = _host_status_apply(host, base, command)
    if _host_status_value(host, replacement, command)[0] != wanted:
        raise ValueError("status-line edit did not address the host's status line")
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"version": 1, "host": host, "existed": existed, "before": base, "after": replacement, "managed": wanted}
    _footer_write(state_path, json.dumps(record, ensure_ascii=False) + "\n")
    _footer_write(path, replacement)
    return "updated" if text is not None else "created"


AGY_ALLOW_RULE = "command(coordinator-invoke *)"


def seed_agy_allow_rule(config_path, *, uninstall=False):
    """Own one entry in agy's `permissions.allow`, so a headless `agy --print` can run the coordinator door.

    Operator entries are never touched. The ownership record keeps the pre-install text: an untouched
    file returns byte for byte; a file edited since is repaired structurally.
    """
    path = Path(config_path)
    state_path = path.with_name(".coordinator-allow-rule-agy.json")
    text = path.read_bytes().decode("utf-8") if path.exists() else None
    if uninstall:
        if not state_path.exists():
            return "absent"
        state = _host_status_state(state_path)
        if text is None:
            state_path.unlink()
            return "absent"
        if text == state["after"]:
            if state["existed"]:
                _footer_write(path, state["before"])
            else:
                path.unlink()
        else:
            data = json.loads(text)
            perms = data.get("permissions") if isinstance(data, dict) else None
            allow = perms.get("allow") if isinstance(perms, dict) else None
            if not isinstance(allow, list) or AGY_ALLOW_RULE not in allow:
                state_path.unlink()
                return "absent"
            allow.remove(AGY_ALLOW_RULE)
            if not allow:
                del perms["allow"]
            if not perms:
                del data["permissions"]
            _footer_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        state_path.unlink()
        return "restored"
    data = json.loads(text or "{}")
    if not isinstance(data, dict):
        raise ValueError("agy settings must be a JSON object")
    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        raise ValueError("agy settings permissions must be an object")
    allow = perms.setdefault("allow", [])
    if not isinstance(allow, list):
        raise ValueError("agy settings permissions.allow must be a list")
    if AGY_ALLOW_RULE in allow:
        return "present"
    allow.append(AGY_ALLOW_RULE)
    replacement = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"version": 1, "host": "agy", "existed": text is not None, "before": text or "",
              "after": replacement, "managed": {"allow": AGY_ALLOW_RULE}}
    _footer_write(state_path, json.dumps(record, ensure_ascii=False) + "\n")
    _footer_write(path, replacement)
    return "updated" if text is not None else "created"


def codex_rules_path():
    """Codex's global instructions file: $CODEX_HOME/AGENTS.md, default ~/.codex."""
    return codex_hooks_path().with_name("AGENTS.md")


def agy_rules_path():
    """agy's global rules file (agy 1.4.12 loads ~/.gemini/GEMINI.md and ~/.gemini/AGENTS.md)."""
    return Path.home() / ".gemini" / "GEMINI.md"


def codex_exec_rule_path():
    """Our Codex execpolicy file: $CODEX_HOME/rules/unimatrix.rules."""
    return codex_hooks_path().parent / "rules" / "unimatrix.rules"


def render_codex_exec_rule(dest_dir):
    """An allow rule for the send command alone, so a Codex reply to a peer needs no approval prompt."""
    argv = [sys.executable, str(Path(dest_dir) / SEND_SCRIPT)]
    return ("# unimatrix: lets Codex answer a peer agent with the send command, without an approval prompt.\n"
            "# Only this one script. Written by unimatrix-install.py --install, removed by --uninstall.\n"
            f"prefix_rule(pattern={json.dumps(argv)}, decision=\"allow\")\n")


def seed_file(path, text):
    """Write a whole file we own; return "created", "updated" or "present"."""
    path = Path(path)
    if path.exists():
        if path.read_text(encoding="utf-8") == text:
            return "present"
        path.write_text(text, encoding="utf-8")
        return "updated"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return "created"


def unseed_file(path):
    path = Path(path)
    if not path.exists():
        return "absent"
    path.unlink()
    return "removed"


LOGIN_AGENT_LABEL = "com.unimatrix.autolink"
LOGIN_AGENT_INTERVAL_S = 60
LOGIN_TASK_NAME = "UnimatrixAutolink"
LOGIN_UNIT = "unimatrix-autolink.service"


def run_schtasks(*args):
    """Run schtasks.exe; None when it cannot launch."""
    try:
        return subprocess.run(["schtasks.exe", *args], capture_output=True, text=True, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None


def login_agent_dir():
    """Where this OS reads user login agents; None on a platform with none."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents"
    if sys.platform.startswith("linux"):
        return Path.home() / ".config" / "systemd" / "user"
    return None


def _autolink_argv(dest_dir):
    return [sys.executable, str(Path(dest_dir) / AUTOLINK_SCRIPT)]


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _winreg():
    import winreg
    return winreg


def _windows_run_command(dest_dir):
    """Autolink command line for the HKCU Run value; pythonw.exe beside the interpreter keeps it windowless."""
    argv = _autolink_argv(dest_dir)
    pythonw = Path(argv[0]).with_name("pythonw.exe")
    if pythonw.is_file():
        argv[0] = str(pythonw)
    return subprocess.list2cmdline(argv)


def _delete_run_value():
    """Delete the HKCU Run value; True when one was there."""
    wr = _winreg()
    _refuse_real_run_key(wr)
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, RUN_KEY, 0, wr.KEY_SET_VALUE) as key:
            wr.DeleteValue(key, LOGIN_TASK_NAME)
    except FileNotFoundError:
        return False
    return True


def _delete_login_task(schtasks=None, strict=False):
    """Delete a legacy scheduled task of the old registration; True when one was removed.

    Best effort unless strict, where a found task that will not delete raises OSError.
    """
    run = schtasks or run_schtasks
    found = run("/Query", "/TN", LOGIN_TASK_NAME)
    if found is None or found.returncode != 0:
        return False
    proc = run("/Delete", "/TN", LOGIN_TASK_NAME, "/F")
    if proc is None or proc.returncode != 0:
        if strict:
            raise OSError(f"schtasks /Delete {LOGIN_TASK_NAME} failed")
        return False
    return True


def _login_agent_text(dest_dir):
    """Bytes of the login-agent file for this OS: a RunAtLoad plist that launchd re-runs every
    LOGIN_AGENT_INTERVAL_S (autolink's healthy path reads records and spawns nothing; a dead supervisor is
    relinked), or a oneshot systemd unit. AbandonProcessGroup keeps launchd's job-exit cleanup from reaping
    the supervisors autolink spawned."""
    argv = _autolink_argv(dest_dir)
    if sys.platform == "darwin":
        import plistlib
        return plistlib.dumps({"Label": LOGIN_AGENT_LABEL, "ProgramArguments": argv, "RunAtLoad": True,
                               "StartInterval": LOGIN_AGENT_INTERVAL_S, "AbandonProcessGroup": True,
                               "ProcessType": "Background", "LowPriorityIO": True})
    return ("[Unit]\nDescription=Unimatrix autolink at login\n\n[Service]\nType=oneshot\n"
            f"ExecStart={' '.join(shlex.quote(a) for a in argv)}\n\n[Install]\nWantedBy=default.target\n"
            ).encode("utf-8")


def _login_agent_paths(agent_dir):
    name = f"{LOGIN_AGENT_LABEL}.plist" if sys.platform == "darwin" else LOGIN_UNIT
    return Path(agent_dir) / name, Path(agent_dir) / "default.target.wants" / LOGIN_UNIT


_TEST_HOME = []
try:
    _REAL_HOME = Path.home()
except RuntimeError:
    _REAL_HOME = None


def _test_home():
    """The test-home guard module (lib/unimatrix_test_home.py), loaded once; None when it is not shipped."""
    if not _TEST_HOME:
        path = _BIN_DIR / "lib" / "unimatrix_test_home.py"
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location("unimatrix_test_home", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _TEST_HOME.append(mod)
    return _TEST_HOME[0]


def _refuse_real_run_key(wr):
    """Under a test settings home, refuse a registry module that is the real `winreg`; a stand-in object passes."""
    guard = _test_home() if os.environ.get("UNIMATRIX_TEST_HOME") else None
    if guard is not None and isinstance(wr, types.ModuleType):
        guard.refuse_real("run_key", f"HKCU\\{RUN_KEY}\\{LOGIN_TASK_NAME}")


def _refuse_real_agent_dir(root):
    """Under a test settings home, refuse the default plist/systemd dir of the account home captured at
    import (a test that redirects HOME afterwards resolves elsewhere) unless it is inside the settings home."""
    guard = _test_home() if os.environ.get("UNIMATRIX_TEST_HOME") else None
    if guard is None or _REAL_HOME is None:
        return
    here = os.path.normcase(os.path.abspath(os.fspath(root)))
    real_dirs = (_REAL_HOME / "Library" / "LaunchAgents", _REAL_HOME / ".config" / "systemd" / "user")
    if here not in {os.path.normcase(os.path.abspath(os.fspath(d))) for d in real_dirs}:
        return
    try:
        guard.refuse_real("key_path", root)
    except guard.TestHomeRefusal:
        raise guard.TestHomeRefusal("login_agent_dir", root) from None


def register_login_agent(dest_dir, agent_dir=None, schtasks=None):
    """Register one run-once-at-login agent for the installed autolink; return (state, where).

    state is "created", "updated" or "present" (plist / systemd unit), "registered" (Windows HKCU Run value,
    needs no elevation; overwritten in place and any legacy scheduled task removed), or "skipped"
    (no login-agent mechanism here, or the registry write failed).
    Plain file writes on macOS and Linux: picked up at next login, no process spawned.
    """
    if sys.platform == "win32":
        wr = _winreg()
        _refuse_real_run_key(wr)
        try:
            with wr.CreateKeyEx(wr.HKEY_CURRENT_USER, RUN_KEY, 0, wr.KEY_SET_VALUE) as key:
                wr.SetValueEx(key, LOGIN_TASK_NAME, 0, wr.REG_SZ, _windows_run_command(dest_dir))
        except OSError as exc:
            return "skipped", f"HKCU Run value write failed: {exc}"
        _delete_login_task(schtasks)
        return "registered", f"HKCU Run value {LOGIN_TASK_NAME}"
    root = Path(agent_dir) if agent_dir else login_agent_dir()
    if root is None:
        return "skipped", f"no login agent for platform {sys.platform!r}"
    if not agent_dir:
        _refuse_real_agent_dir(root)
    target, link = _login_agent_paths(root)
    data = _login_agent_text(dest_dir)
    state = "created"
    if target.exists():
        state = "present" if target.read_bytes() == data else "updated"
    if state != "present":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if sys.platform != "darwin" and not link.is_symlink():
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(f"../{LOGIN_UNIT}")
    return state, str(target)


def unregister_login_agent(agent_dir=None, schtasks=None):
    """Remove the login agent; return "removed" or "absent" (absence is not an error)."""
    if sys.platform == "win32":
        value = _delete_run_value()
        task = _delete_login_task(schtasks, strict=True)
        return "removed" if value or task else "absent"
    root = Path(agent_dir) if agent_dir else login_agent_dir()
    if root is None:
        return "absent"
    removed = False
    for path in _login_agent_paths(root):
        if path.is_symlink() or path.exists():
            path.unlink()
            removed = True
    return "removed" if removed else "absent"


LEGACY_LOGIN_AGENT_LABELS = ("com.coordinator.unimatrix-autolink",)


def run_launchctl(*args):
    """Run launchctl; None when it cannot launch."""
    try:
        return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None


def remove_legacy_login_agents(agent_dir=None, launchctl=None, uid=None):
    """Boot out and delete the prior-era autolink labels so one supervisor remains; macOS only.

    Returns the labels removed: a loaded label is booted out of `gui/<uid>`, its plist unlinked.
    A label neither loaded nor on disk is a no-op, so a rerun returns [].
    """
    if sys.platform != "darwin":
        return []
    run = launchctl or run_launchctl
    root = Path(agent_dir) if agent_dir else login_agent_dir()
    if not agent_dir:
        _refuse_real_agent_dir(root)
    domain = f"gui/{uid if uid is not None else os.getuid()}"
    removed = []
    for label in LEGACY_LOGIN_AGENT_LABELS:
        plist = root / f"{label}.plist"
        loaded = run("print", f"{domain}/{label}")
        did = False
        if loaded is not None and loaded.returncode == 0:
            booted = run("bootout", f"{domain}/{label}")
            did = booted is not None and booted.returncode == 0
        if plist.is_symlink() or plist.exists():
            plist.unlink()
            did = True
        if did:
            removed.append(label)
    return removed


def _pid_alive(pid):
    """True when `pid` exists (a pid we may not signal still exists)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def reap_stale_relays(dest_dir, ps=None, kill=None):
    """SIGTERM relay sessions that are orphaned (sshd parent gone) or older than the installed relay
    script's st_ctime; a live current session is kept. POSIX only. Returns (stale, mismatched, failed) pid lists."""
    if sys.platform == "win32":
        return [], [], []
    rs = _lib("unimatrix_relay_sessions")
    sessions = rs.list_relay_sessions((ps or rs.ps_snapshot)())
    stamps = {}
    for name in rs.RELAY_SCRIPTS:
        try:
            stamps[name] = (Path(dest_dir) / name).stat().st_ctime
        except OSError:
            continue
    groups = rs.classify_relay_sessions(sessions, stamps, _pid_alive)
    victims = groups[rs.STALE] + groups[rs.MISMATCHED]
    _, failed = rs.reap(victims, kill)
    return [s.pid for s in groups[rs.STALE]], [s.pid for s in groups[rs.MISMATCHED]], failed


def render_peer_doctrine(dest_dir, template=None):
    """Return the managed block: the doctrine template with {SEND} bound to the installed send command."""
    body = Path(template or PEER_DOCTRINE_TEMPLATE).read_text(encoding="utf-8").strip("\n")
    body = body.replace("{SEND}", _hook_command(dest_dir, SEND_SCRIPT))
    return f"{DOCTRINE_BEGIN}\n{body}\n{DOCTRINE_END}\n"


_SEED_MODULE = None


def _seed():
    """The doctrine-seed module (marker-parameterised block helpers, skills seeders), loaded once from beside this file."""
    global _SEED_MODULE
    if _SEED_MODULE is None:
        spec = importlib.util.spec_from_file_location("unimatrix_doctrine_seed", _BIN_DIR / DOCTRINE_SEED_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SEED_MODULE = mod
    return _SEED_MODULE


def _plugin_gen():
    """The plugin-tree generator module, loaded from beside this file."""
    spec = importlib.util.spec_from_file_location("unimatrix_plugin_gen", _BIN_DIR / PLUGIN_GEN_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_agy_plugin_tree(args, out, dest=None):
    """Generate the agy plugin tree from --plugin-root (or PLUGIN_ROOT) into --out (or <dest>/agy-plugin).

    `dest` defaults to <settings home>/unimatrix. True when written; a generator OSError/ValueError
    prints WARN and returns False, never raises.
    """
    plugin_root = Path(args.plugin_root) if args.plugin_root else PLUGIN_ROOT
    tree = Path(args.out) if args.out else (dest or settings_home() / "unimatrix") / AGY_PLUGIN_TREE_DIR
    if not args.out and not plugin_root.is_dir():
        print(f"NOTE: agy plugin tree skipped: plugin root {plugin_root} not found (name one with --plugin-root)", file=out)
        return True
    try:
        report = _plugin_gen().generate(plugin_root, tree, "agy")
    except (OSError, ValueError) as exc:
        print(f"WARN: agy plugin tree not written: {exc}", file=out)
        return False
    print(f"agy plugin tree written {len(report['written'])} files in {tree}; "
          f"unmapped {len(report['unmapped'])} (see {tree / 'UNMAPPED.json'})", file=out)
    return True


def _split_block(text):
    """Return (before, after) around our peer-doctrine block, or None when absent."""
    return _seed().split_block(text, DOCTRINE_BEGIN, DOCTRINE_END)


def seed_peer_doctrine(path, block):
    """Write our managed block into a rules file; return "created", "added", "updated" or "present".

    Text outside the markers is never touched. A re-install with identical text changes nothing.
    """
    return _seed().seed_block(path, block, DOCTRINE_BEGIN, DOCTRINE_END)


def unseed_peer_doctrine(path):
    """Remove only our managed block (deleting a file left empty); return "removed" or "absent"."""
    return _seed().unseed_block(path, DOCTRINE_BEGIN, DOCTRINE_END)


def _content_root_pointer():
    """(pointer value or "", the pointer files consulted), read by the shared spawn-free resolver."""
    try:
        mod = _lib("machine_local_impl_resolve")
        value = mod.read_content_root_pointer()
        names = (mod._CONTENT_ROOT_POINTER, mod._LEGACY_ROOT_POINTER)
        dirs = (os.path.join(mod.settings_home(), "machine-local"), mod.claude_home())
    except (OSError, AttributeError):
        return "", ["<content-root pointer unreadable: lib/machine_local_impl_resolve.py missing>"]
    return value, [os.path.join(d, n) for n in names for d in dirs]


def resolve_doctrine(plugin_root=None):
    """Return (root or None, rung, detail): where global-doctrine/ resolved, which rung, what was read.

    Rungs in order: the DoE content-root pointer, then the base beside this install, where a flat
    published checkout keeps the tree under `templates/`. A named `plugin_root` is an explicit override:
    it replaces both the pointer and this script's location. On a miss `rung` is None and `detail`
    names the pointer files read and the beside-install candidates.
    """
    pointer, pointer_files = ("", []) if plugin_root else _content_root_pointer()
    if pointer and (Path(pointer) / "global-doctrine" / "CLAUDE.md").is_file():
        return Path(pointer), "DoE root pointer", pointer
    base = Path(plugin_root) if plugin_root else PLUGIN_ROOT
    for root in (base.parent, base, base.parent / "templates", base / "templates"):
        if (root / "global-doctrine" / "CLAUDE.md").is_file():
            return root, "beside install" if not plugin_root else "--plugin-root", str(root)
    if plugin_root:
        return None, None, f"--plugin-root named, pointer not consulted; searched beside {base}"
    state = f"pointer value '{pointer}' has no global-doctrine/CLAUDE.md" if pointer else "no pointer value"
    return None, None, (f"{state}; pointer files read: {', '.join(pointer_files)}; "
                        f"also searched beside {base}")


def doctrine_root(plugin_root=None):
    """The directory holding global-doctrine/, or None when neither the DoE root pointer nor this install carries it."""
    return resolve_doctrine(plugin_root)[0]


def resolve_engine_root():
    """Return (engine_root or None, provenance); never raises.

    Uses the hook-side resolver when this install carries it, else the in-tree settings-home ladder
    (cc_invoke.resolve_engine_root). A miss returns None with the resolver's first failure line.
    """
    hook_side = PLUGIN_ROOT / "hooks" / "scripts" / "_engine_root.py"
    try:
        if hook_side.is_file():
            spec = importlib.util.spec_from_file_location("_engine_root_for_install", hook_side)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            root, _cls, prov = mod.resolve_claude_klabauter_root_with_provenance()
        else:
            spec = importlib.util.spec_from_file_location("_cc_invoke_for_install", _BIN_DIR / "lib" / "cc_invoke.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            root, prov = mod.resolve_engine_root(str(Path(__file__).resolve())), "settings-home ladder"
    except Exception as exc:
        lines = str(exc).strip().splitlines()
        return None, f"resolver failed: {lines[0] if lines else type(exc).__name__}"
    return (str(root), prov) if root and Path(root).is_dir() else (None, prov if root is None else "root missing")


def trust_codex_hook(hooks_path, timeout=30):
    """Mark our Codex SessionStart hook trusted in config.toml via `codex app-server`.

    Codex runs a user hook only after its content hash is recorded under hooks.state; the app-server
    reports the hash (hooks/list) and writes it (config/value/write). Returns (ok, message); never raises.
    """
    exe = shutil.which("codex")
    if not exe:
        return False, "codex not on PATH; hook stays untrusted until reviewed in /hooks"
    hooks_file = Path(hooks_path)
    env = dict(os.environ, CODEX_HOME=str(hooks_file.parent))
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            [exe, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=env, **kwargs,
        )
    except OSError as exc:
        return False, f"codex app-server did not start: {exc}"
    timer = threading.Timer(timeout, proc.kill)
    timer.start()

    def rpc(rid, method, params):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("codex app-server closed before answering")
            msg = json.loads(line)
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg["result"]

    try:
        rpc(1, "initialize", {"clientInfo": {"name": "unimatrix-install", "version": "1"}})
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized"}) + "\n")
        proc.stdin.flush()
        expected = _codex_expected_handlers(hooks_file)
        here = hooks_file.resolve()

        def discover(rid):
            """One row per expected handler, keyed by event id; ValueError when any is missing, doubled or misplaced."""
            listed = rpc(rid, "hooks/list", {"cwds": []})
            mine = [
                h for entry in listed.get("data", []) for h in entry.get("hooks", [])
                if str(h.get("command", "")) in expected.values()
                and Path(h.get("sourcePath", "")).resolve() == here
            ]
            found = {}
            for h in mine:
                ident = (_event_id(h.get("eventName", "")), str(h.get("command", "")))
                if expected.get(ident) != h.get("command") or ident in found:
                    raise ValueError(f"{h.get('eventName', 'hook')} handler is duplicated or on the wrong event")
                found[ident] = h
            missing = sorted(set(expected) - set(found))
            if missing:
                raise ValueError(f"codex did not discover the {', '.join(event for event, _command in missing)} handler (features.hooks disabled?)")
            if len({h.get("key") for h in found.values()}) != len(found):
                raise ValueError("discovered handlers share a trust key")
            return found

        before = discover(2)
        for hook in before.values():
            if not (isinstance(hook.get("key"), str) and hook["key"]
                    and isinstance(hook.get("currentHash"), str) and hook["currentHash"]):
                raise ValueError(f"{hook.get('eventName', 'hook')} reports no key or content hash")
            rpc(3, "config/value/write", {
                "keyPath": f'hooks.state.{json.dumps(hook["key"], ensure_ascii=False)}.trusted_hash',
                "value": hook["currentHash"],
                "mergeStrategy": "upsert",
            })
        after = discover(4)
        for ident, hook in after.items():
            name = hook.get("eventName", ident)
            current, trusted = hook.get("currentHash"), hook.get("trustedHash")
            if hook.get("key") != before[ident]["key"] or current != before[ident]["currentHash"]:
                raise ValueError(f"{name} changed while trust was being written")
            if hook.get("enabled") is not True:
                raise ValueError(f"{name} is disabled")
            if not (isinstance(current, str) and current):
                raise ValueError(f"{name} reports no content hash after trust was written")
            # Real Codex reports trustStatus without trustedHash; a trustedHash it does report must still match.
            if trusted is not None and not (isinstance(trusted, str) and trusted == current):
                raise ValueError(f"{name} is untrusted (trustedHash does not equal currentHash)")
            if trusted is None and hook.get("trustStatus") != "trusted":
                raise ValueError(f"{name} is untrusted (no trustedHash and trustStatus is not trusted)")
        return True, f"trusted {len(after)} hook(s)"
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        return False, f"codex hook trust failed: {exc}"
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()


def main(argv=None, version_text=None, dest_dir=None, out=None, hooks_path=None,
         codex_hooks=None, codex_trust=None, codex_rules=None, agy_rules=None, codex_exec_rule=None,
         agy_skills_json=None, codex_skills=None, agent_dir=None, schtasks=None, pair_run=None,
         router_live=None, restart_router=None, launchctl=None, relay_ps=None, relay_kill=None, authorized_keys=None):
    """`authorized_keys` overrides the local file whose stale forced-command interpreters --install rewrites;
    an overridden `hooks_path` otherwise skips it. `launchctl` (argv tail -> CompletedProcess), `relay_ps` (() -> ps text) and `relay_kill` (pid -> None)
    replace the real launchctl, `ps` and SIGTERM, which an overridden `hooks_path` otherwise skips.
    `router_live(dest) -> bool` and `restart_router(dest, say) -> dict` replace the installed autolink's
    router check and restart. `hooks_path` overrides agy's file. Codex's file defaults to codex_hooks_path(), or to a sibling
    `codex-hooks.json` when only `hooks_path` is overridden; trust runs against the real Codex only for
    the default path, unless `codex_trust` (a callable taking the hooks path) is injected. The peer
    doctrine files follow the same rule: real home files by default, siblings `codex-AGENTS.md` and
    `agy-GEMINI.md` of `hooks_path` when it is overridden, or the explicit `codex_rules`/`agy_rules`.
    The login agent lives in the OS agent directory by default, a `login-agent` sibling of `hooks_path`
    when that is overridden, or `agent_dir`; `schtasks` (argv tail -> CompletedProcess) replaces the real
    Task Scheduler, which an overridden `hooks_path` otherwise skips."""
    out = out or sys.stdout
    if agent_dir is None and hooks_path is not None:
        agent_dir = Path(hooks_path).with_name("login-agent")
    if schtasks is None and hooks_path is not None:
        schtasks = lambda *a: None
    if hooks_path is not None:
        launchctl = launchctl or (lambda *a: None)
        relay_ps = relay_ps or (lambda: "")
    codex_default = codex_hooks is None and hooks_path is None
    codex_hp = Path(codex_hooks) if codex_hooks else (
        codex_hooks_path() if hooks_path is None else Path(hooks_path).with_name("codex-hooks.json")
    )
    if codex_rules:
        codex_rp = Path(codex_rules)
    else:
        codex_rp = codex_rules_path() if hooks_path is None else Path(hooks_path).with_name("codex-AGENTS.md")
    if codex_exec_rule:
        codex_xp = Path(codex_exec_rule)
    else:
        codex_xp = codex_exec_rule_path() if hooks_path is None else Path(hooks_path).with_name("codex-unimatrix.rules")
    if agy_rules:
        agy_rp = Path(agy_rules)
    else:
        agy_rp = agy_rules_path() if hooks_path is None else Path(hooks_path).with_name("agy-GEMINI.md")
    agy_status = agy_settings_path() if hooks_path is None else Path(hooks_path).with_name("agy-settings.json")
    grok_status = grok_config_path() if hooks_path is None else Path(hooks_path).with_name("grok-config.toml")
    grok_rp = grok_rules_path() if hooks_path is None else Path(hooks_path).with_name("grok-peer-doctrine.md")
    if codex_trust is None and codex_default:
        codex_trust = trust_codex_hook
    agy_sj = Path(agy_skills_json) if agy_skills_json else (
        agy_hooks_path().with_name("skills.json") if hooks_path is None else Path(hooks_path).with_name("agy-skills.json")
    )
    agy_pj = agy_plugins_path() if hooks_path is None else Path(hooks_path).with_name("agy-plugins.json")
    codex_sd = Path(codex_skills) if codex_skills else (
        Path.home() / ".agents" / "skills" if hooks_path is None else Path(hooks_path).with_name("codex-agents-skills")
    )
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--install", action="store_true", help="copy the vendored scripts into the settings home")
    ap.add_argument("--uninstall", action="store_true", help="remove everything --install seeded for Codex and agy")
    ap.add_argument("--force", action="store_true", help="warn instead of refusing outside the verified range")
    ap.add_argument("--hosts", help="comma list of claude,codex,agy,grok (grok installs a status line and the peer doctrine rules file, no hooks); default codex,agy,grok with the Claude range gate. "
                    "Naming no claude skips the gate when `claude` is not on PATH")
    ap.add_argument("--pair", metavar="ALIAS", help="mint or read the pair key for a far box and print its "
                    "authorized_keys line and ssh config block")
    ap.add_argument("--file-key", action="store_true", help="mint (or read) this box's file-transfer key, "
                    "separate from the relay key, and print its public line and fingerprint for the membership signer")
    ap.add_argument("--control-key", metavar="BOX", help="mint or read the control key for a far box "
                    "(~/.ssh/unimatrix_control_<BOX>) and print its authorized_keys line and the "
                    f"{CONTROL_ALIAS_PREFIX}<BOX> ssh config block; the control key can only launch or wake a session")
    ap.add_argument("--unencrypted-key", action="store_true", help="with --pair, --file-key or --control-key: mint the private key "
                    "without a passphrase (for boxes with no 1Password); the default mint is passphrase-encrypted")
    ap.add_argument("--remint", action="store_true", help="with --pair, --file-key or --control-key: delete the existing private "
                    "key, its .pub and its 1Password passphrase item, then mint anew")
    ap.add_argument("--plugin-root", metavar="DIR", help="seed Codex and agy doctrine and skills from this plugin "
                    "root (doctrine in DIR.parent or DIR, skills in DIR/skills) instead of this script's own location")
    ap.add_argument("--out", metavar="DIR", help="write the agy plugin tree (translated plugin source) into DIR; "
                    f"default under --install: <settings home>/unimatrix/{AGY_PLUGIN_TREE_DIR}")
    args = ap.parse_args(argv)
    skills_root = Path(args.plugin_root) / "skills" if args.plugin_root else SKILLS_ROOT
    if args.file_key:
        try:
            priv, pub_line, fp = file_key(run=pair_run or subprocess.run, unencrypted=args.unencrypted_key,
                                          remint=args.remint, on_mint=lambda ref: print(
                                              f"minted a passphrase-encrypted private key\npassphrase: {ref}", file=out))
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            print(f"REFUSE: {exc}", file=out)
            return 6
        print(f"file key: {priv}\nfingerprint: {fp}\n"
              f"file_pub (give to the membership signer; verify the fingerprint there): {pub_line}", file=out)
        return 0
    if args.control_key is not None:
        try:
            priv, body = control_key(args.control_key, run=pair_run or subprocess.run, unencrypted=args.unencrypted_key,
                                     remint=args.remint, on_mint=lambda ref: print(
                                         f"minted a passphrase-encrypted private key\npassphrase: {ref}", file=out))
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            print(f"REFUSE: {exc}", file=out)
            return 6
        print(f"control key for {args.control_key}: {priv}", file=out)
        print("ssh config block for this box (add to ~/.ssh/config):", file=out)
        print(render_ssh_config_block(args.control_key, priv, host_alias=f"{CONTROL_ALIAS_PREFIX}{args.control_key}"),
              file=out)
        status, line = install_over_link(args.control_key, body, run=pair_run or subprocess.run, control=True)
        if status == "no-link":
            print(f"no authorized link to {args.control_key}: paste on the far box (interpreter and path are "
                  f"placeholders: {FAR_INTERPRETER} and {FAR_DEST_DIR}; use the far box's absolute ones):\n"
                  f"{authorized_keys_line(FAR_DEST_DIR, FAR_INTERPRETER, body, control=True)}", file=out)
        elif status == "failed":
            print(f"install over the link to {args.control_key} failed: paste this line on the far box:\n{line}", file=out)
        elif status == "admin":
            print(f"{args.control_key} is a Windows administrator account: nothing written. Append this line to "
                  f"%ProgramData%\\ssh\\administrators_authorized_keys on the far box:\n{line}", file=out)
        else:
            print(f"authorized_keys line {'already present' if status == 'present' else 'added'} on {args.control_key}; "
                  f"line:\n{line}", file=out)
        return 0
    if args.pair is not None:
        try:
            priv, body = pair_key(args.pair, run=pair_run or subprocess.run, unencrypted=args.unencrypted_key,
                                  remint=args.remint, on_mint=lambda ref: print(
                                      f"minted a passphrase-encrypted private key\npassphrase: {ref}", file=out))
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            print(f"REFUSE: {exc}", file=out)
            return 6
        print(f"pair key for {args.pair}: {priv}", file=out)
        if _sibling("unimatrix-membership.py").load_pin(_sibling("unimatrix-roster.py").settings_home()) is not None:
            print(f"signer pin present: no ssh config or authorized_keys edit. On this box run "
                  f"`python {_sibling('unimatrix-roster.py').settings_home() / 'unimatrix' / 'unimatrix-pair.py'} "
                  f"request --box {args.pair}` and give the JSON to the operator, who runs `unimatrix-pair.py "
                  f"approve <request-file>` (the approval is the only 1Password prompt).", file=out)
            return 0
        print("no signer pin (pre-membership): ssh config block for this box (add to ~/.ssh/config):", file=out)
        print(render_ssh_config_block(args.pair, priv), file=out)
        print(f"authorized_keys line for the far box (interpreter and path are placeholders: {FAR_INTERPRETER} "
              f"and {FAR_DEST_DIR}; replace with the far box's absolute ones):", file=out)
        print(authorized_keys_line(FAR_DEST_DIR, FAR_INTERPRETER, body), file=out)
        status, line = install_over_link(args.pair, body, run=pair_run or subprocess.run)
        if status == "no-link":
            print(f"no authorized link to {args.pair}: paste the line above on the far box", file=out)
        elif status == "failed":
            print(f"install over the link to {args.pair} failed: paste this line on the far box:\n{line}", file=out)
        elif status == "admin":
            print(f"{args.pair} is a Windows administrator account: nothing written. Append this line to "
                  f"%ProgramData%\\ssh\\administrators_authorized_keys on the far box:\n{line}", file=out)
        else:
            print(f"authorized_keys line {'already present' if status == 'present' else 'added'} on {args.pair}; "
                  f"line:\n{line}", file=out)
        return 0
    hosts = None
    if args.hosts is not None:
        hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
        bad = [h for h in hosts if h not in HOST_NAMES]
        if bad or not hosts:
            print(f"REFUSE: --hosts takes {','.join(HOST_NAMES)}; got {args.hosts!r}", file=out)
            return 2
    want_codex = hosts is None or "codex" in hosts
    want_agy = hosts is None or "agy" in hosts
    want_grok = hosts is None or "grok" in hosts

    if args.uninstall:
        hp = Path(hooks_path) if hooks_path else agy_hooks_path()
        steps = []
        if want_grok:
            steps.append(("grok status line", grok_status, lambda: seed_host_status_line(grok_status, "grok", uninstall=True)))
            steps.append(("grok peer doctrine", grok_rp, lambda: unseed_peer_doctrine(grok_rp)))
        if want_agy:
            steps.append(("agy allow rule", agy_status, lambda: seed_agy_allow_rule(agy_status, uninstall=True)))
            steps.append(("agy status line", agy_status, lambda: seed_host_status_line(agy_status, "agy", uninstall=True)))
            steps.append(("agy hook", hp, lambda: unseed_agy_hook(hp)))
            steps.append(("agy plugin", agy_pj, lambda: unseed_agy_plugin(
                agy_pj, (Path(dest_dir) if dest_dir else settings_home() / "unimatrix") / AGY_PLUGIN_TREE_DIR)))
            steps.append(("agy skills", agy_sj, lambda: _seed().unseed_agy_skills(agy_sj, skills_root)))
            steps.append(("agy coordinator doctrine", agy_rp, lambda: _seed().unseed_block(
                agy_rp, _seed().DOCTRINE_BEGIN, _seed().DOCTRINE_END)))
            steps.append(("agy peer doctrine", agy_rp, lambda: unseed_peer_doctrine(agy_rp)))
        if want_codex:
            steps.append(("codex status footer", codex_hp.with_name("config.toml"), lambda: seed_codex_status_line(codex_hp.with_name("config.toml"), uninstall=True)))
            steps.append(("codex hook", codex_hp, lambda: unseed_codex_hook(codex_hp)))
            steps.append(("codex skills", codex_sd, lambda: ",".join(_seed().unseed_codex_skills(codex_sd)) or "absent"))
            steps.append(("codex coordinator doctrine", codex_rp, lambda: _seed().unseed_block(
                codex_rp, _seed().DOCTRINE_BEGIN, _seed().DOCTRINE_END)))
            steps.append(("codex peer doctrine", codex_rp, lambda: unseed_peer_doctrine(codex_rp)))
            steps.append(("codex send rule", codex_xp, lambda: unseed_file(codex_xp)))
        steps.append(("login agent", agent_dir or login_agent_dir() or LOGIN_TASK_NAME,
                      lambda: unregister_login_agent(agent_dir, schtasks)))
        for label, where, fn in steps:
            try:
                res = fn()
            except (ValueError, OSError) as exc:
                print(f"REFUSE: {exc}", file=out)
                return 4
            print(f"{label} {res} in {where}", file=out)
        return 0

    if version_text is None:
        gate_skipped = hosts is not None and "claude" not in hosts
        if gate_skipped:
            print("NOTE: --hosts names no claude; Claude version-range gate skipped", file=out)
        else:
            try:
                version_text = probe_claude_version()
            except RuntimeError as exc:
                print(f"REFUSE: {exc}", file=out)
                return 2
    if version_text is not None:
        try:
            status, msg = check_version(version_text, args.force)
        except RuntimeError as exc:
            print(f"REFUSE: {exc}", file=out)
            return 2
        print(msg, file=out)
        if status == "refuse":
            return 1
    if want_agy and args.out and not args.install:
        if _write_agy_plugin_tree(args, out):
            return 0
        print("PARTIAL INSTALL: not installed: agy plugin tree", file=out)
        return 7
    if not args.install:
        return 0

    print(NATIVE_RC_NOTICE, file=out)
    dest = Path(dest_dir) if dest_dir else settings_home() / "unimatrix"
    digest_before = router_closure_digest(dest)
    try:
        copied = install_scripts(Path(__file__).resolve().parent, dest)
    except FileNotFoundError as exc:
        print(f"REFUSE: {exc}", file=out)
        return 3
    except PipeCapabilityRefused as exc:
        print(f"REFUSE: {exc}", file=out)
        return 5
    print(f"installed {len(copied)} scripts into {dest}", file=out)
    stale, mismatched, failed = reap_stale_relays(dest, relay_ps, relay_kill)
    if stale or mismatched or failed:
        print(f"relay sessions reaped: {len(stale)} orphaned, {len(mismatched)} on replaced code"
              + (f"; could not signal pid {', '.join(map(str, failed))}" if failed else ""), file=out)
    restart_failed = False
    if authorized_keys is None and hooks_path is None:
        authorized_keys = Path.home() / ".ssh" / "authorized_keys"
    if authorized_keys is not None:
        try:
            rewrote = rewrite_forced_interpreters(authorized_keys, forced_command_interpreter())
        except OSError as exc:
            rewrote = 0
            print(f"WARN: authorized_keys forced-command interpreters not rewritten: {exc}", file=out)
        if rewrote:
            print(f"authorized_keys: {rewrote} forced-command interpreter(s) rewritten to {forced_command_interpreter()}", file=out)
    router_live = router_live or router_is_live
    if router_live(dest) and (digest_before != router_closure_digest(dest)
                              or not (dest / "router" / "router.pid.json").is_file()):
        try:
            res = (restart_router or restart_installed_router)(dest, lambda s: print(s, file=out)) or {}
            print(f"router restarted: {res.get('stopped')} old pid {res.get('old_pid')} new pid {res.get('new_pid')}", file=out)
        except Exception as exc:
            restart_failed = True
            print(f"REFUSE: router restart failed: {exc}; scripts are installed, run `unimatrix-autolink.py restart`", file=out)
    print(f"authorized_keys line for the far side (paste verbatim, fill <key-body>): "
          f"{authorized_keys_line(dest)}", file=out)
    print(f"authorized_keys line for the far side's file key (fill <key-body> and <box>, this box's name): "
          f"{authorized_keys_line(dest, file_peer='<box>')}", file=out)
    print(f"authorized_keys line for the far side's control key (launch/wake only; fill <key-body>): "
          f"{authorized_keys_line(dest, control=True)}", file=out)
    engine_root, provenance = resolve_engine_root()
    hp = Path(hooks_path) if hooks_path else agy_hooks_path()
    if want_agy:
        try:
            res = seed_agy_hook(hp, dest, engine_root)
        except ValueError as exc:
            print(f"REFUSE: {exc}; agy hook not installed", file=out)
            return 4
        print(f"agy hook {res} in {hp}", file=out)
    if want_codex:
        try:
            res = seed_codex_hook(codex_hp, dest, engine_root)
        except ValueError as exc:
            print(f"REFUSE: {exc}; codex hook not installed", file=out)
            return 4
        print(f"codex hook {res} in {codex_hp}", file=out)
    rc = 0
    lifecycle_ready, lifecycle_note = external_session_capability(engine_root)
    if want_codex and not lifecycle_ready:
        print(f"session lifecycle DEGRADED: {lifecycle_note}; transport hooks remain installed", file=out)
        rc = 7
    verified = None
    if want_codex and codex_trust is not None:
        ok, note = codex_trust(codex_hp)
        if ok:
            print(f"codex hook trust: {note}", file=out)
            verified = True
        else:
            print(f"governance verification FAILED: {note}", file=out)
            verified, rc = False, 5
    missing = []
    if want_codex:
        try:
            config = codex_hp.with_name("config.toml")
            print(f"codex status footer {seed_codex_status_line(config)} in {config}", file=out)
        except (OSError, ValueError) as exc:
            missing.append("codex status footer")
            print(f"WARN: codex status footer not installed: {exc}", file=out)
    for label, host, config, wanted in (("agy", "agy", agy_status, want_agy), ("grok", "grok", grok_status, want_grok)):
        if not wanted:
            continue
        try:
            print(f"{label} status line {seed_host_status_line(config, host)} in {config}", file=out)
        except (OSError, ValueError) as exc:
            missing.append(f"{label} status line")
            print(f"WARN: {label} status line not installed: {exc}", file=out)
    if want_agy:
        try:
            print(f"agy allow rule {seed_agy_allow_rule(agy_status)} in {agy_status}", file=out)
        except (OSError, ValueError) as exc:
            missing.append("agy allow rule")
            print(f"WARN: agy allow rule not installed: {exc}", file=out)
        _seed_agy_config(hp.with_name("config.json"), out)
        if not _write_agy_plugin_tree(args, out, dest):
            missing.append("agy plugin tree")
        else:
            tree = Path(args.out) if args.out else dest / AGY_PLUGIN_TREE_DIR
            if tree.is_dir():
                try:
                    print(f"agy plugin {seed_agy_plugin(agy_pj, tree)} in {agy_pj}", file=out)
                except (OSError, ValueError) as exc:
                    missing.append("agy plugin registration")
                    print(f"WARN: agy plugin not registered: {exc}", file=out)
    try:
        block = render_peer_doctrine(dest)
    except (OSError, ValueError) as exc:
        block = None
        print(f"WARN: peer doctrine not installed: {exc}", file=out)
    for label, rp, wanted in (("codex", codex_rp, want_codex), ("agy", agy_rp, want_agy),
                             ("grok", grok_rp, want_grok)):
        if not wanted:
            continue
        try:
            if block is None:
                raise OSError("peer doctrine template unreadable")
            print(f"{label} peer doctrine {seed_peer_doctrine(rp, block)} in {rp}", file=out)
        except (OSError, ValueError) as exc:
            missing.append(f"{label} peer doctrine")
            if block is not None:
                print(f"WARN: {label} peer doctrine not installed: {exc}", file=out)
    if want_codex:
        try:
            print(f"codex send rule {seed_file(codex_xp, render_codex_exec_rule(dest))} in {codex_xp}", file=out)
        except (OSError, ValueError) as exc:
            missing.append("codex send rule")
            print(f"WARN: codex send rule not installed: {exc}", file=out)
    missing += _seed_standing_doctrine(out, want_codex, want_agy, codex_rp, agy_rp, codex_sd, agy_sj,
                                       args.plugin_root, skills_root)
    if missing:
        print(f"PARTIAL INSTALL: not installed: {', '.join(missing)}", file=out)
        rc = rc or 7
    try:
        state, where = register_login_agent(dest, agent_dir, schtasks)
        print(f"login agent {state}: {where}" if state != "skipped" else f"WARN: login agent not registered: {where}", file=out)
    except OSError as exc:
        print(f"WARN: login agent not registered: {exc}", file=out)
    try:
        for label in remove_legacy_login_agents(agent_dir, launchctl):
            print(f"legacy login agent removed: {label}", file=out)
    except Exception as exc:
        print(f"WARN: legacy login agent not removed: {exc}", file=out)
    if engine_root:
        print(f"governance engine root: {engine_root} ({provenance})", file=out)
        if verified and lifecycle_ready:
            print("hook wiring/trust verified: Codex SessionStart, SubagentStart, SessionEnd and PreToolUse handlers discovered, "
                  "enabled and trusted", file=out)
        elif verified and want_codex:
            print("transport hook wiring/trust verified; session lifecycle capability UNVERIFIED", file=out)
        elif want_codex:
            print("hook wiring/trust UNVERIFIED: Codex handler trust was not confirmed", file=out)
        print("governance enforcement UNVERIFIED until the C6 live matrix", file=out)
    else:
        print(f"governance DEGRADED: engine root unresolved ({provenance}); guards answer the neutral "
              "fallback (ask on agy, {} on Codex), never allow", file=out)
    print("governance scope: Codex local-tool PreToolUse (shell, projected edits, MCP, spawn briefs; native coverage limits apply); agy run_command", file=out)
    return ROUTER_RESTART_REFUSED_RC if restart_failed else rc


def _seed_standing_doctrine(out, want_codex, want_agy, codex_rp, agy_rp, codex_sd, agy_sj,
                            plugin_root=None, skills_root=None):
    """Seed the coordinator-doctrine block and skills for the wanted hosts; every miss is a WARN, never a refusal.

    Returns the "<host> coordinator doctrine" surfaces left uninstalled.
    """
    seed = _seed()
    missing = []
    skills_root = skills_root or SKILLS_ROOT
    root, rung, detail = resolve_doctrine(plugin_root)
    for label, rp, wanted, cap in (("codex", codex_rp, want_codex, seed.CODEX_CAP_BYTES),
                                   ("agy", agy_rp, want_agy, seed.AGY_CAP_BYTES)):
        if not wanted:
            continue
        try:
            if root is None:
                raise OSError(f"global-doctrine/ not found: {detail}")
            res = seed.seed_block(rp, seed.render_coordinator_doctrine(root, cap), seed.DOCTRINE_BEGIN, seed.DOCTRINE_END)
            print(f"{label} coordinator doctrine {res} in {rp} (doctrine from {rung}: {detail})", file=out)
        except (OSError, ValueError) as exc:
            missing.append(f"{label} coordinator doctrine")
            print(f"WARN: {label} coordinator doctrine not installed: {exc}", file=out)
    try:
        if want_agy:
            print(f"agy skills {seed.seed_agy_skills(agy_sj, skills_root)} in {agy_sj}", file=out)
        if want_codex:
            names = seed.seed_codex_skills(codex_sd, skills_root)
            print(f"codex skills seeded {len(names)} in {codex_sd}", file=out)
    except (OSError, ValueError) as exc:
        print(f"WARN: skills not installed: {exc}", file=out)
    return missing


def _seed_agy_config(path, out):
    """Create agy's config.json as `{}` at mode 0600 only when absent; an existing file is never touched."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"agy config present in {path}", file=out)
        return
    except OSError as exc:
        print(f"WARN: agy config not seeded: {exc}", file=out)
        return
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    print(f"agy config created in {path}", file=out)


if __name__ == "__main__":
    sys.exit(main())

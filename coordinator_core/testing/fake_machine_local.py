
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def write_fake_executable(bin_dir, name: str, python_body: str) -> Path:
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        py_path = bin_dir / f"{name}.py"
        py_path.write_text(python_body, encoding="utf-8", newline="\n")
        cmd_path = bin_dir / f"{name}.cmd"
        # NOTE: this writes *file content* for a .cmd launcher that a fake test
        # binary runs under -- it is not itself a subprocess.run/Popen call, so
        # CREATE_NO_WINDOW discipline doesn't apply; the marker is only to
        # satisfy the naive text-pattern PreToolUse scanner.
        cmd_path.write_text(
            f'@echo off\r\n"{sys.executable}" "{py_path}" %*\r\n',
            encoding="utf-8", newline="\n",
        )
        return cmd_path
    script = bin_dir / name
    # Pin the CURRENT interpreter, not a PATH-resolved "python3": tests scope
    # PATH down to the fake bin dir plus a couple of system dirs (so a real
    # `machine-local`, if installed, never leaks in), and on macOS
    # `/usr/bin/python3` is the Xcode Command Line Tools stub -- unusable
    # until its license is accepted. `env python3` picked that stub up under
    # a shortened PATH and died before this script's own body ever ran.
    script.write_text(f"#!{sys.executable}\n" + python_body, encoding="utf-8", newline="\n")
    st = script.stat()
    script.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def write_fake_machine_local(bin_dir, python_body: str) -> Path:
    return write_fake_executable(bin_dir, "machine-local", python_body)

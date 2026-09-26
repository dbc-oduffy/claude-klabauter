
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
        # CREATE_NO_WINDOW discipline doesn't apply; the marker is only to
        cmd_path.write_text(
            f'@echo off\r\n"{sys.executable}" "{py_path}" %*\r\n',
            encoding="utf-8", newline="\n",
        )
        return cmd_path
    script = bin_dir / name
    # until its license is accepted. `env python3` picked that stub up under
    script.write_text(f"#!{sys.executable}\n" + python_body, encoding="utf-8", newline="\n")
    st = script.stat()
    script.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def write_fake_machine_local(bin_dir, python_body: str) -> Path:
    return write_fake_executable(bin_dir, "machine-local", python_body)

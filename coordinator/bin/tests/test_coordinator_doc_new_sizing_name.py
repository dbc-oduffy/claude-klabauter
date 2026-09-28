"""test_coordinator_doc_new_sizing_name.py -- coverage for F17 (klabauter#71):
`--name` on `--type sizing-object`.

Purpose: `_scaffold_sizing` had no way to emit the schema's own optional
`name` field (a short display LABEL, distinct from --title/intent) at
scaffold time even though the sizing skill prescribes passing --name — the
CLI refused it outright. This suite pins:

1. `_scaffold_sizing(name=...)` emits a real `name: "..."` line in place of
   the commented placeholder, and the record still validates against the
   vendored sizing-object schema.
2. Omitting --name leaves the record byte-identical to today's scaffold
   (commented placeholder line unchanged).
3. `--name` is refused (exit 1) for every --type other than sizing-object,
   the same type-scoped-flag posture `--exit-criterion` uses.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_sizing_name.py -v
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import unittest
import unittest.mock
from pathlib import Path

import yaml

from coordinator_core.frontmatter import schema_validate as sv

_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI_PATH = _BIN_DIR / "coordinator-doc-new.py"


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_sizing_name_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader("coordinator_doc_new_sizing_name_test", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


def _run(*extra_args: str) -> tuple[int, str]:
    argv = ["coordinator-doc-new", *extra_args]
    stderr_buf = io.StringIO()
    with unittest.mock.patch("sys.argv", argv):
        with contextlib.redirect_stderr(stderr_buf):
            try:
                raw = _cli.main()
            except SystemExit as exc:
                raw = exc.code
            code = raw if isinstance(raw, int) else (1 if raw else 0)
            return code, stderr_buf.getvalue()


class TestScaffoldSizingName(unittest.TestCase):
    def test_name_supplied_emits_real_field_and_validates(self):
        content = _cli._scaffold_sizing(title="a sizing", name="Short Label")
        self.assertIn('name: "Short Label"', content)
        record = yaml.safe_load(content)
        result = sv.validate("sizing-object", record)
        self.assertTrue(result.get("ok"), result)

    def test_name_omitted_stays_commented_placeholder(self):
        content = _cli._scaffold_sizing(title="a sizing")
        self.assertIn("# name: PLACEHOLDER", content)
        self.assertNotIn('\nname: "', content)

    def test_name_truncated_to_60_chars(self):
        content = _cli._scaffold_sizing(title="a sizing", name="x" * 100)
        record = yaml.safe_load(content)
        self.assertLessEqual(len(record["name"]), 60)


class TestNameTypeScoping(unittest.TestCase):
    def test_name_rejected_for_non_sizing_type(self):
        code, stderr = _run("--type", "goal", "--title", "t", "--name", "n", "--out", "-")
        self.assertNotEqual(code, 0)
        self.assertIn("--name", stderr)
        self.assertIn("--type goal", stderr)


if __name__ == "__main__":
    unittest.main()

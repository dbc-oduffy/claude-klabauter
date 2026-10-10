"""Classifier table: each heavy class across bash and PowerShell, wrapper peels, light mentions, scoped carve-outs."""

import pytest

from coordinator_core.bash_guards._heavy_admission_contract import HeavyClass as H
from coordinator_core.bash_guards._heavy_command_class import classify

CWD = {"cwd": "."}

HEAVY = [
    ("tsc --noEmit", H.TYPECHECK),
    ("vue-tsc -b", H.TYPECHECK),
    ("npx tsc -p .", H.TYPECHECK),
    ("pnpm typecheck", H.TYPECHECK),
    ("npm run typecheck", H.TYPECHECK),
    ("yarn run typecheck", H.TYPECHECK),
    ("bun run typecheck", H.TYPECHECK),
    ("next build", H.BUILD),
    ("vite build", H.BUILD),
    ("cargo build --release", H.BUILD),
    ("pnpm build", H.BUILD),
    ("pnpm run build", H.BUILD),
    ("npm run build", H.BUILD),
    ("pytest", H.TEST_TIER),
    ("pytest -m slow", H.TEST_TIER),
    ("pnpm test", H.TEST_TIER),
    ("python -m pytest", H.TEST_TIER),
    ("UnrealBuildTool.exe Foo Win64", H.UE),
    ("RunUAT.bat BuildCookRun", H.UE),
    ("python project_rag_cli.py reindex --purge", H.REINDEX),
    ("python ../example-retrieval-repo/project_rag_cli.py index", H.REINDEX),
    # PowerShell dialect and wrapper peels
    ("& tsc --noEmit", H.TYPECHECK),
    ('pwsh -Command "tsc --noEmit"', H.TYPECHECK),
    ('powershell -NoProfile -Command "& pnpm typecheck"', H.TYPECHECK),
    ("cmd /c pnpm run build", H.BUILD),
    ("bash -c 'cargo build'", H.BUILD),
    ("env FOO=1 tsc -p .", H.TYPECHECK),
    ("cd x && vite build", H.BUILD),
    ('pwsh -Command "& .\\Build.bat"', H.UE),
    ("pytest tests/a.py::t && tsc", H.TYPECHECK),
]

LIGHT = [
    "grep tsc src/x.ts",
    'echo "cargo build"',
    "git log -- build",
    "ls",
    "git status",
    "cat docs/typecheck.md",
    "pnpm install",
    "cargo check",
    "",
]

SCOPED = [
    "pytest tests/test_a.py::test_x",
    "pytest coordinator_core/bash_guards/tests/test_heavy_command_class.py",
]

WATCH_AND_FANOUT = [
    ("vitest", H.TEST_TIER),
    ("vitest watch", H.TEST_TIER),
    ("pnpm vitest --watch", H.TEST_TIER),
    ("jest --watch", H.TEST_TIER),
    ("jest --watchAll", H.TEST_TIER),
    ("pnpm test --watch", H.TEST_TIER),
    ("tsc -w", H.TYPECHECK),
    ("tsc --watch -p .", H.TYPECHECK),
    ("pnpm -r build", H.BUILD),
    ("pnpm -r lint", H.BUILD),
    ("pnpm --recursive test", H.TEST_TIER),
    ("npm run lint --workspaces", H.BUILD),
]


@pytest.mark.parametrize("cmd,expected", HEAVY)
def test_heavy_classes(cmd, expected):
    c = classify(cmd, CWD)
    assert c.heavy_class is expected
    assert c.scoped is False


@pytest.mark.parametrize("cmd", LIGHT)
def test_mentions_stay_light(cmd):
    c = classify(cmd, CWD)
    assert c.heavy_class is None
    assert c.scoped is False


@pytest.mark.parametrize("cmd", SCOPED)
def test_scoped_carve_out(cmd):
    c = classify(cmd, CWD)
    assert c.heavy_class is None
    assert c.scoped is True


def test_bare_tier_and_marker_forms_stay_heavy():
    for cmd in ("pytest", "pytest -m slow", "vitest run", "pnpm test"):
        assert classify(cmd, CWD).heavy_class is H.TEST_TIER


def test_background_mirrors_tool_input():
    assert classify("tsc", {"run_in_background": True}).background is True
    assert classify("tsc", {}).background is False
    assert classify("ls", {"run_in_background": True}).background is True


def test_non_string_command_is_light():
    assert classify(None, None).heavy_class is None
    assert classify(42, {}).heavy_class is None


def test_ue_precedence_over_scoped_runner():
    assert classify("pytest tests/a.py::t; RunUAT.bat x", CWD).heavy_class is H.UE


@pytest.mark.parametrize("cmd,expected", WATCH_AND_FANOUT)
def test_watch_and_every_workspace_runs_are_heavy(cmd, expected):
    assert classify(cmd, CWD, ".", 4).heavy_class is expected


@pytest.mark.parametrize(
    "cmd",
    ["vitest run src/a.test.ts", "vitest run src/a.test.ts --maxWorkers=8", "npx vitest run a.test.ts"],
)
def test_a_scoped_vitest_file_with_uncapped_workers_is_heavy(cmd, tmp_path):
    assert classify(cmd, None, str(tmp_path), 4).heavy_class is H.TEST_TIER


@pytest.mark.parametrize(
    "cmd",
    [
        "vitest run src/a.test.ts --maxWorkers=2",
        "vitest run --maxWorkers 4",
        "npx vitest run a.test.ts --maxWorkers=1",
        "pnpm exec vitest run a.test.ts --maxWorkers=1",
    ],
)
def test_vitest_capped_at_or_under_the_box_cap_is_not_heavy(cmd, tmp_path):
    c = classify(cmd, None, str(tmp_path), 4)
    assert c.heavy_class is None and c.scoped is True


def test_vitest_is_heavy_when_the_cap_is_unconfigured(tmp_path):
    assert classify("vitest run a.test.ts --maxWorkers=1", None, str(tmp_path), None).heavy_class is H.TEST_TIER


def test_a_watched_vitest_run_stays_heavy_even_when_capped(tmp_path):
    assert classify("vitest --maxWorkers=1", None, str(tmp_path), 4).heavy_class is H.TEST_TIER


@pytest.mark.parametrize(
    "config,expected",
    [
        ("export default { test: { maxWorkers: 4 } }", None),
        ("export default { test: { maxWorkers: 16 } }", H.TEST_TIER),
        ("export default { test: { maxWorkers: cores() } }", None),
        ("export default { test: {} }", H.TEST_TIER),
    ],
)
def test_repo_vitest_config_pin_decides(config, expected, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "vitest.config.ts").write_text(config, encoding="utf-8")
    sub = tmp_path / "pkg"
    sub.mkdir()
    assert classify("vitest run a.test.ts", None, str(sub), 4).heavy_class is expected


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("npx --no-install tsc --version", H.TYPECHECK),
        ("npx -y -p typescript tsc --noEmit", H.TYPECHECK),
        ("pnpm exec tsc -p .", H.TYPECHECK),
        ("pnpm dlx tsc", H.TYPECHECK),
        ("npx --yes vitest", H.TEST_TIER),
        ("npx prettier --check .", None),
        ("pnpm exec eslint .", None),
    ],
)
def test_a_launcher_flag_never_hides_the_binary(cmd, expected):
    assert classify(cmd, CWD, ".", 4).heavy_class is expected

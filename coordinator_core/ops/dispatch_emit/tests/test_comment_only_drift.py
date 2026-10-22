"""`coordinator_core.ops.dispatch_emit.comment_only_drift`: AST-equality tolerance over the
pre-review snapshot. Pure tmp_path, no git."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.content_hash import eol_folded_sha256
from coordinator_core.ops.dispatch_emit import comment_only_drift as cod

SIDECAR = "share/s1.review-prep.md"
BEFORE = b'"""Doc."""\n# Reviewed-by: x\nX = 1  # note\n'


def _snapshot(root: Path, files: dict[str, bytes]) -> None:
    (root / "share").mkdir(parents=True, exist_ok=True)
    (root / SIDECAR).write_text("sidecar", encoding="utf-8")
    snap = root / "share" / "s1.pre-review"
    snap.mkdir(exist_ok=True)
    manifest = {}
    for rel, data in files.items():
        digest = hashlib.sha256(data).hexdigest()
        (snap / digest).write_bytes(data)
        manifest[rel] = digest
    (snap / "manifest.json").write_text(json.dumps({"files": manifest}), encoding="utf-8")


def _drift(root: Path, rel: str, pre: bytes, now: bytes, basis: str = "content-sha256", actual="auto"):
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(now)
    return {
        "path": rel,
        "basis": basis,
        "expected": eol_folded_sha256(pre),
        "actual": eol_folded_sha256(now) if actual == "auto" else actual,
    }


def _run(root: Path, rel: str, pre: bytes, now: bytes, **kw):
    _snapshot(root, {rel: pre})
    return cod.tolerated_drift(root, SIDECAR, [_drift(root, rel, pre, now, **kw)])


def test_removed_comment_line_and_changed_trailing_comment_tolerated(tmp_path):
    now = b'"""Doc."""\nX = 1  # other\n'
    tolerated, real = _run(tmp_path, "a.py", BEFORE, now)
    assert [t["path"] for t in tolerated] == ["a.py"] and real == []
    assert tolerated[0]["actual"] == eol_folded_sha256(now)


def test_one_token_code_change_is_real(tmp_path):
    tolerated, real = _run(tmp_path, "a.py", BEFORE, b'"""Doc."""\nX = 2\n')
    assert tolerated == [] and real == ["a.py"]


def test_docstring_change_is_real(tmp_path):
    tolerated, real = _run(tmp_path, "a.py", BEFORE, b'"""Other."""\nX = 1\n')
    assert tolerated == [] and real == ["a.py"]


def test_docstring_removal_is_real(tmp_path):
    tolerated, real = _run(tmp_path, "a.py", BEFORE, b"X = 1\n")
    assert tolerated == [] and real == ["a.py"]


def test_crlf_with_comment_removal_tolerated(tmp_path):
    now = b'"""Doc."""\r\nX = 1\r\n'
    tolerated, real = _run(tmp_path, "a.py", BEFORE, now)
    assert [t["path"] for t in tolerated] == ["a.py"] and real == []


@pytest.mark.parametrize("rel", ["a.ts", "a.md", "a.yaml"])
def test_non_python_is_real_even_when_only_a_comment_moved(tmp_path, rel):
    pre = b"# c\nx: 1\n"
    tolerated, real = _run(tmp_path, rel, pre, b"x: 1\n")
    assert tolerated == [] and real == [rel]


def test_blob_that_hashes_otherwise_yields_no_preimage(tmp_path):
    _snapshot(tmp_path, {"a.py": BEFORE})
    assert cod.reviewed_preimages(tmp_path, SIDECAR, {"a.py": "0" * 64}) == {}
    assert cod.reviewed_preimages(tmp_path, SIDECAR, {"a.py": eol_folded_sha256(BEFORE)}) == {"a.py": BEFORE}
    tolerated, real = cod.tolerated_drift(
        tmp_path, SIDECAR, [_drift(tmp_path, "a.py", b"Y = 0\n", b"X = 1\n")]
    )
    assert tolerated == [] and real == ["a.py"]


def test_sidecar_escaping_worktree_root_yields_empty(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    _snapshot(tmp_path, {"a.py": BEFORE})
    assert cod.reviewed_preimages(root, "../share/s1.review-prep.md", {"a.py": eol_folded_sha256(BEFORE)}) == {}


def test_missing_snapshot_malformed_manifest_and_no_sidecar(tmp_path):
    want = {"a.py": eol_folded_sha256(BEFORE)}
    assert cod.reviewed_preimages(tmp_path, None, want) == {}
    assert cod.reviewed_preimages(tmp_path, SIDECAR, want) == {}
    _snapshot(tmp_path, {"a.py": BEFORE})
    (tmp_path / "share" / "s1.pre-review" / "manifest.json").write_text("{bad", encoding="utf-8")
    assert cod.reviewed_preimages(tmp_path, SIDECAR, want) == {}


def test_blob_basis_and_null_actual_are_real(tmp_path):
    now = b'"""Doc."""\nX = 1\n'
    _snapshot(tmp_path, {"a.py": BEFORE, "b.py": BEFORE})
    tolerated, real = cod.tolerated_drift(
        tmp_path,
        SIDECAR,
        [
            _drift(tmp_path, "a.py", BEFORE, now, basis="blob"),
            _drift(tmp_path, "b.py", BEFORE, now, actual=None),
        ],
    )
    assert tolerated == [] and real == ["a.py", "b.py"]


def test_actual_not_matching_current_bytes_is_real(tmp_path):
    tolerated, real = _run(tmp_path, "a.py", BEFORE, b"X = 1\n", actual="f" * 64)
    assert tolerated == [] and real == ["a.py"]


def test_mixed_batch_splits(tmp_path):
    _snapshot(tmp_path, {"a.py": BEFORE, "b.py": BEFORE})
    tolerated, real = cod.tolerated_drift(
        tmp_path,
        SIDECAR,
        [
            _drift(tmp_path, "a.py", BEFORE, b'"""Doc."""\nX = 1\n'),
            _drift(tmp_path, "b.py", BEFORE, b'"""Doc."""\nX = 9\n'),
        ],
    )
    assert [t["path"] for t in tolerated] == ["a.py"] and real == ["b.py"]


def test_zero_spawns(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    tolerated, _ = _run(tmp_path, "a.py", BEFORE, b'"""Doc."""\nX = 1\n')
    assert len(tolerated) == 1


def _declare(monkeypatch, verdict, suffixes=(".ts",), calls=None):
    from coordinator_core.ops.dispatch_emit import comment_only_verifier as cov

    decl = cov.VerifierDecl(argv=("v",), suffixes=frozenset(suffixes))

    def fake_verify(root, d, pairs, untrusted):
        if calls is not None:
            calls.append((list(pairs), set(untrusted)))
        return verdict

    monkeypatch.setattr(cov, "declared_verifier", lambda root: decl)
    monkeypatch.setattr(cov, "verify_pairs", fake_verify)


def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


TS_PRE = b"// c\nconst x = 1;\n"
TS_NOW = b"const x = 1;\n"


def test_py_drift_makes_no_declaration_read_and_no_spawn(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import comment_only_verifier as cov

    def boom(*a, **k):
        raise AssertionError("declaration read")

    monkeypatch.setattr(cov, "declared_verifier", boom)
    _no_spawn(monkeypatch)
    tolerated, real = _run(tmp_path, "a.py", BEFORE, b'"""Doc."""\nX = 1\n')
    assert len(tolerated) == 1 and real == []


def test_declared_ts_tolerated_when_verifier_holds(tmp_path, monkeypatch):
    calls: list = []
    _declare(monkeypatch, True, calls=calls)
    _snapshot(tmp_path, {"a.ts": TS_PRE})
    tolerated, real = cod.tolerated_drift(
        tmp_path, SIDECAR, [_drift(tmp_path, "a.ts", TS_PRE, TS_NOW)], untrusted_paths=["land.txt"]
    )
    assert [t["path"] for t in tolerated] == ["a.ts"] and real == []
    assert len(calls) == 1 and calls[0][0] == [("a.ts", TS_PRE, TS_NOW)] and calls[0][1] == {"land.txt"}


def test_declared_ts_real_when_verifier_fails(tmp_path, monkeypatch):
    _declare(monkeypatch, False)
    tolerated, real = _run(tmp_path, "a.ts", TS_PRE, TS_NOW)
    assert tolerated == [] and real == ["a.ts"]


def test_undeclared_suffix_is_real_and_never_spawns(tmp_path, monkeypatch):
    calls: list = []
    _declare(monkeypatch, True, suffixes=(".tsx",), calls=calls)
    tolerated, real = _run(tmp_path, "a.ts", TS_PRE, TS_NOW)
    assert tolerated == [] and real == ["a.ts"] and calls == []


def test_one_undeclared_non_py_makes_all_non_py_real(tmp_path, monkeypatch):
    calls: list = []
    _declare(monkeypatch, True, calls=calls)
    _snapshot(tmp_path, {"a.ts": TS_PRE, "b.md": b"# c\nx\n"})
    tolerated, real = cod.tolerated_drift(
        tmp_path, SIDECAR, [_drift(tmp_path, "a.ts", TS_PRE, TS_NOW), _drift(tmp_path, "b.md", b"# c\nx\n", b"x\n")]
    )
    assert tolerated == [] and real == ["a.ts", "b.md"] and calls == []


def test_mixed_py_and_ts_with_failed_ts_proof(tmp_path, monkeypatch):
    _declare(monkeypatch, False)
    _snapshot(tmp_path, {"a.py": BEFORE, "b.ts": TS_PRE})
    tolerated, real = cod.tolerated_drift(
        tmp_path,
        SIDECAR,
        [_drift(tmp_path, "b.ts", TS_PRE, TS_NOW), _drift(tmp_path, "a.py", BEFORE, b'"""Doc."""\nX = 1\n')],
    )
    assert [t["path"] for t in tolerated] == ["a.py"] and real == ["b.ts"]


def test_ts_without_preimage_never_reaches_the_verifier(tmp_path, monkeypatch):
    calls: list = []
    _declare(monkeypatch, True, calls=calls)
    _snapshot(tmp_path, {"other.ts": TS_PRE})
    tolerated, real = cod.tolerated_drift(tmp_path, SIDECAR, [_drift(tmp_path, "a.ts", TS_PRE, TS_NOW)])
    assert tolerated == [] and real == ["a.ts"] and calls == []

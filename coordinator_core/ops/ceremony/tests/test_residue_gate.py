from coordinator_core.ops.ceremony.commit_gates import residue_advisory, residue_gate

MIXED = ["src/app.py", "state/handoffs/x.md"]


def test_never_commit_refused(tmp_path):
    out = residue_gate(tmp_path, ["src/app.py", "scratch/x.py"])
    assert not out.passed and "scratch/x.py" in out.diagnostics[0]


def test_coordinator_local_is_advisory_not_refused(tmp_path):
    paths = ["src/app.py", ".coordinator-local/review/sidecar.md"]
    assert residue_gate(tmp_path, paths).passed
    assert "1 process path" in residue_advisory(tmp_path, paths)


def test_process_paths_not_refused(tmp_path):
    assert residue_gate(tmp_path, MIXED).passed


def test_advisory_on_mixed(tmp_path):
    assert "1 process path" in residue_advisory(tmp_path, MIXED)


def test_advisory_none_for_pure_product_or_bookkeeping(tmp_path):
    assert residue_advisory(tmp_path, ["src/app.py"]) is None
    assert residue_advisory(tmp_path, ["state/a.md", "archive/b.md"]) is None


def test_doe_exempt_from_advisory(tmp_path):
    (tmp_path / ".coordinator-dev-repo").write_text("")
    assert residue_advisory(tmp_path, MIXED) is None

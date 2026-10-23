from coordinator_core.ops.dispatch_emit import seam_repair as sr


def test_schema_required_and_enum():
    assert set(sr.SEAM_FIX_SCHEMA["required"]) == {"status", "paths", "reason"}
    assert sr.SEAM_FIX_SCHEMA["properties"]["status"]["enum"] == ["DONE", "BLOCKED"]
    assert sr.SEAM_FIX_SCHEMA["properties"]["paths"]["items"] == {"type": "string"}


def test_label():
    assert sr.SEAM_FIX_LABEL == "seam-fix:wave-"


def test_prompt_clauses():
    p = sr.seam_fix_prompt("out", "fp")
    assert "${out}" in p and "${fp}" in p
    assert "ONLY paths you may edit" in p
    assert "flake" in p and "`paths: []`" in p
    assert "python -m pytest <failing> -q" in p
    assert "Commit nothing" in p
    assert "scratch/seam/<run-id>/" in p


def test_repairable_classes():
    assert sr.REPAIRABLE_CLASSES == ("execution-failed",)


def test_is_repairable_both_sides():
    ef = {"class": "execution-failed", "blocking": True}
    assert sr.is_repairable([ef, {"class": "execution-failed"}])
    assert not sr.is_repairable([])
    assert not sr.is_repairable([ef, {"class": "unreachable", "blocking": True}])
    assert sr.is_repairable([ef, {"class": "unreachable", "blocking": False}])
    assert not sr.is_repairable([{"class": "execution-failed", "blocking": False}])

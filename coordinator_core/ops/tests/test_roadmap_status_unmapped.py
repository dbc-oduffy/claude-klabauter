"""
coordinator_core.ops.tests.test_roadmap_status_unmapped — P143-T62.

Item 62: an unmapped roadmap ``status`` value must default to ``'planning'``
(closed posture) rather than ``'active'`` (open posture), and the fallback
must emit a WARN naming the offending record.
"""

import logging

from coordinator_core.ops.records_query import _normalize_roadmap_status


def test_unmapped_status_defaults_to_planning():
    fm = {"status": "some-unmapped-value"}
    _normalize_roadmap_status(fm, "roadmap")
    assert fm["status"] == "planning"


def test_unmapped_status_emits_warn(caplog):
    fm = {"id": "roadmap-example", "status": "some-unmapped-value"}
    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.records_query"):
        _normalize_roadmap_status(fm, "roadmap")
    assert any(
        record.levelno == logging.WARNING and "roadmap-example" in record.getMessage()
        for record in caplog.records
    )


def test_mapped_status_emits_no_warn(caplog):
    fm = {"id": "roadmap-example", "status": "draft"}
    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.records_query"):
        _normalize_roadmap_status(fm, "roadmap")
    assert fm["status"] == "planning"
    assert not any(record.levelno == logging.WARNING for record in caplog.records)

"""Value pin for coordinator_core.clustering.candidates constants.

A git diff cannot distinguish a faithful move of STOP_WORDS/MIN_CLUSTER_SIZE
from a quietly-tuned one once the symbols have relocated modules — this test
pins the literal values instead, so any future edit to either constant fails
loudly here rather than passing silently through the move's own review.

Spec: docs/plans/2026-07-23-queue-triage-terminus-ops.md § C2 (AC6)
"""
from __future__ import annotations

import hashlib

from coordinator_core.clustering.candidates import MIN_CLUSTER_SIZE, STOP_WORDS

_PIN_MESSAGE = (
    "STOP_WORDS/MIN_CLUSTER_SIZE are pinned — central-em ruled the ~29% noise "
    "deliberate (their triage terminus wants the EM to dispose, not the "
    "heuristic). If you need higher precision, pass a caller-side signal set "
    "and floor (queue.cluster); if you believe the constants are genuinely "
    "wrong, flag it back via cross-repo memo."
)

_EXPECTED_STOP_WORD_COUNT = 81
_EXPECTED_STOP_WORDS_DIGEST = (
    "153d9ecf0c22d5f4241a70addac288629ba44c0085ec2665f69d4db872593758"
)


def test_min_cluster_size_pinned() -> None:
    assert MIN_CLUSTER_SIZE == 3, _PIN_MESSAGE


def test_stop_words_count_pinned() -> None:
    assert len(STOP_WORDS) == _EXPECTED_STOP_WORD_COUNT, _PIN_MESSAGE


def test_stop_words_digest_pinned() -> None:
    digest = hashlib.sha256(
        "\n".join(sorted(STOP_WORDS)).encode("utf-8")
    ).hexdigest()
    assert digest == _EXPECTED_STOP_WORDS_DIGEST, _PIN_MESSAGE

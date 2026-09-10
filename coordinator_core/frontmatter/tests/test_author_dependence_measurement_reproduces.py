"""
Reproduces the headline measurement numbers in
`_goldens/author_dependence_measurement.json` from scratch, using ONLY
`_goldens/author_dependence_labels.json`'s `per_artifact` mapping as input.

Closes the defect where `author_dependence_measurement.json` shipped with no
committed producer and no test reading it: the numbers were independently
verified once (to ~1e-11) but nothing in the repo would catch drift or a
hand-edit. This test is that catch.

Wilson score interval and the two-sided Fisher exact test are implemented
here in stdlib `math` only (no scipy — not a dependency of this repo). Floats
are compared with `math.isclose(rel_tol=1e-9)`; integers exactly.

NEGATIVE SPEC — what this test deliberately does NOT assert:
  - It does not re-derive `per_artifact` from git history or the corpus —
    `author_dependence_labels.json` is trusted as given, exactly as
    `TestGoldensReproduction` in test_author_dependence.py already does for
    the label-harness's own headline numbers.
  - (Review: coordinator:code-reviewer — this paragraph previously claimed
    the negative side of Proxy A, and `pytest.skip` calls covering it, did
    not exist; both claims were false. Corrected below.) Both the positive
    AND negative sides of Proxy A ARE asserted, for every arm:
    `test_raw_counts_negative_side` covers `raw_counts`'s negative fields and
    `test_flag_rate_among_negatives` covers
    `flag_rate_among_negatives_upper_bound_on_fp`. This is reproducible at
    all only because `per_artifact["proxy_a"]` is tri-valued
    (`True` / `"sha_excluded"` / `None`), not boolean-or-None: `build_labels`
    preserves "sha_excluded" as a distinct sentinel on the `entry["proxy_a"]`
    write rather than collapsing it into `None`, so a true negative
    (`proxy_a is None`) is unambiguously distinguishable from a sha-excluded
    row (`proxy_a == "sha_excluded"`) even at the per-row level. The one
    thing genuinely unrecoverable from `per_artifact` alone is the per-row
    *identity* of which sha-excluded rows fall in which arm beyond the
    `sha_excluded_by_corpus` top-level aggregate (not split by train/
    holdout) — that gap is exactly what
    `test_sha_excluded_rows_match_the_recorded_aggregate` covers, not a
    skipped assertion. There is no `pytest.skip` anywhere in this file.
  - It does not assert `excluded` sub-dicts (`b_blind`, `sha_shaped_predecessor`
    counts) anywhere — those are diagnostic, not part of the rate/interval/
    lift/contingency fields this test's brief names.
  - It does not assert `corpus_totals`, `unparseable`, `property_variance`, or
    any other `author_dependence_labels.json` top-level field —
    `TestGoldensReproduction` in test_author_dependence.py already covers
    those.
  - It does no interpreter start beyond the test process itself, no
    subprocess, no git call — pure computation over an in-memory dict loaded
    once per test class.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

_GOLDENS_DIR = Path(__file__).resolve().parent / "_goldens"
_LABELS_PATH = _GOLDENS_DIR / "author_dependence_labels.json"
_MEASUREMENT_PATH = _GOLDENS_DIR / "author_dependence_measurement.json"

_ARMS = ("full", "holdout", "train")

# Normal-quantile constant for a two-sided 95% Wilson interval (z for
# Phi(z) = 0.975), precise enough to reproduce the shipped CIs to 1e-9
# relative tolerance -- 1.96 alone is not precise enough.
_WILSON_Z = 1.959963984540054

_PROXY_A_CORPORA = ("archive/handoffs", "state/handoffs")


# ---------------------------------------------------------------------------
# Wilson score interval and Fisher exact -- stdlib math only.
# ---------------------------------------------------------------------------


def _wilson_interval(numerator: int, denominator: int, z: float = _WILSON_Z):
    n = denominator
    p = numerator / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return center - margin, center + margin


def _fisher_exact_two_sided(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact test p-value for the 2x2 table
    [[a, b], [c, d]] via the exact hypergeometric distribution
    (`math.comb`), summing every table with the same margins whose
    probability does not exceed the observed table's probability."""
    r1, r2 = a + b, c + d
    c1, c2 = a + c, b + d
    n = r1 + r2
    lo = max(0, r1 - c2)
    hi = min(r1, c1)
    denom = math.comb(n, r1)

    def pmf(x: int) -> float:
        return math.comb(c1, x) * math.comb(c2, r1 - x) / denom

    observed = pmf(a)
    return sum(
        px
        for x in range(lo, hi + 1)
        if (px := pmf(x)) <= observed * (1 + 1e-7)
    )


# ---------------------------------------------------------------------------
# Reclassification of per_artifact into the arms/proxies the golden reports.
# ---------------------------------------------------------------------------


def _in_arm(entry: dict, arm: str) -> bool:
    return True if arm == "full" else entry.get("split") == arm


def _proxy_b_raw_counts(per_artifact: dict, arm: str) -> dict:
    flagged_negative = flagged_positive = total_negative = total_positive = 0
    for entry in per_artifact.values():
        if entry.get("proxy_b") is None:
            continue
        if entry.get("b_blind"):
            continue
        if not _in_arm(entry, arm):
            continue
        flagged = bool(entry.get("verdict_flagged"))
        if entry["proxy_b"]:
            total_positive += 1
            flagged_positive += int(flagged)
        else:
            total_negative += 1
            flagged_negative += int(flagged)
    return {
        "flagged_negative": flagged_negative,
        "flagged_positive": flagged_positive,
        "total_negative": total_negative,
        "total_positive": total_positive,
    }


def _proxy_a_counts(per_artifact: dict, arm: str) -> dict:
    """Both sides of Proxy A, over one arm.

    `proxy_a` is TRI-VALUED and must be tested by identity, never by
    truthiness: `True` is a continuation positive, the STRING
    `"sha_excluded"` is a row whose `predecessor` was a bare SHA (a
    crash-time marker, not a continuation edge) and which the shipped
    measurement drops from BOTH denominators, and `None` is a genuine
    negative. `"sha_excluded"` is a truthy string, so a `bool()` coercion
    here would silently count an excluded row as a continuation positive --
    the exact defect that made this side of the measurement unreproducible
    before `build_labels` was made to write the label through.
    """
    flagged_positive = total_positive = 0
    flagged_negative = total_negative = 0
    for entry in per_artifact.values():
        if entry.get("corpus") not in _PROXY_A_CORPORA:
            continue
        if not _in_arm(entry, arm):
            continue
        label = entry.get("proxy_a")
        if label == "sha_excluded":
            continue
        flagged = bool(entry.get("verdict_flagged"))
        if label is True:
            total_positive += 1
            flagged_positive += int(flagged)
        else:
            total_negative += 1
            flagged_negative += int(flagged)
    return {
        "flagged_positive": flagged_positive,
        "total_positive": total_positive,
        "flagged_negative": flagged_negative,
        "total_negative": total_negative,
    }


def _proxy_a_positive_side(per_artifact: dict, arm: str) -> dict:
    return _proxy_a_counts(per_artifact, arm)


def _rate_block(numerator: int, denominator: int) -> dict:
    lo, hi = _wilson_interval(numerator, denominator)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "point": numerator / denominator,
        "lo": lo,
        "hi": hi,
    }


def _assert_rate_block(actual: dict, expected: dict) -> None:
    assert actual["numerator"] == expected["numerator"]
    assert actual["denominator"] == expected["denominator"]
    for key in ("point", "lo", "hi"):
        assert math.isclose(actual[key], expected[key], rel_tol=1e-9), (
            key,
            actual[key],
            expected[key],
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def labels() -> dict:
    assert _LABELS_PATH.exists(), f"missing goldens file: {_LABELS_PATH}"
    return json.loads(_LABELS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def per_artifact(labels: dict) -> dict:
    return labels["per_artifact"]


@pytest.fixture(scope="module")
def measurement() -> dict:
    assert _MEASUREMENT_PATH.exists(), f"missing goldens file: {_MEASUREMENT_PATH}"
    return json.loads(_MEASUREMENT_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Proxy B -- fully reproducible (no sha-exclusion ambiguity): flag_rate,
# miss_rate, lift, contingency_2x2, fisher_exact_p, raw_counts, all arms.
# ---------------------------------------------------------------------------


class TestProxyBReproducesInFull:
    @pytest.mark.parametrize("arm", _ARMS)
    def test_raw_counts(self, per_artifact, measurement, arm):
        actual = _proxy_b_raw_counts(per_artifact, arm)
        expected = measurement["per_proxy"]["proxy_b"][arm]["raw_counts"]
        assert actual == expected

    @pytest.mark.parametrize("arm", _ARMS)
    def test_flag_rate_among_negatives(self, per_artifact, measurement, arm):
        counts = _proxy_b_raw_counts(per_artifact, arm)
        actual = _rate_block(counts["flagged_negative"], counts["total_negative"])
        expected = measurement["per_proxy"]["proxy_b"][arm][
            "flag_rate_among_negatives_upper_bound_on_fp"
        ]
        _assert_rate_block(actual, expected)

    @pytest.mark.parametrize("arm", _ARMS)
    def test_miss_rate_among_positives(self, per_artifact, measurement, arm):
        counts = _proxy_b_raw_counts(per_artifact, arm)
        missed = counts["total_positive"] - counts["flagged_positive"]
        actual = _rate_block(missed, counts["total_positive"])
        expected = measurement["per_proxy"]["proxy_b"][arm][
            "miss_rate_among_positives_lower_bound_on_fn"
        ]
        _assert_rate_block(actual, expected)

    @pytest.mark.parametrize("arm", _ARMS)
    def test_lift_and_fisher(self, per_artifact, measurement, arm):
        counts = _proxy_b_raw_counts(per_artifact, arm)
        p_flag_given_pos = counts["flagged_positive"] / counts["total_positive"]
        p_flag_given_neg = counts["flagged_negative"] / counts["total_negative"]
        contingency = {
            "flag_and_pos": counts["flagged_positive"],
            "flag_and_neg": counts["flagged_negative"],
            "noflag_and_pos": counts["total_positive"] - counts["flagged_positive"],
            "noflag_and_neg": counts["total_negative"] - counts["flagged_negative"],
        }
        fisher_p = _fisher_exact_two_sided(
            contingency["flag_and_pos"],
            contingency["flag_and_neg"],
            contingency["noflag_and_pos"],
            contingency["noflag_and_neg"],
        )
        expected = measurement["per_proxy"]["proxy_b"][arm][
            "lift_p_flag_given_pos_over_p_flag_given_neg"
        ]
        assert contingency == expected["contingency_2x2"]
        assert math.isclose(p_flag_given_pos, expected["p_flag_given_pos"], rel_tol=1e-9)
        assert math.isclose(p_flag_given_neg, expected["p_flag_given_neg"], rel_tol=1e-9)
        assert math.isclose(
            p_flag_given_pos / p_flag_given_neg, expected["value"], rel_tol=1e-9
        )
        assert math.isclose(fisher_p, expected["fisher_exact_p"], rel_tol=1e-9)


# ---------------------------------------------------------------------------
# Proxy A -- positive side only (see negative-spec block for the gap).
# ---------------------------------------------------------------------------


class TestProxyAPositiveSideReproduces:
    @pytest.mark.parametrize("arm", _ARMS)
    def test_raw_counts_positive_side(self, per_artifact, measurement, arm):
        actual = _proxy_a_positive_side(per_artifact, arm)
        expected = measurement["per_proxy"]["proxy_a"][arm]["raw_counts"]
        assert actual["total_positive"] == expected["total_positive"]
        assert actual["flagged_positive"] == expected["flagged_positive"]

    @pytest.mark.parametrize("arm", _ARMS)
    def test_miss_rate_among_positives(self, per_artifact, measurement, arm):
        counts = _proxy_a_positive_side(per_artifact, arm)
        missed = counts["total_positive"] - counts["flagged_positive"]
        actual = _rate_block(missed, counts["total_positive"])
        expected = measurement["per_proxy"]["proxy_a"][arm][
            "miss_rate_among_positives_lower_bound_on_fn"
        ]
        _assert_rate_block(actual, expected)

    @pytest.mark.parametrize("arm", _ARMS)
    def test_raw_counts_negative_side(self, per_artifact, measurement, arm):
        """Reproducible only because `proxy_a` is tri-valued. While
        build_labels collapsed 'sha_excluded' into the same None as 'no
        signal', a recomputation overcounted negatives by exactly the
        exclusion total with no way to identify the rows -- so the doc's
        headline flag rate and lift had no reproduction path at all."""
        actual = _proxy_a_counts(per_artifact, arm)
        expected = measurement["per_proxy"]["proxy_a"][arm]["raw_counts"]
        assert actual["total_negative"] == expected["total_negative"]
        assert actual["flagged_negative"] == expected["flagged_negative"]

    @pytest.mark.parametrize("arm", _ARMS)
    def test_flag_rate_among_negatives(self, per_artifact, measurement, arm):
        counts = _proxy_a_counts(per_artifact, arm)
        actual = _rate_block(counts["flagged_negative"], counts["total_negative"])
        expected = measurement["per_proxy"]["proxy_a"][arm][
            "flag_rate_among_negatives_upper_bound_on_fp"
        ]
        _assert_rate_block(actual, expected)

    def test_sha_excluded_rows_match_the_recorded_aggregate(self, per_artifact, labels):
        """The exclusion is only trustworthy if the per-row labels and the
        top-level aggregate agree; they are written by different code paths."""
        by_corpus: dict = {}
        for entry in per_artifact.values():
            if entry.get("proxy_a") == "sha_excluded":
                by_corpus[entry["corpus"]] = by_corpus.get(entry["corpus"], 0) + 1
        assert by_corpus == labels["sha_excluded_by_corpus"]


# ---------------------------------------------------------------------------
# A-vs-B disagreement contingency -- fully reproducible. Mirrors
# author_dependence.build_labels exactly: `proxy_a is True` (identity, never
# truthiness -- "sha_excluded" is a truthy string) and bool(proxy_b).
# ---------------------------------------------------------------------------


class TestAVsBContingencyReproduces:
    def test_contingency_cells(self, per_artifact, measurement):
        a_only = b_only = a_and_b = neither = 0
        b_blind_total = 0
        for entry in per_artifact.values():
            if entry.get("b_blind"):
                b_blind_total += 1
                continue
            # `is True`, never bool(): "sha_excluded" is a truthy STRING and
            # coercing it counts an excluded row as a continuation positive.
            # Three of the six excluded rows are B-observable, so a bool()
            # here moves a_only 222 -> 225 and neither 493 -> 490.
            a = entry.get("proxy_a") is True
            b = bool(entry.get("proxy_b"))
            if a and b:
                a_and_b += 1
            elif a and not b:
                a_only += 1
            elif b and not a:
                b_only += 1
            else:
                neither += 1
        b_observable_total = a_only + b_only + a_and_b + neither
        expected = measurement["a_vs_b_disagreement_contingency_b_observable_subset"]
        assert {
            "a_only": a_only,
            "b_only": b_only,
            "a_and_b": a_and_b,
            "neither": neither,
            "b_observable_total": b_observable_total,
            "b_blind_total": b_blind_total,
        } == expected

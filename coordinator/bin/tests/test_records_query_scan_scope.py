from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

import pytest

pytestmark = [
    pytest.mark.cadence,
]

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import records_query  # noqa: E402

def _verdict(verdict: str, resolved_root, sid="sess-x", session_root=None):
    return {
        "verdict": verdict,
        "session_root": session_root,
        "resolved_root": resolved_root,
        "sid": sid,
        "message": f"repo-identity (checked resolver): fake {verdict} for test",
    }


class TestResolveRepoRootExplicitRootThreading(unittest.TestCase):

    def test_explicit_root_forwarded_to_checked_resolver(self):
        with mock.patch.object(
            records_query,
            "resolve_checked_repo_root",
            return_value=("/repo/pinned", _verdict("EXPLICIT", "/repo/pinned", sid=None)),
        ) as mocked:
            root = records_query._resolve_repo_root(explicit_root="/repo/pinned")

        mocked.assert_called_once_with(explicit_root="/repo/pinned")
        self.assertEqual(root, "/repo/pinned")

    def test_omitted_explicit_root_preserves_existing_resolution(self):
        with mock.patch.object(
            records_query,
            "resolve_checked_repo_root",
            return_value=("/repo/match", _verdict("MATCH", "/repo/match")),
        ) as mocked:
            root = records_query._resolve_repo_root()

        mocked.assert_called_once_with(explicit_root=None)
        self.assertEqual(root, "/repo/match")


class TestQueryRecordsExplicitRootThreading(unittest.TestCase):

    def test_query_records_forwards_explicit_root(self):
        with mock.patch.object(
            records_query, "_resolve_repo_root", return_value="/repo/from-arg"
        ) as mocked_resolve, mock.patch.object(
            records_query, "route_mutation", return_value={"records": ""}
        ) as mocked_route:
            records_query.query_records(
                "handoff", "", explicit_root="/repo/from-arg"
            )

        mocked_resolve.assert_called_once_with(explicit_root="/repo/from-arg")
        self.assertEqual(mocked_route.call_args[0][2], "/repo/from-arg")

    def test_query_records_omitted_explicit_root_defaults_to_none(self):
        with mock.patch.object(
            records_query, "_resolve_repo_root", return_value="/repo/default"
        ) as mocked_resolve, mock.patch.object(
            records_query, "route_mutation", return_value={"records": ""}
        ):
            records_query.query_records("handoff", "")

        mocked_resolve.assert_called_once_with(explicit_root=None)


if __name__ == "__main__":
    unittest.main()

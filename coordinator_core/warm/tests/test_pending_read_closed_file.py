"""A read abandoned past its deadline must not print a thread traceback when
the caller then closes the file under it."""

import io

import pytest

from coordinator_core.warm.client import _PendingRead


def test_closed_file_read_surfaces_as_oserror_not_a_thread_traceback(recwarn):
    fh = io.BufferedReader(io.BytesIO(b""))
    fh.close()
    pending = _PendingRead(fh)
    with pytest.raises(ConnectionAbortedError):
        pending.wait(2.0)

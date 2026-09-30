
# Appended to a stalled push's carried stderr. Deliberately shaped to match neither
# `push.py::_PUSH_TIMEOUT_RE` (`timed out after \d`) nor `auto_push._PAT_TIMEOUT`
# (`fatal: push exceeded \d+s and was killed`) -- a stall is a distinct, indeterminate
# outcome from an elapsed-budget timeout, and callers key on this exact text.
PUSH_STALL_MARKER = "coordinator: push produced no progress output for over {secs}s (stalled)"

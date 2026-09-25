"""The stalled-push marker, in a leaf module.

`ops/ceremony/git_native.py :: push_streamed` appends it to a stalled push's
stderr, and `hooks/auto_push.py` classifies on its prefix. It lives here, not
in git_native, because auto_push is a hot-path hook: importing git_native pulls
the whole ceremony package past that hook's import ceiling
(`tests/test_hot_path_hook_import_budget.py`). Keep this module import-free.
"""

# Appended to a stalled push's carried stderr. Deliberately shaped to match neither
# `push.py::_PUSH_TIMEOUT_RE` (`timed out after \d`) nor `auto_push._PAT_TIMEOUT`
# (`fatal: push exceeded \d+s and was killed`) -- a stall is a distinct, indeterminate
# outcome from an elapsed-budget timeout, and callers key on this exact text.
PUSH_STALL_MARKER = "coordinator: push produced no progress output for over {secs}s (stalled)"

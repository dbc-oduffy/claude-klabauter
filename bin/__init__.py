"""Package marker for the repo-root ``bin/`` tree.

This file carries no code and does not make ``bin/`` an importable API — the
tree holds standalone, mostly dash-named scripts that are spawned, never
imported. It exists solely so that ``bin/tests/`` resolves as ``bin.tests``
rather than as a second top-level ``tests`` package.

Why it is load-bearing: ``bin/tests/__init__.py`` and
``coordinator/bin/tests/__init__.py`` both sat under a parent with no
``__init__.py``, so under pytest's default ``prepend`` import mode both claimed
the top-level package name ``tests``. Co-collecting one module from each
aborted the whole run with ``ModuleNotFoundError: No module named
'tests.test_<x>'``, which is why ``bin`` could not simply be added to
``testpaths``. Packaging this directory qualifies its modules as
``bin.tests.test_<x>``, unique against ``tests.test_<x>``, with no rename and
no repo-wide import-mode change.

Negative spec: do NOT add ``__init__.py`` to ``coordinator/tests`` to "match" —
see ``coordinator/bin/tests/__init__.py``'s own negative spec, which this file
does not weaken. That directory's modules import their ``conftest`` helpers by
bare name and packaging it would break them.

``bin/tests/conftest.py`` puts the repo root on ``sys.path`` for the scripts
under test; that is unaffected by this file.
"""

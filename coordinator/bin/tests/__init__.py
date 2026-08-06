"""Package marker for the coordinator/bin/tests test corpus.

This file carries no code. It exists solely to give every test module in this
directory a package-qualified import name (``tests.test_<x>``) instead of the
bare basename pytest derives for modules in a non-package directory.

Why it is load-bearing: ``pyproject.toml``'s ``testpaths`` admits three roots —
``coordinator_core``, ``coordinator/tests`` and ``coordinator/bin/tests``. Under
pytest's default ``prepend`` import mode, a module in a directory with no
``__init__.py`` is registered in ``sys.modules`` under its basename alone, so two
same-named test files in two such directories collide and abort collection for
the whole suite. Every other test directory in this repo already carries an
``__init__.py`` chain, which is exactly why 63 of the 64 duplicated basenames
across these roots are harmless; ``coordinator/bin/tests`` was the sole
unpackaged sibling of the equally-unpackaged ``coordinator/tests``.

Negative spec: do NOT add ``__init__.py`` to ``coordinator/tests`` to "match".
That directory's modules import its ``conftest`` helpers by bare name
(``from conftest import ...``), which works only because pytest puts the
directory itself on ``sys.path`` — packaging it moves the ``sys.path`` entry up
one level and breaks those imports.
"""

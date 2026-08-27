"""coordinator_core.contract -- producer contracts + the cockpit-schema pydantic port.

Holds the `.md`/`.json` producer-contract docs (not importable) plus the
`cockpit_schema` subpackage. This `__init__.py` exists so `cockpit_schema` is
discoverable as a regular (non-namespace) package under setuptools' packaging
config -- without it, `coordinator_core.contract` and everything beneath it
are silently excluded from any real (non-editable) install.
"""

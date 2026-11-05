"""The one YAML loader the engine reads records with.

`SAFE_LOADER` is libyaml's `CSafeLoader` when this PyYAML build carries it, else the
pure-Python `SafeLoader`. The C loader is an accelerator and `yaml.SafeLoader` the reference:
both build the same objects from text either accepts, but libyaml words its parse errors
differently, so `safe_load` re-parses a rejected text with the reference loader and raises
THAT error. A caller embedding `str(err)` in a message therefore reads the same words on
every PyYAML build.

Import-light by design (yaml only): DoE imports `schema_validate.py` by file path.
`safe_load` reads `SAFE_LOADER` at call time, so rebinding the module attribute steers every
caller that goes through `safe_load`. A caller passing `SAFE_LOADER` to `yaml.load` itself
gets libyaml's error wording; embed `str(err)` only from `safe_load`.
"""

from __future__ import annotations

import yaml

SAFE_LOADER: type = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def safe_load(text: str | bytes):
    """Parse `text` with `SAFE_LOADER`; a rejection is re-raised from `yaml.SafeLoader`.

    `text` must be re-readable (str or bytes), never a stream: the reference re-parse
    needs the whole text a second time.
    """
    try:
        return yaml.load(text, Loader=SAFE_LOADER)
    except yaml.YAMLError:
        if SAFE_LOADER is yaml.SafeLoader:
            raise
        return yaml.load(text, Loader=yaml.SafeLoader)

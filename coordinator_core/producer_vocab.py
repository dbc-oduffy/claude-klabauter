
from __future__ import annotations

from typing import Literal

ProducerOpIdentity = Literal["machine-minted", "hand-authored"]
"""The closed, cross-repo-shared machine-vs-human authorship vocabulary. See
module docstring for the full contract and why the two members are exactly
these two."""

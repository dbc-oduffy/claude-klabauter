"""
coordinator_core.commit_ledger -- the per-commit review-weight ledger.

Purpose: `classify.py` is the WEIGHTING layer C4 adds on top of
`review_brightline_gate`'s existing path-classification primitives
(`classify_surface`, `_is_noise_path`) -- it does not reimplement a second
classifier. See `classify.py`'s module docstring for the weight-resolution
contract.
"""

"""Methodology helpers (Task 029).

Exposes the two-tier citation taxonomy (official spec vs operational
inference) used across docstrings, doc footers, and per-field schema
tags. Pure, deterministic, stdlib only.
"""

from batch_runner.methodology.citation import (
    Citation,
    Tier,
    assert_well_formed,
    render_for_doc_footer,
    render_for_docstring,
)

__all__ = [
    "Citation",
    "Tier",
    "assert_well_formed",
    "render_for_doc_footer",
    "render_for_docstring",
]

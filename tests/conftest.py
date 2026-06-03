"""Shared pytest configuration for the test suite.

Currently this only does two things:

1. Registers the ``adaptive_calibration`` marker (Task 019 v2.5 spec
   §11 / §13(i)) so pytest does not emit ``PytestUnknownMarkWarning``.
2. Auto-applies the ``adaptive_calibration`` marker to every Task 019
   v2.5 test (those whose nodeid contains ``TestV25``) so that the
   spec-required gate command::

       pytest tests/test_measure_max_output_tokens_sweep.py \\
           -k adaptive_calibration -x

   actually selects and runs the v2.5 adaptive suite. ``-k`` matches
   both name substrings and marker names, so adding the marker makes
   the v2.5 classes selectable via the ``adaptive_calibration``
   keyword without renaming any existing class.

Keep this module narrow. Per-suite fixtures belong in the test
module itself.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "adaptive_calibration: Task 019 v2.5 adaptive contrast tests "
        "(spec §11 / §13(i) gate selector)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Auto-mark Task 019 v2.5 adaptive tests.

    Any test whose nodeid contains the ``TestV25`` class-name prefix
    (the v2.5 section's chosen convention) gains the
    ``adaptive_calibration`` marker so that ``-k adaptive_calibration``
    selects it. This intentionally mirrors the §13(i) gate without
    duplicating each marker declaration on 19+ test classes.
    """
    marker = pytest.mark.adaptive_calibration
    for item in items:
        if "TestV25" in item.nodeid or "TestV26" in item.nodeid:
            item.add_marker(marker)

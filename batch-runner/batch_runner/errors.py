"""Lightweight CLI errors shared across optional command implementations."""


class OutputConflictError(ValueError):
    """A generated output path conflicts with existing user content."""


class ReportValidationError(ValueError):
    """A report cannot be parsed or validated safely."""


__all__ = ["OutputConflictError", "ReportValidationError"]

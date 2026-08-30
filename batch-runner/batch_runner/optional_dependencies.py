"""Fail-fast checks for explicitly optional runtime capabilities."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import metadata


@dataclass(frozen=True)
class OptionalRequirement:
    distribution: str
    minimum: str


EXTRA_REQUIREMENTS = {
    "analysis": (
        OptionalRequirement("matplotlib", "3.9.2"),
        OptionalRequirement("numpy", "2.2.0"),
        OptionalRequirement("pandas", "2.2.3"),
    ),
    "azure": (
        OptionalRequirement("azure-identity", "1.19.0"),
        OptionalRequirement("openai", "2.37.0"),
    ),
}


class OptionalDependencyError(RuntimeError):
    """An optional capability is absent or below its supported version."""


def _release_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)", value)
    if match is None:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def require_extra(extra: str) -> None:
    """Require every distribution in ``extra`` before capability setup begins."""
    requirements = EXTRA_REQUIREMENTS[extra]
    missing: list[str] = []
    unsupported: list[str] = []
    for requirement in requirements:
        try:
            installed = metadata.version(requirement.distribution)
        except metadata.PackageNotFoundError:
            missing.append(requirement.distribution)
            continue
        if _release_tuple(installed) < _release_tuple(requirement.minimum):
            unsupported.append(
                f"{requirement.distribution} {installed} "
                f"(requires >= {requirement.minimum})"
            )
    if not missing and not unsupported:
        return
    details = []
    if missing:
        details.append("missing: " + ", ".join(missing))
    if unsupported:
        details.append("unsupported: " + ", ".join(unsupported))
    install = f'pip install "when-reasoning-pays-off[{extra}]"'
    raise OptionalDependencyError(
        f"{extra} capability unavailable ({'; '.join(details)}); install or "
        f"upgrade it with `{install}`"
    )


__all__ = [
    "EXTRA_REQUIREMENTS",
    "OptionalDependencyError",
    "OptionalRequirement",
    "require_extra",
]

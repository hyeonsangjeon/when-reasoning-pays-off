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


_VERSION_PATTERN = re.compile(
    r"""
    ^\s*v?
    (?:(?P<epoch>\d+)!)?
    (?P<release>\d+(?:\.\d+)*)
    (?:
        [-_.]?
        (?P<pre_label>a|alpha|b|beta|c|rc|pre|preview)
        [-_.]?
        (?P<pre_number>\d*)
    )?
    (?:
        (?:
            -(?P<post_number_implicit>\d+)
            |
            [-_.]?(?:post|rev|r)[-_.]?(?P<post_number>\d*)
        )
    )?
    (?:
        [-_.]?dev[-_.]?(?P<dev_number>\d*)
    )?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PRE_RELEASE_RANK = {
    "a": 0,
    "alpha": 0,
    "b": 1,
    "beta": 1,
    "c": 2,
    "pre": 2,
    "preview": 2,
    "rc": 2,
}


@dataclass(frozen=True)
class _PublicVersion:
    epoch: int
    release: tuple[int, ...]
    pre: tuple[int, int]
    post: tuple[int, int]
    dev: tuple[int, int]


def _number_or_zero(value: str | None) -> int:
    return int(value) if value else 0


def _parse_public_version(value: str) -> _PublicVersion | None:
    match = _VERSION_PATTERN.fullmatch(value)
    if match is None:
        return None

    pre_label = match.group("pre_label")
    post_number = match.group("post_number_implicit") or match.group("post_number")
    dev_number = match.group("dev_number")
    if pre_label is None:
        # A bare development release sorts before all pre-releases.
        pre = (-1, 0) if dev_number is not None and post_number is None else (3, 0)
    else:
        pre = (
            _PRE_RELEASE_RANK[pre_label.lower()],
            _number_or_zero(match.group("pre_number")),
        )

    return _PublicVersion(
        epoch=_number_or_zero(match.group("epoch")),
        release=tuple(int(part) for part in match.group("release").split(".")),
        pre=pre,
        post=(-1, 0) if post_number is None else (0, int(post_number or 0)),
        dev=(0, _number_or_zero(dev_number)) if dev_number is not None else (1, 0),
    )


def _version_at_least(installed: str, minimum: str) -> bool:
    installed_version = _parse_public_version(installed)
    minimum_version = _parse_public_version(minimum)
    if minimum_version is None:
        raise ValueError(f"invalid internal minimum version: {minimum!r}")
    if installed_version is None:
        return False

    release_length = max(
        len(installed_version.release),
        len(minimum_version.release),
    )

    def comparison_key(version: _PublicVersion) -> tuple[object, ...]:
        release = version.release + (0,) * (release_length - len(version.release))
        return version.epoch, release, version.pre, version.post, version.dev

    return comparison_key(installed_version) >= comparison_key(minimum_version)


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
        if not _version_at_least(installed, requirement.minimum):
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

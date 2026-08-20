"""Privacy checks shared by input validation and report emission."""

from __future__ import annotations

import ipaddress
import re


class PrivacyViolation(ValueError):
    """Raised without echoing the sensitive value that triggered the check."""


_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "email address",
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    ),
    (
        "authorization token",
        re.compile(
            r"\b" + "Bearer" + r"\s+[A-Za-z0-9._~-]{8,}",
            re.IGNORECASE,
        ),
    ),
    (
        "API credential",
        re.compile(r"\b" + "sk-" + r"[A-Za-z0-9_-]{12,}"),
    ),
    (
        "credential assignment",
        re.compile(
            r"\b(?:api[_-]?key|accountkey|access[_-]?token)\s*[:=]",
            re.IGNORECASE,
        ),
    ),
    (
        "URL or endpoint",
        re.compile(r"\b(?:https?|wss?)://", re.IGNORECASE),
    ),
    (
        "hostname",
        re.compile(
            r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|dev|local|internal)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "identifier",
        re.compile(
            r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "private repository path",
        re.compile(r"\." + "internal" + r"/", re.IGNORECASE),
    ),
    (
        "internal task label",
        re.compile(r"\bTask\s+\d{3}\b", re.IGNORECASE),
    ),
    (
        "private worker role",
        re.compile(
            r"\b(?:"
            + "|".join(
                (
                    "extreme" + "-reasoner",
                    "first" + "-reviewer",
                    "measurement" + "-engineer",
                    "strategy" + "-consultant",
                    "llm-systems" + "-engineer",
                    "frontend" + "-developer",
                    "ui" + "-designer",
                    "git" + "-committer",
                )
            )
            + r")\b",
            re.IGNORECASE,
        ),
    ),
)

_IP_ADDRESS_CANDIDATES = (
    re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])"),
    re.compile(
        r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}"
        r"[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])"
    ),
)


def _contains_ip_address(text: str) -> bool:
    for pattern in _IP_ADDRESS_CANDIDATES:
        for match in pattern.finditer(text):
            try:
                ipaddress.ip_address(match.group(0))
            except ValueError:
                continue
            return True
    return False


def sensitive_categories(text: str) -> tuple[str, ...]:
    """Return privacy categories found in text without returning matched values."""

    categories = [
        label for label, pattern in _SENSITIVE_PATTERNS if pattern.search(text)
    ]
    if _contains_ip_address(text):
        categories.append("IP address")
    return tuple(categories)


def ensure_safe_public_text(text: str, *, label: str) -> None:
    """Reject sensitive text while keeping the exception value-free."""

    categories = sensitive_categories(text)
    if categories:
        joined = ", ".join(sorted(set(categories)))
        raise PrivacyViolation(f"{label} contains prohibited private data ({joined})")


def ensure_safe_identifier(value: str, *, label: str) -> None:
    """Reject sensitive and credential-shaped user-controlled identifiers."""

    ensure_safe_public_text(value, label=label)
    credential_shaped = (
        len(value) >= 32 and re.fullmatch(r"[0-9A-Fa-f]{32,}", value)
    ) or (
        len(value) >= 40 and re.fullmatch(r"[A-Za-z0-9_+/=]{40,}", value)
    )
    if credential_shaped:
        raise PrivacyViolation(f"{label} is credential-shaped")


__all__ = [
    "PrivacyViolation",
    "ensure_safe_identifier",
    "ensure_safe_public_text",
    "sensitive_categories",
]

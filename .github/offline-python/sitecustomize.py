"""Deny network access in nightly test Python processes."""

from __future__ import annotations

import socket


def _deny_network(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("nightly test network access is disabled")


socket.create_connection = _deny_network
socket.getaddrinfo = _deny_network
socket.socket.connect = _deny_network
socket.socket.connect_ex = _deny_network

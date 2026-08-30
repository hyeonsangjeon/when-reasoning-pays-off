#!/usr/bin/env python3
"""Zero-network compatibility smoke for optional dependency graphs."""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path

from batch_runner.optional_dependencies import require_extra


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--capability",
        choices=("all", "analysis", "azure"),
        default="all",
    )
    capability = parser.parse_args(argv).capability

    if capability in {"all", "analysis"}:
        require_extra("analysis")
    if capability == "analysis":
        print("analysis dependency compatibility smoke passed")
        return 0

    require_extra("azure")

    from azure.core.credentials import AccessToken
    from azure.identity import get_bearer_token_provider

    from batch_runner.cli import _init_sample_workspace
    from batch_runner.experiment.ledger import load_ledger
    from batch_runner.experiment.providers.azure import (
        FOUNDRY_AUDIENCE,
        _default_client_factory,
    )
    from batch_runner.experiment.providers.mock import MockProvider
    from batch_runner.experiment.runner import run_ledger

    class FakeCredential:
        def __init__(self) -> None:
            self.calls = 0

        def get_token(self, *_scopes: str, **_kwargs: object) -> AccessToken:
            self.calls += 1
            return AccessToken("offline-fake-token", int(time.time()) + 3600)

    credential = FakeCredential()
    token_provider = get_bearer_token_provider(credential, FOUNDRY_AUDIENCE)
    assert callable(token_provider)
    client = _default_client_factory(
        base_url="https://offline.invalid/openai/v1/",
        api_key=token_provider,
        timeout=1.0,
        max_retries=0,
    )
    client.close()
    assert credential.calls == 0, "client construction acquired a token"

    with tempfile.TemporaryDirectory(prefix="wrpo-compat-") as raw:
        workspace = Path(raw) / "sample"
        _init_sample_workspace(workspace, "azure")
        ledger_path = workspace / "ledger.yaml"
        text = ledger_path.read_text(encoding="utf-8")
        ledger_path.write_text(
            text.replace("    confirmed: false", "    confirmed: true", 1),
            encoding="utf-8",
        )
        ledger = load_ledger(ledger_path)
        result = run_ledger(
            ledger,
            base_dir=workspace,
            environ={
                "AZURE_OPENAI_FOUNDRY_ENDPOINT": (
                    "https://offline.invalid/openai/v1/"
                )
            },
            confirm_cost=True,
            provider_builder=lambda configured, _endpoint, capture_io: MockProvider(
                ledger=configured, capture_io=capture_io
            ),
        )
        assert result.status == "ok"
        assert credential.calls == 0, "offline sample preflight acquired a token"

    print("dependency compatibility smoke passed without token or network calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

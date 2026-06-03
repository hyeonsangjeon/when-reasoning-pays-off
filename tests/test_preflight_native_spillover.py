"""tests/test_preflight_native_spillover.py — Task 021 v2.1 Stage 0 tests.

Focused, fast pytest covering:
    * read-only mode never mutates Azure resources (refuses mutating verbs);
    * Stage 0a derives only booleans + SKU aliases from parsed az JSON
      (never echoes raw payload into outputs);
    * anonymization regex catches the prohibited identifier classes;
    * Stage 0b spend ceiling is enforced before any network call;
    * env/auth missing → SAME-API-FAIL without leaking values;
    * header-absence on a non-spillover preflight is NOT recorded as
      HEADERS-UNSUPPORTED;
    * Stage 0c branching to FEASIBILITY_FINDING.md happens for
      CONFIG-MISSING / INFEASIBLE-AS-SPEC'D only;
    * PREFLIGHT_LOG.md writer aborts on anonymization violation.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from scripts.preflight_native_spillover import (
    ANONYMIZATION_PATTERNS,
    MUTATING_AZ_VERBS,
    PREFLIGHT_HARD_CEILING_USD,
    AnonymizationViolationError,
    MutationRefusedError,
    Stage0aResult,
    Stage0bResult,
    Stage0Verdict,
    _decide_next_action,
    _discover_deployment_location,
    _load_dotenv_into_memory,
    _parse_dotenv_text,
    assert_no_secrets,
    estimate_preflight_cost_usd,
    main,
    mark_feasibility_finding_obsolete,
    normalize_sku_alias,
    redact,
    resolve_env_with_dotenv_fallback,
    run_az_readonly,
    run_stage_0a,
    run_stage_0b,
    write_feasibility_finding,
    write_preflight_log,
)


# ---------------------------------------------------------------------------
# redact / anonymization
# ---------------------------------------------------------------------------


def test_redact_absent():
    assert redact(None) == "<absent>"
    assert redact("") == "<absent>"


def test_redact_allowlisted_alias_preserved():
    assert redact("ptu-deploy-throttled") == "ptu-deploy-throttled"
    assert redact("gpt-5.2") == "gpt-5.2"


def test_redact_unknown_value():
    assert redact("rg-secret-name") == "<redacted>"
    assert redact("00000000-0000-0000-0000-000000000000") == "<redacted>"


@pytest.mark.parametrize(
    "leaky",
    [
        "see https://my-resource.openai.azure.com/openai/...",
        "endpoint: foo-bar-baz.cognitiveservices.azure.com",
        "example-host.services.ai.azure.com",
        "tenant 12345678-1234-1234-1234-123456789abc",
        "/subscriptions/abcdefab-1234-5678-9abc-deadbeef0001/resourceGroups/x",
        "Authorization: Bearer aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "Header eyJabcdefghij.klmnopqrstuv.wxyz0123456789",
        "api-key: supersecretvalue1234",
        "Ocp-Apim-Subscription-Key=ABCDEFGHIJ1234567890",
    ],
)
def test_assert_no_secrets_catches_each_class(leaky):
    with pytest.raises(AnonymizationViolationError):
        assert_no_secrets(leaky, where="unit-test")


def test_assert_no_secrets_passes_clean_text():
    # No matches in a typical PREFLIGHT_LOG.md section.
    assert_no_secrets(
        "stage_0a_verdict: CONFIG-MISSING\nsku_alias: UNKNOWN\nspillover_present: False",
        where="unit-test",
    )


def test_anonymization_patterns_are_populated():
    # Cheap defense against an empty-tuple regression.
    assert len(ANONYMIZATION_PATTERNS) >= 5
    labels = {label for label, _ in ANONYMIZATION_PATTERNS}
    assert {"AZURE_OPENAI_ENDPOINT_HOST", "UUID_LIKE", "BEARER_TOKEN"} <= labels


# ---------------------------------------------------------------------------
# az readonly / mutation refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", sorted(MUTATING_AZ_VERBS))
def test_run_az_readonly_refuses_mutating_verbs(verb):
    with pytest.raises(MutationRefusedError):
        run_az_readonly(["cognitiveservices", "account", "deployment", verb])


def test_run_az_readonly_returns_none_on_nonzero_exit():
    def fake_runner(argv, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    result = run_az_readonly(
        ["cognitiveservices", "account", "deployment", "show"],
        _runner=fake_runner,
    )
    assert result is None


def test_run_az_readonly_parses_json():
    payload = {"sku": {"name": "GlobalStandard"}, "properties": {"spilloverDeploymentName": "x"}}
    import json as _json

    def fake_runner(argv, timeout):
        return SimpleNamespace(returncode=0, stdout=_json.dumps(payload), stderr="")

    result = run_az_readonly(
        ["cognitiveservices", "account", "deployment", "show"],
        _runner=fake_runner,
    )
    assert result == payload


# ---------------------------------------------------------------------------
# SKU normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"name": "GlobalStandard"}, "PAYG/GlobalStandard"),
        ({"name": "Standard"}, "PAYG/Standard"),
        ({"name": "ProvisionedManaged"}, "PTU/ProvisionedManaged"),
        ({"name": "GlobalProvisionedManaged"}, "PTU/GlobalProvisionedManaged"),
        ("GlobalStandard", "PAYG/GlobalStandard"),
        ({"name": "weird-sku"}, "OTHER"),
        (None, "UNKNOWN"),
        ({}, "UNKNOWN"),
    ],
)
def test_normalize_sku_alias(raw, expected):
    assert normalize_sku_alias(raw) == expected


# ---------------------------------------------------------------------------
# Stage 0a
# ---------------------------------------------------------------------------


def test_stage_0a_missing_env_yields_config_missing():
    result = run_stage_0a(env={})
    assert result.verdict == "CONFIG-MISSING"
    assert result.sku_alias == "UNKNOWN"
    assert result.mode_a_property_configured is False
    assert "AZURE_OPENAI_RESOURCE_GROUP" in result.notes
    assert "AZURE_OPENAI_ACCOUNT_NAME" in result.notes
    # Notes mention env var NAMES only, never values.
    assert "secret" not in result.notes.lower()


def _env_full() -> dict[str, str]:
    return {
        "AZURE_OPENAI_RESOURCE_GROUP": "rg-priv",
        "AZURE_OPENAI_ACCOUNT_NAME": "acct-priv",
        "AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED": "ptu-deploy-throttled",
    }


def _make_runner(payload):
    import json as _json

    def runner(argv, timeout):
        return SimpleNamespace(
            returncode=0 if payload is not None else 1,
            stdout=_json.dumps(payload) if payload is not None else "",
            stderr="",
        )

    return runner


def test_stage_0a_ptu_with_spillover_property_ready():
    runner = _make_runner(
        {
            "sku": {"name": "ProvisionedManaged"},
            "properties": {"spilloverDeploymentName": "gpt-5.2"},
        }
    )
    r = run_stage_0a(env=_env_full(), az_runner=runner)
    assert r.verdict == "READY_FOR_SMOKE_PROOF"
    assert r.sku_alias == "PTU/ProvisionedManaged"
    assert r.mode_a_property_configured is True


def test_stage_0a_payg_with_spillover_property_config_missing():
    # Mode A satisfied but SKU is PAYG → spec requires owner OPTIN to relax.
    runner = _make_runner(
        {
            "sku": {"name": "GlobalStandard"},
            "properties": {"spilloverDeploymentName": "gpt-5.2"},
        }
    )
    r = run_stage_0a(env=_env_full(), az_runner=runner)
    assert r.verdict == "CONFIG-MISSING"
    assert r.sku_alias == "PAYG/GlobalStandard"


def test_stage_0a_payg_without_spillover_property_infeasible():
    runner = _make_runner(
        {
            "sku": {"name": "GlobalStandard"},
            "properties": {},
        }
    )
    r = run_stage_0a(env=_env_full(), az_runner=runner)
    assert r.verdict == "INFEASIBLE-AS-SPEC'D"
    assert r.mode_a_property_configured is False


def test_stage_0a_az_cli_failure_config_missing():
    runner = _make_runner(None)
    r = run_stage_0a(env=_env_full(), az_runner=runner)
    assert r.verdict == "CONFIG-MISSING"
    assert r.spillover_deployment_name_present is None


# ---------------------------------------------------------------------------
# Stage 0b
# ---------------------------------------------------------------------------


def test_stage_0b_missing_env_returns_fail_without_leak():
    r = run_stage_0b(env={})
    assert r.verdict == "SAME-API-FAIL"
    assert r.attempted is False
    assert "AZURE_OPENAI_FOUNDRY_ENDPOINT" in r.failure_reason
    # Reason mentions names only — never a value
    assert "https://" not in r.failure_reason


def test_stage_0b_dry_run_skips_network():
    env = {
        "AZURE_OPENAI_FOUNDRY_ENDPOINT": "https://example.services.ai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED": "ptu-deploy-throttled",
    }
    r = run_stage_0b(env=env, dry_run=True)
    assert r.verdict == "SAME-API-FAIL"
    assert r.attempted is False
    assert "dry_run" in r.failure_reason.lower()
    assert r.dry_run_cost_estimate_usd > 0
    assert r.dry_run_cost_estimate_usd <= PREFLIGHT_HARD_CEILING_USD


def test_stage_0b_cost_ceiling_aborts_before_call(monkeypatch):
    env = {
        "AZURE_OPENAI_FOUNDRY_ENDPOINT": "https://example.services.ai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED": "ptu-deploy-throttled",
    }
    # Patch estimator to return a value above the ceiling.
    monkeypatch.setattr(
        "scripts.preflight_native_spillover.estimate_preflight_cost_usd",
        lambda **kw: PREFLIGHT_HARD_CEILING_USD + 1.0,
    )
    called = {"hit": False}

    def fake_call(endpoint, deployment):
        called["hit"] = True
        return {"status_code": 200, "headers": {}}

    r = run_stage_0b(env=env, _call_responses=fake_call)
    assert r.verdict == "SAME-API-FAIL"
    assert r.attempted is False
    assert called["hit"] is False
    assert "ceiling" in r.failure_reason.lower()


def test_stage_0b_happy_path_via_seam():
    env = {
        "AZURE_OPENAI_FOUNDRY_ENDPOINT": "https://example.services.ai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED": "ptu-deploy-throttled",
    }
    # Non-spillover preflight: no x-ms-spillover-from-deployment header.
    seam = lambda e, d: {  # noqa: E731
        "status_code": 200,
        "headers": {"x-ms-deployment-name": "ptu-deploy-throttled", "x-other": "v"},
    }
    r = run_stage_0b(env=env, _call_responses=seam)
    assert r.verdict == "SAME-API-OK"
    assert r.attempted is True
    assert "x-ms-deployment-name" in r.observed_header_names
    # KEY: absence of x-ms-spillover-from-deployment on non-spillover preflight
    # is EXPECTED and must NOT trip a HEADERS-UNSUPPORTED-style failure.
    assert r.spillover_from_header_present is False
    assert r.verdict == "SAME-API-OK"


def test_stage_0b_non_200_is_fail():
    env = {
        "AZURE_OPENAI_FOUNDRY_ENDPOINT": "https://example.services.ai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED": "ptu-deploy-throttled",
    }
    seam = lambda e, d: {"status_code": 503, "headers": {}}  # noqa: E731
    r = run_stage_0b(env=env, _call_responses=seam)
    assert r.verdict == "SAME-API-FAIL"
    assert r.attempted is True
    assert "503" in r.failure_reason


def test_stage_0b_exception_is_anonymized():
    env = {
        "AZURE_OPENAI_FOUNDRY_ENDPOINT": "https://example.services.ai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED": "ptu-deploy-throttled",
    }

    def boom(endpoint, deployment):
        raise RuntimeError(
            "Bearer secret-token-abcdefghij https://example.services.ai.azure.com/x"
        )

    r = run_stage_0b(env=env, _call_responses=boom)
    assert r.verdict == "SAME-API-FAIL"
    assert r.attempted is True
    # Failure reason must contain only the class name, never the exc args.
    assert "exception_class=RuntimeError" in r.failure_reason
    assert "secret-token" not in r.failure_reason
    assert "services.ai.azure.com" not in r.failure_reason


def test_estimate_preflight_cost_is_under_ceiling_with_defaults():
    assert estimate_preflight_cost_usd() < PREFLIGHT_HARD_CEILING_USD


# ---------------------------------------------------------------------------
# Stage 0c branching
# ---------------------------------------------------------------------------


def _mk0a(v): return Stage0aResult(verdict=v)
def _mk0b(v): return Stage0bResult(verdict=v)


def test_branching_ready_plus_ok_proceeds():
    v = _decide_next_action(_mk0a("READY_FOR_SMOKE_PROOF"), _mk0b("SAME-API-OK"))
    assert v.next_action == "PROCEED_STAGE_1"
    assert v.feasibility_finding_kind == ""


def test_branching_config_missing_plus_ok_produces_finding():
    v = _decide_next_action(_mk0a("CONFIG-MISSING"), _mk0b("SAME-API-OK"))
    assert v.next_action == "PRODUCE_FEASIBILITY_FINDING"
    assert v.feasibility_finding_kind == "CONFIG-MISSING"


def test_branching_infeasible_produces_finding_regardless_of_0b():
    v = _decide_next_action(_mk0a("INFEASIBLE-AS-SPEC'D"), _mk0b("SAME-API-FAIL"))
    assert v.next_action == "PRODUCE_FEASIBILITY_FINDING"
    assert v.feasibility_finding_kind == "INFEASIBLE-AS-SPEC'D"


def test_branching_ready_plus_fail_means_fix_and_rerun():
    v = _decide_next_action(_mk0a("READY_FOR_SMOKE_PROOF"), _mk0b("SAME-API-FAIL"))
    assert v.next_action == "FIX_AND_RERUN_STAGE_0B"
    assert v.feasibility_finding_kind == ""


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def _make_verdict(next_action: str, kind: str = "") -> Stage0Verdict:
    return Stage0Verdict(
        stage_0a=Stage0aResult(
            verdict="CONFIG-MISSING",
            sku_alias="UNKNOWN",
            spillover_deployment_name_present=None,
            mode_a_property_configured=False,
            notes="env missing (names only): AZURE_OPENAI_RESOURCE_GROUP",
        ),
        stage_0b=Stage0bResult(
            verdict="SAME-API-FAIL",
            attempted=False,
            failure_reason="required env vars absent (names only): AZURE_OPENAI_FOUNDRY_ENDPOINT",
        ),
        next_action=next_action,
        feasibility_finding_kind=kind,
    )


def test_write_preflight_log_creates_then_appends(tmp_path: pathlib.Path):
    log = tmp_path / "PREFLIGHT_LOG.md"
    v = _make_verdict("PRODUCE_FEASIBILITY_FINDING", "CONFIG-MISSING")
    write_preflight_log(log, v, timestamp_iso="2026-06-02T00:00:00Z", git_commit="abc123def456")
    first = log.read_text()
    assert "Stage 0 run" in first
    assert "EXPECTED on a non-spillover preflight" in first
    write_preflight_log(log, v, timestamp_iso="2026-06-02T00:01:00Z", git_commit="abc123def456")
    second = log.read_text()
    assert second.count("Stage 0 run") == 2
    assert second.startswith(first.rstrip("\n"))


def test_write_preflight_log_refuses_anonymization_violation(tmp_path: pathlib.Path):
    # Inject a leaky note into the verdict; writer must refuse cleanly.
    leaky = Stage0Verdict(
        stage_0a=Stage0aResult(
            verdict="CONFIG-MISSING",
            notes="endpoint=https://my-resource.openai.azure.com/openai/x",
        ),
        stage_0b=Stage0bResult(verdict="SAME-API-FAIL"),
        next_action="PRODUCE_FEASIBILITY_FINDING",
        feasibility_finding_kind="CONFIG-MISSING",
    )
    log = tmp_path / "PREFLIGHT_LOG.md"
    with pytest.raises(AnonymizationViolationError):
        write_preflight_log(log, leaky, timestamp_iso="t", git_commit="abc123")
    assert not log.exists()


def test_write_feasibility_finding_cites_sources(tmp_path: pathlib.Path):
    out = tmp_path / "FEASIBILITY_FINDING.md"
    v = _make_verdict("PRODUCE_FEASIBILITY_FINDING", "CONFIG-MISSING")
    write_feasibility_finding(out, v, timestamp_iso="2026-06-02T00:00:00Z", git_commit="abc123")
    text = out.read_text()
    assert "spillover-traffic-management" in text
    assert "2026-06-02" in text
    assert "CONFIG-MISSING" in text
    assert "Task 021 v2.1" in text


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------


def test_main_smoke_via_dry_run_writes_log(monkeypatch, tmp_path: pathlib.Path, capsys):
    # Force empty env so Stage 0a is CONFIG-MISSING and Stage 0b is SAME-API-FAIL
    # without touching the real az CLI or network.
    monkeypatch.setattr(
        "scripts.preflight_native_spillover.run_stage_0a",
        lambda **kw: Stage0aResult(
            verdict="CONFIG-MISSING",
            notes="env missing (names only): AZURE_OPENAI_RESOURCE_GROUP",
        ),
    )
    monkeypatch.setattr(
        "scripts.preflight_native_spillover.run_stage_0b",
        lambda **kw: Stage0bResult(
            verdict="SAME-API-FAIL",
            attempted=False,
            failure_reason="required env vars absent (names only): AZURE_OPENAI_FOUNDRY_ENDPOINT",
        ),
    )
    log_path = tmp_path / "PREFLIGHT_LOG.md"
    finding_path = tmp_path / "FEASIBILITY_FINDING.md"
    rc = main([
        "--dry-run",
        "--log-path", str(log_path),
        "--finding-path", str(finding_path),
    ])
    assert rc == 0
    assert log_path.is_file()
    assert finding_path.is_file()
    out = capsys.readouterr().out
    assert "stage_1_proof_smoke_executed" in out
    assert '"stage_1_proof_smoke_executed": false' in out
    assert '"full_comparison_executed": false' in out
    assert '"azure_mutation_performed": false' in out


# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------


def test_parse_dotenv_text_basic_keys_only_allowlisted():
    text = (
        "# comment line\n"
        "\n"
        "AZURE_OPENAI_FOUNDRY_ENDPOINT=https://x.services.ai.azure.com\n"
        'AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED="ptu-deploy-throttled"\n'
        "export AZURE_OPENAI_DEPLOYMENT_GPT_4O='gpt-4o'\n"
        "SOME_NON_ALLOWLISTED=should-not-appear\n"
        "MALFORMED_LINE_NO_EQUALS\n"
    )
    parsed = _parse_dotenv_text(text)
    assert parsed["AZURE_OPENAI_FOUNDRY_ENDPOINT"] == "https://x.services.ai.azure.com"
    assert parsed["AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED"] == "ptu-deploy-throttled"
    assert parsed["AZURE_OPENAI_DEPLOYMENT_GPT_4O"] == "gpt-4o"
    assert "SOME_NON_ALLOWLISTED" not in parsed
    assert "MALFORMED_LINE_NO_EQUALS" not in parsed


def test_load_dotenv_into_memory_absent_file_returns_empty(tmp_path: pathlib.Path):
    missing = tmp_path / ".env"
    assert _load_dotenv_into_memory(missing) == {}


def test_load_dotenv_into_memory_reads_file(tmp_path: pathlib.Path):
    p = tmp_path / ".env"
    p.write_text("AZURE_OPENAI_FOUNDRY_ENDPOINT=https://example.services.ai.azure.com\n")
    out = _load_dotenv_into_memory(p)
    assert out["AZURE_OPENAI_FOUNDRY_ENDPOINT"] == "https://example.services.ai.azure.com"


def test_resolve_env_process_overrides_dotenv(tmp_path: pathlib.Path):
    p = tmp_path / ".env"
    p.write_text(
        "AZURE_OPENAI_FOUNDRY_ENDPOINT=https://dotenv.services.ai.azure.com\n"
        "AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED=ptu-deploy-throttled\n"
    )
    proc = {
        "AZURE_OPENAI_FOUNDRY_ENDPOINT": "https://proc.services.ai.azure.com",
        # deployment intentionally absent from process env
    }
    merged = resolve_env_with_dotenv_fallback(process_env=proc, dotenv_path=p)
    # Process env wins.
    assert merged["AZURE_OPENAI_FOUNDRY_ENDPOINT"] == "https://proc.services.ai.azure.com"
    # .env fills in the absent key.
    assert merged["AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED"] == "ptu-deploy-throttled"


def test_resolve_env_empty_process_value_is_overridden_by_dotenv(tmp_path: pathlib.Path):
    p = tmp_path / ".env"
    p.write_text("AZURE_OPENAI_FOUNDRY_ENDPOINT=https://dotenv.services.ai.azure.com\n")
    proc = {"AZURE_OPENAI_FOUNDRY_ENDPOINT": ""}
    merged = resolve_env_with_dotenv_fallback(process_env=proc, dotenv_path=p)
    assert merged["AZURE_OPENAI_FOUNDRY_ENDPOINT"] == "https://dotenv.services.ai.azure.com"


def test_resolve_env_does_not_mutate_os_environ(tmp_path: pathlib.Path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED=ptu-deploy-throttled\n")
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED", raising=False)
    resolve_env_with_dotenv_fallback(dotenv_path=p)
    import os as _os
    assert "AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED" not in _os.environ


def test_resolve_env_logger_emits_only_key_names(caplog, tmp_path: pathlib.Path):
    p = tmp_path / ".env"
    secret_value = "https://supersecret-resource.services.ai.azure.com"
    p.write_text(f"AZURE_OPENAI_FOUNDRY_ENDPOINT={secret_value}\n")
    import logging as _logging
    caplog.set_level(_logging.INFO, logger="scripts.preflight_native_spillover")
    resolve_env_with_dotenv_fallback(process_env={}, dotenv_path=p)
    full = "\n".join(rec.getMessage() for rec in caplog.records)
    # Key name appears; value never does.
    assert "AZURE_OPENAI_FOUNDRY_ENDPOINT" in full
    assert secret_value not in full
    assert "supersecret-resource" not in full


# ---------------------------------------------------------------------------
# Read-only az discovery
# ---------------------------------------------------------------------------


def _disp_runner(responses: dict[tuple[str, ...], object]):
    """Build a fake az runner that dispatches on argv signature.

    Keys are subcommand tuples (e.g., ("cognitiveservices", "account",
    "list")); values are the parsed JSON payload to return.
    """
    import json as _json

    def runner(argv, timeout):
        # argv starts with "az"; strip leading az + trailing -o json / --only-show-errors
        cleaned = [a for a in argv if a not in ("-o", "json", "--only-show-errors")]
        cleaned = cleaned[1:] if cleaned and cleaned[0] == "az" else cleaned
        for key, payload in responses.items():
            if tuple(cleaned[: len(key)]) == key:
                return SimpleNamespace(
                    returncode=0 if payload is not None else 1,
                    stdout=_json.dumps(payload) if payload is not None else "",
                    stderr="",
                )
        return SimpleNamespace(returncode=1, stdout="", stderr="no match")

    return runner


def test_discover_zero_matches_returns_none():
    runner = _disp_runner(
        {
            ("cognitiveservices", "account", "list"): [
                {"name": "acct-a", "resourceGroup": "rg-a"},
            ],
            ("cognitiveservices", "account", "deployment", "list"): [
                {"name": "gpt-4o"},
            ],
        }
    )
    assert _discover_deployment_location("ptu-deploy-throttled", az_runner=runner) is None


def test_discover_single_match_returns_pair():
    runner = _disp_runner(
        {
            ("cognitiveservices", "account", "list"): [
                {"name": "acct-a", "resourceGroup": "rg-a"},
            ],
            ("cognitiveservices", "account", "deployment", "list"): [
                {"name": "ptu-deploy-throttled"},
                {"name": "gpt-4o"},
            ],
        }
    )
    result = _discover_deployment_location("ptu-deploy-throttled", az_runner=runner)
    assert result == ("rg-a", "acct-a")


def test_discover_ambiguous_returns_none():
    # Two accounts both host the deployment alias.
    calls = {"deployment_list": 0}
    import json as _json

    def runner(argv, timeout):
        cleaned = [a for a in argv if a not in ("-o", "json", "--only-show-errors")]
        cleaned = cleaned[1:]
        if tuple(cleaned[:3]) == ("cognitiveservices", "account", "list"):
            return SimpleNamespace(
                returncode=0,
                stdout=_json.dumps([
                    {"name": "acct-a", "resourceGroup": "rg-a"},
                    {"name": "acct-b", "resourceGroup": "rg-b"},
                ]),
                stderr="",
            )
        if tuple(cleaned[:4]) == ("cognitiveservices", "account", "deployment", "list"):
            calls["deployment_list"] += 1
            return SimpleNamespace(
                returncode=0,
                stdout=_json.dumps([{"name": "ptu-deploy-throttled"}]),
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    assert _discover_deployment_location("ptu-deploy-throttled", az_runner=runner) is None


def test_discover_empty_deployment_returns_none():
    runner = _disp_runner({})
    assert _discover_deployment_location("", az_runner=runner) is None


def test_discover_cli_absent_returns_none():
    runner = _disp_runner(
        {("cognitiveservices", "account", "list"): None}
    )
    assert _discover_deployment_location("ptu-deploy-throttled", az_runner=runner) is None


def test_stage_0a_uses_discovery_when_rg_account_absent_and_redacts_notes():
    """When RG/account are absent but deployment is present and discovery
    finds exactly one match, Stage 0a proceeds. Crucially, the discovered
    rg/account names MUST NOT appear in the Stage0aResult.notes."""
    runner = _disp_runner(
        {
            ("cognitiveservices", "account", "list"): [
                {"name": "very-secret-account-name", "resourceGroup": "very-secret-rg"},
            ],
            ("cognitiveservices", "account", "deployment", "list"): [
                {"name": "ptu-deploy-throttled"},
            ],
            ("cognitiveservices", "account", "deployment", "show"): {
                "sku": {"name": "ProvisionedManaged"},
                "properties": {"spilloverDeploymentName": "gpt-5.2"},
            },
        }
    )
    env = {"AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED": "ptu-deploy-throttled"}
    r = run_stage_0a(env=env, az_runner=runner)
    assert r.verdict == "READY_FOR_SMOKE_PROOF"
    assert r.sku_alias == "PTU/ProvisionedManaged"
    # Notes must not contain the discovered identifiers.
    assert "very-secret-account-name" not in r.notes
    assert "very-secret-rg" not in r.notes
    # And no leakage via standard anonymization patterns either.
    assert_no_secrets(r.notes, where="discovery-notes")


def test_stage_0a_discovery_failure_yields_config_missing():
    runner = _disp_runner(
        {
            ("cognitiveservices", "account", "list"): [
                {"name": "acct-a", "resourceGroup": "rg-a"},
            ],
            ("cognitiveservices", "account", "deployment", "list"): [],
        }
    )
    env = {"AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED": "ptu-deploy-throttled"}
    r = run_stage_0a(env=env, az_runner=runner)
    assert r.verdict == "CONFIG-MISSING"
    assert r.spillover_deployment_name_present is None
    # Identifier names from the (failed) discovery must not leak.
    assert "acct-a" not in r.notes
    assert "rg-a" not in r.notes
    assert_no_secrets(r.notes, where="discovery-failed-notes")


# ---------------------------------------------------------------------------
# Obsolete-finding writer
# ---------------------------------------------------------------------------


def test_mark_obsolete_no_op_when_file_absent(tmp_path: pathlib.Path):
    p = tmp_path / "FEASIBILITY_FINDING.md"
    assert mark_feasibility_finding_obsolete(p, timestamp_iso="t", git_commit="abc") is False
    assert not p.exists()


def test_mark_obsolete_rewrites_existing_finding(tmp_path: pathlib.Path):
    p = tmp_path / "FEASIBILITY_FINDING.md"
    # Pre-existing CONFIG-MISSING content.
    p.write_text("# Task 021 v2.1 — Feasibility Finding (CONFIG-MISSING)\n\nstale...\n")
    ret = mark_feasibility_finding_obsolete(
        p, timestamp_iso="2026-06-02T10:00:00Z", git_commit="abc123"
    )
    assert ret is True
    text = p.read_text()
    assert "OBSOLETE" in text
    assert "Stage 1" in text  # mentions Stage 1 was NOT executed
    assert "NOT been executed" in text
    assert "2026-06-02T10:00:00Z" in text


def test_main_proceed_stage_1_marks_existing_finding_obsolete(
    monkeypatch, tmp_path: pathlib.Path, capsys
):
    """When Stage 0c resolves PROCEED_STAGE_1 and a stale finding file
    exists, main() must rewrite it as obsolete and NOT execute Stage 1."""
    monkeypatch.setattr(
        "scripts.preflight_native_spillover.run_stage_0a",
        lambda **kw: Stage0aResult(
            verdict="READY_FOR_SMOKE_PROOF",
            sku_alias="PTU/ProvisionedManaged",
            spillover_deployment_name_present=True,
            mode_a_property_configured=True,
            notes="Mode A satisfied (value redacted); SKU PTU.",
        ),
    )
    monkeypatch.setattr(
        "scripts.preflight_native_spillover.run_stage_0b",
        lambda **kw: Stage0bResult(
            verdict="SAME-API-OK",
            attempted=True,
            observed_header_names=("x-ms-deployment-name",),
            spillover_from_header_present=False,
            dry_run_cost_estimate_usd=0.0001,
            failure_reason="",
        ),
    )
    log_path = tmp_path / "PREFLIGHT_LOG.md"
    finding_path = tmp_path / "FEASIBILITY_FINDING.md"
    # Stale finding from a previous run.
    finding_path.write_text(
        "# Task 021 v2.1 — Feasibility Finding (CONFIG-MISSING)\n\nstale\n"
    )
    rc = main([
        "--log-path", str(log_path),
        "--finding-path", str(finding_path),
    ])
    assert rc == 0
    assert finding_path.is_file()
    text = finding_path.read_text()
    # Stale CONFIG-MISSING content must be GONE.
    assert "stale" not in text
    # And replaced with an OBSOLETE marker.
    assert "OBSOLETE" in text
    out = capsys.readouterr().out
    assert '"feasibility_finding_obsoleted": true' in out
    # Stage 1 was NOT executed.
    assert '"stage_1_proof_smoke_executed": false' in out


def test_main_proceed_stage_1_no_finding_file_is_noop(
    monkeypatch, tmp_path: pathlib.Path, capsys
):
    monkeypatch.setattr(
        "scripts.preflight_native_spillover.run_stage_0a",
        lambda **kw: Stage0aResult(verdict="READY_FOR_SMOKE_PROOF"),
    )
    monkeypatch.setattr(
        "scripts.preflight_native_spillover.run_stage_0b",
        lambda **kw: Stage0bResult(verdict="SAME-API-OK", attempted=True),
    )
    log_path = tmp_path / "PREFLIGHT_LOG.md"
    finding_path = tmp_path / "FEASIBILITY_FINDING.md"
    rc = main([
        "--log-path", str(log_path),
        "--finding-path", str(finding_path),
    ])
    assert rc == 0
    assert not finding_path.exists()
    out = capsys.readouterr().out
    assert '"feasibility_finding_obsoleted": false' in out

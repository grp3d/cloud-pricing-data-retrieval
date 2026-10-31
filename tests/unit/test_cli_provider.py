"""
Provider-aware CLI (contracts/cli.md; FR-006, SC-006; feature 004 T020).
"""

import json
import logging
from dataclasses import dataclass, field

import pytest
from click.testing import CliRunner

from src.pipeline import __main__ as cli_module
from src.pipeline import providers
from src.pipeline.config import ProviderSettings, SettingsError
from src.pipeline.notify import Notifier


@dataclass(frozen=True)
class KeyedSettings(ProviderSettings):
    SECRET_FIELDS = ("api_key",)
    api_key: str = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env):
        key = env.get("STUB_API_KEY")
        if not key:
            raise SettingsError("STUB_API_KEY is required")
        return cls(api_key=key)


@pytest.fixture
def keyed_provider(monkeypatch):
    monkeypatch.setitem(
        providers.PROVIDERS,
        "keyed",
        providers.ProviderSpec(
            name="keyed",
            tables={"thing": 1},
            default_locations=("here-1",),
            downloader="x:y",
            transform="x:y",
            settings="tests.unit.test_cli_provider:KeyedSettings",
        ),
    )


def _invoke(args, env):
    return CliRunner().invoke(cli_module.cli, args, env=env, catch_exceptions=False)


def test_upload_history_needs_a_legacy_layout_provider(keyed_provider, tmp_path):
    result = _invoke(
        ["upload-history", "--snapshot-date", "2026-10-05", "--source", str(tmp_path)],
        {"PIPELINE_PROVIDER": "keyed", "STUB_API_KEY": "SECRET-123", "PIPELINE_STORAGE_URI": f"file://{tmp_path}/s"},
    )
    assert result.exit_code == 2
    assert "upload-history" in result.output


def test_missing_provider_secret_exits_2(keyed_provider, tmp_path):
    result = _invoke(["run"], {"PIPELINE_PROVIDER": "keyed", "PIPELINE_STORAGE_URI": f"file://{tmp_path}/s"})
    assert result.exit_code == 2
    assert not (tmp_path / "s").exists()  # nothing claimed or written


def test_settings_prints_redacted_provider_settings(keyed_provider):
    result = _invoke(["settings"], {"PIPELINE_PROVIDER": "keyed", "STUB_API_KEY": "SECRET-123"})
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert doc["provider"] == "keyed"
    assert doc["provider_settings"] == {"api_key": "***"}
    assert "SECRET-123" not in result.output


def test_log_lines_are_redacted(keyed_provider, capsys):
    cli_module._configure_logging()
    cli_module._install_redaction(["SECRET-123"])
    try:
        logging.getLogger("pipeline").error("request failed with SECRET-123 in it")
        out = capsys.readouterr().out
    finally:
        cli_module._install_redaction([])
    assert "SECRET-123" not in out
    assert "***" in out


def test_notifier_redacts_secrets():
    class Settings:
        alert_topic_arn = None
        environment = "local"
        provider = "keyed"
        host_label = "local"

        def secret_values(self):
            return ["SECRET-123"]

    notifier = Notifier(Settings())
    subject, body = notifier.format("RUN FAILED", {"snapshot_date": "2026-10-05"}, detail="boom SECRET-123")
    assert "SECRET-123" not in subject + body

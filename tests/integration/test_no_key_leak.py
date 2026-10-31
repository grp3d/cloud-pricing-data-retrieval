"""
The GCP key appears nowhere the pipeline writes (FR-006, SC-006; feature 004 T032).

Runs GCP fixture runs through success, rejected-key and failing-page paths. The fake
source echoes the key back in error bodies, the worst case for leaks. Logs, run reports,
alert payloads, the `settings` output and every stored object (decompressed) are searched.
"""

import datetime as dt
import json
import logging
import os
from compression import zstd

import pytest
from click.testing import CliRunner

from src.gcp.downloader import GcpCatalogDownloader
from src.pipeline import __main__ as cli_module
from src.pipeline.config import PipelineSettings
from src.pipeline.notify import Notifier
from src.pipeline.runner import RunRequest, run_snapshot
from tests.helpers.fake_pricing import http_error
from tests.unit.test_gcp_api import catalog_routes, listing_urls

KEY = "TEST-KEY-7f3a9c"
T0 = dt.datetime(2026, 10, 30, 13, 0, 0, tzinfo=dt.timezone.utc)


class RecordingNotifier(Notifier):
    def __init__(self, settings):
        super().__init__(settings)
        self.payloads = []

    def send(self, kind, report, detail=None):
        self.payloads.append(self.format(kind, report, detail))
        return super().send(kind, report, detail)


def _settings(root):
    return PipelineSettings.from_env(
        {
            "PIPELINE_STORAGE_URI": f"file://{root}",
            "PIPELINE_PROVIDER": "gcp",
            "GCP_PRICING_API_KEY": KEY,
            "PRICING_REGIONS": "us-east1",
            "PRICING_DOWNLOAD_RETRY_BASE_DELAY_SECONDS": "0",
            "PRICING_DOWNLOAD_RETRY_MAX_RETRIES": "1",
            "PRICING_DOWNLOAD_RATE_LIMIT_PER_SECOND": "1000",
        }
    )


def _paths(settings, scenario):
    fake = catalog_routes(settings)
    urls = listing_urls(settings)
    echo = json.dumps({"error": {"status": "API_KEY_INVALID", "message": f"API key {KEY} not valid"}}).encode()
    if scenario == "rejected":
        fake.fail(urls["services"], http_error(400, body=echo))
    elif scenario == "failing-page":
        fake.fail(urls["skus"] + "&pageToken=fixture-skus-p0002", http_error(503, body=f"upstream saw {KEY}".encode()))
    return fake


def _stored_text(root):
    for dirpath, _, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                data = fh.read()
            if name.endswith(".zst"):
                data = zstd.decompress(data)
            yield path, data


@pytest.mark.parametrize("scenario", ["success", "rejected", "failing-page"])
def test_key_never_written(scenario, tmp_path, caplog):
    root = tmp_path / "store"
    settings = _settings(root)
    notifier = RecordingNotifier(settings)
    cli_module._install_redaction(settings.secret_values())
    try:
        with caplog.at_level(logging.DEBUG):
            report = run_snapshot(
                settings,
                RunRequest(snapshot_date="2026-10-30", trigger="manual"),
                downloader=GcpCatalogDownloader(settings, fetch=_paths(settings, scenario), sleep=lambda s: None),
                now=lambda: T0,
                sleep=lambda s: None,
                work_dir=str(tmp_path / "work"),
                notifier=notifier,
            )
    finally:
        cli_module._install_redaction([])

    expected = {"success": "succeeded", "rejected": "failed", "failing-page": "failed"}[scenario]
    assert report.snapshot_status == expected
    assert KEY not in report.to_json()
    assert KEY not in caplog.text
    for subject, body in notifier.payloads:
        assert KEY not in subject and KEY not in body
    leaks = [path for path, data in _stored_text(root) if KEY.encode() in data]
    assert leaks == []
    if scenario != "success":
        assert notifier.payloads, "a failure alert was expected"


def test_settings_output_and_repr(tmp_path):
    result = CliRunner().invoke(
        cli_module.cli, ["settings"], env={"PIPELINE_PROVIDER": "gcp", "GCP_PRICING_API_KEY": KEY},
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert KEY not in result.output
    assert json.loads(result.output)["provider_settings"]["api_key"] == "***"
    assert KEY not in repr(_settings(tmp_path))

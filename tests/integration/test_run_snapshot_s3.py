"""The same end-to-end scenarios as test_run_snapshot_local.py, against moto S3 (FR-008)."""

import pytest

from tests.integration.test_run_snapshot_local import SCENARIOS


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__)
def test_s3(scenario, s3_store, tmp_path):
    scenario(s3_store, tmp_path)


def test_host_label_recorded(s3_store, tmp_path):
    """An off-AWS writer produces the same output; only run.host differs (FR-052, FR-056)."""
    import datetime as dt

    from src.pipeline import manifest as m
    from src.pipeline.config import PipelineSettings
    from src.pipeline.runner import RunRequest, run_snapshot
    from tests.helpers.fake_pricing import FakePricingSource

    settings = PipelineSettings.from_env(
        {"PIPELINE_STORAGE_URI": s3_store.uri, "PRICING_REGIONS": "us-east-1", "PIPELINE_HOST_LABEL": "home-server"}
    )
    run_snapshot(
        settings,
        RunRequest(snapshot_date="2026-10-05"),
        store=s3_store,
        downloader=FakePricingSource(),
        now=lambda: dt.datetime(2026, 10, 5, 13, tzinfo=dt.timezone.utc),
        sleep=lambda s: None,
        work_dir=str(tmp_path),
    )
    assert m.read_current(s3_store, "aws", "2026-10-05").run.host == "home-server"

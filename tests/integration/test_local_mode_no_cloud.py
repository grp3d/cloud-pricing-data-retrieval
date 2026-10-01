"""
A local-directory run needs no cloud storage or alerting (FR-036, SC-009), and produces the
same manifest as an S3 run apart from run identity, timestamps and host.
"""

import datetime as dt
import json

import boto3

from src.pipeline import layout
from src.pipeline import manifest as m
from src.pipeline.config import PipelineSettings
from src.pipeline.runner import RunRequest, run_snapshot
from tests.helpers.fake_pricing import FakePricingSource

DATE = "2026-10-05"
T0 = dt.datetime(2026, 10, 5, 13, 0, tzinfo=dt.timezone.utc)
VOLATILE = {"run_id", "created_at"}


def _run(store, tmp_path, host):
    settings = PipelineSettings.from_env(
        {"PIPELINE_STORAGE_URI": store.uri, "PRICING_REGIONS": "us-east-1,eu-west-1", "PIPELINE_HOST_LABEL": host}
    )
    return run_snapshot(
        settings,
        RunRequest(snapshot_date=DATE),
        store=store,
        downloader=FakePricingSource(),
        now=lambda: T0,
        sleep=lambda s: None,
        work_dir=str(tmp_path),
    )


def _normalized(store, run_id):
    doc = json.loads(store.get_bytes(layout.manifest_key("aws", DATE)))
    for key in VOLATILE:
        doc.pop(key)
    doc["run"].pop("host")
    text = json.dumps(doc, sort_keys=True).replace(run_id, "<run>")
    return json.loads(text)


def test_local_run_uses_no_cloud_clients(local_store, tmp_path, monkeypatch):
    def forbidden(service, *args, **kwargs):
        if service in ("s3", "sns"):
            raise AssertionError(f"local mode must not create a {service} client")
        return real(service, *args, **kwargs)

    real = boto3.client
    monkeypatch.setattr(boto3, "client", forbidden)
    report = _run(local_store, tmp_path, "local")
    assert report.snapshot_status == "succeeded"


def test_local_and_s3_manifests_are_equivalent(local_store, s3_store, tmp_path):
    local = _run(local_store, tmp_path / "a", "laptop")
    cloud = _run(s3_store, tmp_path / "b", "aws-ecs")
    assert _normalized(local_store, local.run_id) == _normalized(s3_store, cloud.run_id)
    assert m.read_current(local_store, "aws", DATE).run.host == "laptop"
    assert m.read_current(s3_store, "aws", DATE).run.host == "aws-ecs"

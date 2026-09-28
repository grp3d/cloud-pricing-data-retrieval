"""
Contract schemas (analysis C2/C3, constitution IV): the runtime copies must equal the
spec contracts, and documents the code builds must validate against them.
"""

import datetime as dt
import json
import os

import pytest

from src.pipeline import latest
from src.pipeline import manifest as m
from src.pipeline.config import PipelineSettings
from tests.helpers.contracts import SCHEMA_DIR, validate_latest_doc, validate_manifest_doc

REPO = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
SPEC_CONTRACTS = os.path.join(REPO, "specs", "003-pipeline-cloud-deployment", "contracts")
T0 = dt.datetime(2026, 10, 5, 13, 0, 0, tzinfo=dt.timezone.utc)
RUN = "20261005T130000Z-111111"
DATE = "2026-10-05"
SETTINGS = PipelineSettings.from_env({})


@pytest.mark.parametrize("name", ["manifest.schema.json", "latest.schema.json"])
def test_runtime_schema_copies_match_spec_contracts(name):
    with open(os.path.join(SCHEMA_DIR, name), "rb") as a, open(os.path.join(SPEC_CONTRACTS, name), "rb") as b:
        assert a.read() == b.read(), f"src/pipeline/schemas/{name} drifted from the spec contract"


def _ok(region):
    tables = {
        t: m.RegionTableEntry(
            RUN,
            3,
            [
                m.DataFile(
                    f"aws/parquet/{t}/snapshot_date={DATE}/region={region}/part-{RUN}.parquet", 10, "a" * 64, 3
                )
            ],
        )
        for t in m.TABLES
    }
    raw = m.make_raw_entry(f"aws/raw/{DATE}/{region}/{RUN}/", 2, 99, T0, 30)
    return m.RegionOutcome(region=region, succeeded=True, attempts=1, tables=tables, raw=raw)


def _build(outcomes):
    return m.build_revision(
        None,
        provider="aws",
        snapshot_date=DATE,
        run_id=RUN,
        run_info=m.new_run_info(SETTINGS, "scheduled", "full", T0, T0),
        outcomes=outcomes,
        requested_regions=[o.region for o in outcomes],
        created_at=T0,
    )


@pytest.mark.parametrize(
    "outcomes,status",
    [
        (lambda: [_ok("us-east-1"), _ok("eu-west-1")], "succeeded"),
        (lambda: [_ok("us-east-1"), m.RegionOutcome("eu-west-1", False, 1, reason="HTTP 403")], "partial"),
        (lambda: [m.RegionOutcome("eu-west-1", False, 4, reason="ReadTimeout")], "failed"),
    ],
)
def test_built_manifests_validate(outcomes, status):
    man = _build(outcomes())
    assert man.status == status
    validate_manifest_doc(json.loads(man.to_json()))


def test_latest_pointer_validates(local_store):
    man = _build([_ok("us-east-1")])
    latest.update_latest(local_store, man, T0)
    validate_latest_doc(latest.read_latest(local_store, "aws"))


def test_purged_manifest_validates():
    man = m.build_purged_revision(_build([_ok("us-east-1")]), T0)
    assert man.status == "purged"
    validate_manifest_doc(json.loads(man.to_json()))


def test_backfill_manifest_validates(tmp_path):
    """origin=backfill requires raw=null (manifest schema allOf rule)."""
    from src.pipeline import backfill
    from tests.unit.test_backfill import legacy_tree

    src = legacy_tree(tmp_path / "data")
    snap = backfill.find_local_snapshot(str(src), "aws", "2026-06-01")
    man, _ = backfill.build_backfill_manifest(snap, SETTINGS, run_id=RUN, revision=1, now=T0)
    doc = json.loads(man.to_json())
    validate_manifest_doc(doc)
    assert doc["raw"] is None
    doc["raw"] = {}
    with pytest.raises(Exception):
        validate_manifest_doc(doc)

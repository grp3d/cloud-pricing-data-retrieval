"""
Transform-only for Azure and GCP (US5; FR-023, FR-034; feature 004 T048): a new revision
rebuilt from stored raw data, with no request to the source; refused (exit 3) when the raw
data is gone.
"""

import pytest

from src.pipeline import manifest as m
from src.pipeline.runner import PreconditionError
from tests.helpers.fake_pricing import FakeHttpSource
from tests.integration import test_run_snapshot_azure as azure_run
from tests.integration import test_run_snapshot_gcp as gcp_run

MODULES = {"azure": azure_run, "gcp": gcp_run}


@pytest.mark.parametrize("provider", ["azure", "gcp"])
def test_transform_only_rebuilds_without_requests(provider, store, tmp_path):
    module = MODULES[provider]
    first, _, _ = module.run(store, tmp_path)
    assert first.snapshot_status == "succeeded"
    silent = FakeHttpSource()
    second, _, _ = module.run(store, tmp_path, fake=silent, transform_only=True)
    assert silent.requests == []
    assert second.snapshot_status == "succeeded" and second.revision == 2
    assert second.row_counts == first.row_counts
    current = m.read_current(store, provider, module.DATE)
    assert current.run.mode == "transform-only"
    assert current.raw == m.read_current(store, provider, module.DATE).raw


@pytest.mark.parametrize("provider", ["azure", "gcp"])
def test_transform_only_without_raw_is_refused(provider, store, tmp_path):
    module = MODULES[provider]
    module.run(store, tmp_path)
    for obj in list(store.list(f"{provider}/raw/")):
        store.delete(obj.key)
    with pytest.raises(PreconditionError):
        module.run(store, tmp_path, fake=FakeHttpSource(), transform_only=True)


def test_transform_only_keeps_the_devtest_exclusion(store, tmp_path):
    settings = azure_run.settings_for(store, INCLUDE_AZURE_DEVTEST_PRICES="false")
    azure_run.run(store, tmp_path, settings=settings)
    azure_run.run(store, tmp_path, settings=settings, fake=FakeHttpSource(), transform_only=True)
    assert m.read_current(store, "azure", azure_run.DATE).excluded_price_types == ["DevTestConsumption"]

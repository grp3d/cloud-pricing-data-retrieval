"""
GCP downloader for the pipeline runner: the whole catalog once per run (FR-031, FR-034).
"""

import time
from typing import Callable, Optional

from src.gcp.api import GcpCatalogClient
from src.pipeline.downloader import CatalogDownload, CatalogDownloader


class GcpCatalogDownloader(CatalogDownloader):
    def __init__(self, settings, log: Optional[Callable[[str], None]] = None, fetch=None, sleep=time.sleep):
        self._log = log
        self._client = GcpCatalogClient(settings, fetch=fetch, sleep=sleep)

    def download_catalog(self, dest_dir: str) -> CatalogDownload:
        out = self._client.download_catalog(dest_dir)
        if self._log:
            self._log(f"gcp catalog: {len(out.files)} page(s), {out.counters.get('requests_made', 0)} request(s)")
        return out

"""
Clock sanity check for cloud storage roots (spec edge case "clock is wrong", research R5).

Run-claim expiry and superseded-file grace periods compare this host's clock with storage
timestamps. Fargate clocks are NTP-synced; an off-AWS writer might not be, so a run refuses
to start when its clock is off by more than MAX_CLOCK_SKEW_SECONDS.
"""

import datetime as dt

from src.pipeline.config import SettingsError


def check_clock_skew(store, settings, now: dt.datetime) -> None:
    if getattr(store, "is_local", True):
        return
    server = store.server_time()
    if server is None:
        return
    skew = abs((now - server).total_seconds())
    if skew > settings.max_clock_skew_seconds:
        raise SettingsError(
            f"clock skew of {skew:.0f}s with the storage service exceeds "
            f"MAX_CLOCK_SKEW_SECONDS={settings.max_clock_skew_seconds}; sync this host's clock (NTP)"
        )

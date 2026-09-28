"""
Owner alerts and optional success summaries (FR-026, FR-028, FR-050, FR-057; research R12).

Published to SNS (email) when ALERT_TOPIC_ARN is set; otherwise logged, so local runs
still show what would have been sent. Publishing never raises: a broken alert channel
must not turn a completed run into a crash.
"""

import json
import logging
from dataclasses import asdict, is_dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

RUN_FAILED = "RUN FAILED"
RUN_PARTIAL = "RUN PARTIAL"
RUN_REFUSED = "RUN REFUSED"
RETENTION_ERROR = "RETENTION ERROR"
RUN_SUCCEEDED = "RUN SUCCEEDED"
RUN_CRASHED = "RUN CRASHED"

SNS_SUBJECT_MAX = 100


def _as_dict(report) -> dict:
    if report is None:
        return {}
    return asdict(report) if is_dataclass(report) else dict(report)


def alert_kinds_for(report, settings) -> List[str]:
    """Which messages a finished run should send (contracts/configuration.md)."""
    r = _as_dict(report)
    kinds: List[str] = []
    if r.get("outcome") == "refused":
        if r.get("trigger") == "scheduled":
            kinds.append(RUN_REFUSED)
        return kinds
    status = r.get("snapshot_status")
    any_region_failed = any(x.get("outcome") == "failed" for x in r.get("region_results") or [])
    if status == "failed":
        kinds.append(RUN_FAILED)
    elif status == "partial" or any_region_failed:
        kinds.append(RUN_PARTIAL)
    elif status == "succeeded" and settings.success_summary_enabled:
        kinds.append(RUN_SUCCEEDED)
    if (r.get("retention") or {}).get("error"):
        kinds.append(RETENTION_ERROR)
    return kinds


class Notifier:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.topic_arn = settings.alert_topic_arn
        self._client = client
        self.sent: List[str] = []
        if self.topic_arn and self._client is None:
            import boto3

            region = self.topic_arn.split(":")[3] if self.topic_arn.count(":") >= 5 else None
            self._client = boto3.client("sns", region_name=region)

    @classmethod
    def from_settings(cls, settings) -> "Notifier":
        return cls(settings)

    def format(self, kind: str, report, detail: Optional[str] = None) -> Tuple[str, str]:
        r = _as_dict(report)
        date = r.get("snapshot_date") or ""
        subject = f"[cloud-pricing {self.settings.environment}] {kind}: {self.settings.provider} {date}".strip()
        subject = subject[:SNS_SUBJECT_MAX]

        lines = [
            f"{kind} — {self.settings.provider} snapshot {date or '(unknown date)'}",
            f"run_id: {r.get('run_id', '-')}   trigger: {r.get('trigger', '-')}   mode: {r.get('mode', '-')}",
            f"host: {self.settings.host_label}   snapshot status: {r.get('snapshot_status') or '-'}",
        ]
        if r.get("refused_by"):
            lines.append(f"refused: another run holds the claim (run_id={r['refused_by']})")
        for rr in r.get("region_results") or []:
            reason = f" — {rr['reason']}" if rr.get("reason") else ""
            lines.append(f"  {rr.get('region')}: {rr.get('outcome')} (attempts {rr.get('attempts', 0)}){reason}")
        if r.get("row_counts"):
            lines.append("row counts: " + ", ".join(f"{k}={v:,}" for k, v in sorted(r["row_counts"].items())))
        if r.get("duration_seconds"):
            lines.append(f"duration: {r['duration_seconds']:.0f}s")
        if (r.get("retention") or {}).get("error"):
            lines.append(f"retention error: {r['retention']['error']}")
        if detail:
            lines.append("")
            lines.append(detail)
        body = "\n".join(lines) + "\n---\n" + json.dumps({"run_report": r}, sort_keys=True, default=str)
        return subject, body

    def send(self, kind: str, report, detail: Optional[str] = None) -> bool:
        subject, body = self.format(kind, report, detail)
        if not self.topic_arn:
            logger.warning(json.dumps({"alert": kind, "subject": subject, "body": body}))
            self.sent.append(kind)
            return True
        try:
            self._client.publish(TopicArn=self.topic_arn, Subject=subject, Message=body)
        except Exception as e:
            logger.error(f"Failed to publish {kind} alert: {e}")
            return False
        self.sent.append(kind)
        return True

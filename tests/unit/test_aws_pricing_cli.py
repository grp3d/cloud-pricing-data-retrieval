"""The ad hoc single-region CLI keeps its behavior (FR-037)."""

from click.testing import CliRunner

from src.aws_pricing_api import CredentialsError, PricingJobResult
from src.aws_pricing_cli import fetch_pricing


def _capture(monkeypatch, result=None, exc=None):
    seen = {}

    def fake_run(request):
        seen["request"] = request
        if exc:
            raise exc
        return result or PricingJobResult(downloaded_files=["a.json"], success=True)

    monkeypatch.setattr("src.aws_pricing_cli.run_pricing_job", fake_run)
    return seen


def test_all_services_request(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    out = CliRunner().invoke(
        fetch_pricing, ["--region", "eu-west-1", "--all-services", "--output-dir", str(tmp_path), "--format", "json"]
    )
    assert out.exit_code == 0
    req = seen["request"]
    assert (req.region, req.all_services, req.output_dir, req.output_format) == (
        "eu-west-1",
        True,
        str(tmp_path),
        "json",
    )
    assert "Download completed! 1 file(s) downloaded." in out.output


def test_service_codes_consolidate_and_truncate(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    CliRunner().invoke(
        fetch_pricing,
        [
            "--region", "us-east-1", "--service-code", "AmazonEC2, AmazonS3", "--output-dir", str(tmp_path),
            "--consolidate", "--truncate", "sku, price",
        ],
    )
    req = seen["request"]
    assert req.service_codes == ["AmazonEC2", "AmazonS3"]
    assert req.consolidate is True
    assert req.truncate_columns == ["sku", "price"]
    assert req.output_format == "csv"  # unchanged default


def test_requires_service_selection(monkeypatch):
    seen = _capture(monkeypatch)
    out = CliRunner().invoke(fetch_pricing, ["--region", "us-east-1"])
    assert "Must specify either --service-code or --all-services" in out.output
    assert "request" not in seen


def test_credentials_error_exits_1(monkeypatch, tmp_path):
    _capture(monkeypatch, exc=CredentialsError("no creds"))
    out = CliRunner().invoke(fetch_pricing, ["--region", "us-east-1", "--all-services", "--output-dir", str(tmp_path)])
    assert out.exit_code == 1
    assert "Credential Error" in out.output


def test_no_data_message(monkeypatch, tmp_path):
    _capture(monkeypatch, result=PricingJobResult(success=False, errors=["No pricing data was downloaded."]))
    out = CliRunner().invoke(fetch_pricing, ["--region", "us-east-1", "--all-services", "--output-dir", str(tmp_path)])
    assert out.exit_code == 0
    assert "No pricing data was downloaded." in out.output

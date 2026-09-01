"""
aws_pricing_cli.py

Thin CLI wrapper around aws_pricing_api.py.
All business logic lives in pricing_api; this module only handles
argument parsing and terminal output.
"""

import sys

import click

from src.aws_pricing_api import (
    CREDENTIAL_HELP,
    CredentialsError,
    PricingJobRequest,
    resolve_output_dir,
    run_pricing_job,
)


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--region", required=True, help="AWS region to fetch prices for")
@click.option(
    "--service-code",
    help="Comma-separated list of AWS service codes (e.g. AmazonEC2,AmazonS3)",
)
@click.option(
    "--all-services", is_flag=True, help="Fetch prices for all available services"
)
@click.option(
    "--output-dir",
    default="",
    help="Directory to save output files (defaults to DATA_DIRECTORY_ROOT or current directory)",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["csv", "json", "excel"]),
    default="csv",
    help="Output file format",
)
@click.option(
    "--consolidate", is_flag=True, help="Consolidate all pricing files into one"
)
@click.option(
    "--truncate", help="Comma-separated list of columns to keep in the output"
)
@click.option("--debug", is_flag=True, help="Enable debug mode (full tracebacks)")
def fetch_pricing(
    region: str,
    service_code: str,
    all_services: bool,
    output_dir: str,
    output_format: str,
    consolidate: bool,
    truncate: str,
    debug: bool,
):
    """AWS service pricing information fetcher."""

    if not (service_code or all_services):
        click.secho(
            "\nError: Must specify either --service-code or --all-services",
            fg="red",
            bold=True,
        )
        ctx = click.get_current_context()
        click.echo(ctx.get_help())
        return

    if service_code and all_services:
        click.secho(
            "\nError: Cannot use both --service-code and --all-services",
            fg="red",
            bold=True,
        )
        return

    click.secho("=" * 50, fg="blue")
    click.secho(f" AWS Pricing Data Fetcher - Region: {region} ", fg="blue", bold=True)
    click.secho("=" * 50, fg="blue")

    output_dir = resolve_output_dir(output_dir)

    request = PricingJobRequest(
        region=region,
        output_dir=output_dir,
        output_format=output_format,
        service_codes=(
            [c.strip() for c in service_code.split(",")] if service_code else []
        ),
        all_services=all_services,
        consolidate=consolidate,
        truncate_columns=(
            [c.strip() for c in truncate.split(",")] if truncate else []
        ),
        log_callback=lambda msg: click.echo(msg),
    )

    try:
        result = run_pricing_job(request)

        if result.errors:
            for err in result.errors:
                click.secho(f"\nError: {err}", fg="red", err=True)

        if result.failed_services:
            click.secho("\nWarning: No pricing data found for:", fg="yellow")
            for svc in result.failed_services:
                click.secho(f"  - {svc}", fg="yellow")

        if result.consolidated_file:
            click.secho(
                f"Consolidated file: {result.consolidated_file}", fg="green"
            )
        if result.truncated_file:
            click.secho(
                f"Truncated file:    {result.truncated_file}", fg="green"
            )

        if result.success:
            click.secho(
                f"\nDownload completed! {len(result.downloaded_files)} file(s) downloaded.",
                fg="green",
                bold=True,
            )
        else:
            click.secho("\nNo pricing data was downloaded.", fg="yellow", bold=True)

    except CredentialsError as e:
        click.secho(f"\nCredential Error: {e}", fg="red", err=True)
        click.echo(CREDENTIAL_HELP)
        sys.exit(1)
    except Exception as e:
        if debug:
            raise
        click.secho(f"\nError: {e}", fg="red", err=True)
        sys.exit(1)


if __name__ == "__main__":
    fetch_pricing()
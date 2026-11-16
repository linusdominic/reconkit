"""Command line interface.

Exit codes (stable; safe to branch on in CI and orchestrators):

* 0 - every check passed / no breaking contract change
* 1 - differences found, a contract rule failed, or a breaking contract change
* 2 - invalid configuration, contract file or command line
* 3 - a check could not run (connection failure, query error)
"""

from __future__ import annotations

import functools
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import click
from rich.console import Console
from rich.markup import escape

from reconkit import __version__
from reconkit.config import (
    CheckConfig,
    Config,
    ContractCheckConfig,
    OptionsConfig,
    TableConfig,
    load_config,
)
from reconkit.contracts.diff import diff_contracts, version_bump_ok
from reconkit.contracts.model import load_contract
from reconkit.errors import ConfigError, ReconkitError
from reconkit.report import render_terminal, to_html, to_json, to_junit
from reconkit.runner import EXIT_CONFIG, EXIT_DIFFERENCES, EXIT_ERROR, EXIT_OK, Runner, RunReport

F = TypeVar("F", bound=Callable[..., Any])


def _handle_errors(fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ConfigError as exc:
            click.echo(f"reconkit: configuration error: {exc}", err=True)
            sys.exit(EXIT_CONFIG)
        except ReconkitError as exc:
            click.echo(f"reconkit: error: {exc}", err=True)
            sys.exit(EXIT_ERROR)

    return wrapper  # type: ignore[return-value]


def _report_options(fn: F) -> F:
    options = [
        click.option(
            "--json",
            "json_path",
            type=click.Path(dir_okay=False),
            help="Write a JSON report to this path ('-' for stdout).",
        ),
        click.option(
            "--junit",
            "junit_path",
            type=click.Path(dir_okay=False),
            help="Write a JUnit XML report to this path.",
        ),
        click.option(
            "--html",
            "html_path",
            type=click.Path(dir_okay=False),
            help="Write a self-contained HTML report to this path.",
        ),
        click.option(
            "--max-rows",
            default=20,
            show_default=True,
            help="Differing rows listed in the terminal report.",
        ),
        click.option("-q", "--quiet", is_flag=True, help="No terminal report."),
        click.option("-v", "--verbose", is_flag=True, help="Also list passing contract rules."),
    ]
    for option in reversed(options):
        fn = option(fn)
    return fn


def _emit(
    report: RunReport,
    *,
    json_path: str | None,
    junit_path: str | None,
    html_path: str | None,
    max_rows: int,
    quiet: bool,
    verbose: bool,
) -> None:
    json_to_stdout = json_path == "-"
    if not quiet and not json_to_stdout:
        render_terminal(report, Console(), max_rows=max_rows, verbose=verbose)
    if json_to_stdout:
        click.echo(to_json(report))
    elif json_path:
        Path(json_path).write_text(to_json(report), encoding="utf-8")
    if junit_path:
        Path(junit_path).write_text(to_junit(report), encoding="utf-8")
    if html_path:
        Path(html_path).write_text(to_html(report), encoding="utf-8")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="reconkit")
def main() -> None:
    """Reconcile tables across databases and enforce data contracts."""


@main.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    default="reconkit.yml",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Config file.",
)
@click.option("--all", "run_all", is_flag=True, help="Run every check and contract.")
@click.option("--no-audit", is_flag=True, help="Do not write to audit tables.")
@click.argument("names", nargs=-1)
@_report_options
@_handle_errors
def run(
    config_path: str, run_all: bool, no_audit: bool, names: tuple[str, ...], **report_opts: Any
) -> None:
    """Run named checks from the config file, or all of them with --all."""
    if not run_all and not names:
        raise click.UsageError("name at least one check, or pass --all")
    if run_all and names:
        raise click.UsageError("pass either check names or --all, not both")
    with Runner(load_config(config_path)) as runner:
        report = runner.run(None if run_all else list(names), audit=not no_audit)
    _emit(report, **report_opts)
    sys.exit(report.exit_code)


def _side(spec: str) -> TableConfig:
    connection, sep, table = spec.partition(":")
    if not sep or not connection or not table:
        raise ConfigError(f"expected CONNECTION:TABLE, got {spec!r}")
    return TableConfig(connection=connection, table=table)


@main.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    default="reconkit.yml",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Config file providing the connections.",
)
@click.option("--left", required=True, metavar="CONNECTION:TABLE")
@click.option("--right", required=True, metavar="CONNECTION:TABLE")
@click.option("-k", "--key", "key", multiple=True, required=True, help="Key column; repeatable.")
@click.option("--column", "columns", multiple=True, help="Compare only these columns.")
@click.option("--ignore", multiple=True, help="Column to leave out; repeatable.")
@click.option("--where", help="Row filter applied to both tables.")
@click.option("--strategy", type=click.Choice(["auto", "bisect", "join"]), default="auto")
@click.option("--bucketing", type=click.Choice(["auto", "range", "hash"]), default="auto")
@click.option("--sample", type=float, help="Compare a key-hash sample, e.g. 0.01.")
@click.option("--limit", type=int, help="Stop after this many differing rows.")
@_report_options
@_handle_errors
def diff(
    config_path: str,
    left: str,
    right: str,
    key: tuple[str, ...],
    columns: tuple[str, ...],
    ignore: tuple[str, ...],
    where: str | None,
    strategy: str,
    bucketing: str,
    sample: float | None,
    limit: int | None,
    **report_opts: Any,
) -> None:
    """Compare two tables ad hoc, using connections from the config file."""
    config = load_config(config_path)
    lside, rside = _side(left), _side(right)
    lside.where = rside.where = where
    config.checks = {
        "diff": CheckConfig(
            left=lside,
            right=rside,
            key=list(key),
            columns=list(columns) or None,
            ignore=list(ignore),
            options=OptionsConfig.model_validate(
                {"strategy": strategy, "bucketing": bucketing, "sample": sample, "limit": limit}
            ),
        )
    }
    config.contracts = {}
    for side in (lside, rside):
        config.connection(side.connection)
    with Runner(config) as runner:
        report = runner.run(["diff"], audit=False)
    _emit(report, **report_opts)
    sys.exit(report.exit_code)


@main.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    default="reconkit.yml",
    show_default=True,
    type=click.Path(dir_okay=False),
)
@click.option("--connect", is_flag=True, help="Also open every connection.")
@_handle_errors
def validate(config_path: str, connect: bool) -> None:
    """Check that the config file is valid."""
    config = load_config(config_path)
    for name, contract in config.contracts.items():
        load_contract(config.base_dir / contract.contract)
        del name
    click.echo(
        f"{config_path}: ok ({len(config.connections)} connections, {len(config.checks)} "
        f"checks, {len(config.contracts)} contracts)"
    )
    if connect:
        with Runner(config) as runner:
            for name in config.connections:
                runner.adapter(name).query("SELECT 1")
                click.echo(f"  connection {name}: ok")


@main.group()
def contract() -> None:
    """Data contract commands."""


@contract.command("check")
@click.argument("contract_path", type=click.Path(dir_okay=False))
@click.option(
    "-c",
    "--config",
    "config_path",
    default="reconkit.yml",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Config file providing the connection.",
)
@click.option("--connection", required=True, help="Connection name from the config file.")
@click.option("--table", help="Override the contract's physical table name.")
@_report_options
@_handle_errors
def contract_check(
    contract_path: str, config_path: str, connection: str, table: str | None, **report_opts: Any
) -> None:
    """Validate a live table against a contract file."""
    config: Config = load_config(config_path)
    config.connection(connection)
    name = Path(contract_path).stem
    config.checks = {}
    config.contracts = {
        name: ContractCheckConfig(
            connection=connection, table=table, contract=str(Path(contract_path).resolve())
        )
    }
    with Runner(config) as runner:
        report = runner.run([name], audit=False)
    _emit(report, **report_opts)
    sys.exit(report.exit_code)


@contract.command("diff")
@click.argument("old_path", type=click.Path(dir_okay=False))
@click.argument("new_path", type=click.Path(dir_okay=False))
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json", "markdown"]),
    default="text",
    show_default=True,
)
@click.option(
    "--fail-on",
    type=click.Choice(["breaking", "never"]),
    default="breaking",
    show_default=True,
    help="When to exit with code 1.",
)
@click.option(
    "--require-major-bump",
    is_flag=True,
    help="Only fail on breaking changes that lack a major version bump.",
)
@_handle_errors
def contract_diff(
    old_path: str, new_path: str, fmt: str, fail_on: str, require_major_bump: bool
) -> None:
    """Classify changes between two contract versions as breaking or non-breaking."""
    old, new = load_contract(old_path), load_contract(new_path)
    changes = diff_contracts(old, new)
    breaking = [c for c in changes if c.severity == "breaking"]
    bumped = version_bump_ok(old, new, changes)
    if fmt == "json":
        click.echo(
            json.dumps(
                {
                    "old_version": old.version,
                    "new_version": new.version,
                    "breaking": len(breaking),
                    "non_breaking": len(changes) - len(breaking),
                    "major_version_bumped": bumped,
                    "changes": [c.__dict__ for c in changes],
                },
                indent=2,
            )
        )
    elif fmt == "markdown":
        click.echo(f"### Contract changes: {old.version} -> {new.version}\n")
        if not changes:
            click.echo("No changes to the schema or quality rules.")
        else:
            click.echo("| Severity | Path | Change |\n| --- | --- | --- |")
            for c in changes:
                label = "**breaking**" if c.severity == "breaking" else "non-breaking"
                click.echo(f"| {label} | `{c.path}` | {c.message} |")
        if breaking and not bumped:
            click.echo("\nBreaking changes without a major version bump.")
    else:
        console = Console()
        console.print(
            f"contract {escape(old.version)} -> {escape(new.version)}: "
            f"{len(breaking)} breaking, {len(changes) - len(breaking)} non-breaking"
        )
        for c in changes:
            tag = (
                "[red]BREAKING    [/red]"
                if c.severity == "breaking"
                else "[green]non-breaking[/green]"
            )
            console.print(f"  {tag} {escape(c.path)}: {escape(c.message)}")
        if breaking and not bumped:
            console.print("[yellow]breaking changes without a major version bump[/yellow]")
    failed = bool(breaking) and fail_on == "breaking" and not (require_major_bump and bumped)
    sys.exit(EXIT_DIFFERENCES if failed else EXIT_OK)


if __name__ == "__main__":  # pragma: no cover
    main()

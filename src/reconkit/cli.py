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


@main.command("data-diff")
@click.argument("database1", required=False)
@click.argument("table1", required=False)
@click.argument("database2", required=False)
@click.argument("table2", required=False)
@click.option(
    "-k",
    "--key-columns",
    multiple=True,
    metavar="NAME",
    help="Key column; repeatable. Default: id.",
)
@click.option(
    "-t",
    "--update-column",
    metavar="NAME",
    help="Name of an updated_at column; compared, and used by --min-age/--max-age.",
)
@click.option(
    "-c",
    "--columns",
    multiple=True,
    metavar="NAME",
    help="Extra column to compare; repeatable; % matches any characters.",
)
@click.option(
    "-l", "--limit", type=int, metavar="NUM", help="Maximum number of differences to print."
)
@click.option("--bisection-factor", type=int, metavar="NUM", help="Segments per iteration.")
@click.option(
    "--bisection-threshold", type=int, metavar="NUM", help="Download segments at most this large."
)
@click.option(
    "--min-age",
    metavar="AGE",
    help="Only rows older than this, e.g. 5min (by --update-column, in UTC).",
)
@click.option("--max-age", metavar="AGE", help="Only rows younger than this.")
@click.option("-s", "--stats", is_flag=True, help="Print stats instead of a detailed diff.")
@click.option("--json", "json_output", is_flag=True, help="Print JSON lines.")
@click.option("-w", "--where", help="Extra SQL predicate applied to both tables.")
@click.option(
    "-a",
    "--algorithm",
    type=click.Choice(["auto", "joindiff", "hashdiff"]),
    help="joindiff needs both tables in one database.",
)
@click.option("--case-sensitive", is_flag=True, help="Match column names case-sensitively.")
@click.option(
    "--assume-unique-key",
    is_flag=True,
    help="Accepted for compatibility; reconkit reports duplicate keys instead of failing.",
)
@click.option("-j", "--threads", metavar="N", help="Accepted for compatibility and ignored.")
@click.option("-d", "--debug", is_flag=True, help="Same as --verbose.")
@click.option(
    "-v", "--verbose", is_flag=True, help="Print the columns compared and timing to stderr."
)
@click.option(
    "--no-tracking",
    is_flag=True,
    help="Accepted for compatibility; reconkit sends nothing anywhere.",
)
@click.option("--table-write-limit", type=int, hidden=True)
@click.option("-i", "--interactive", is_flag=True, hidden=True)
@click.option("-m", "--materialize-to-table", hidden=True)
@click.option("--materialize-all-rows", is_flag=True, hidden=True)
@click.option("--sample-exclusive-rows", is_flag=True, hidden=True)
@click.option("--cloud", is_flag=True, hidden=True)
@click.option("--conf", type=click.Path(dir_okay=False), help="data-diff TOML configuration file.")
@click.option("--run", "run_name", metavar="NAME", help="Run configuration to use from --conf.")
@click.option("--dbt", is_flag=True, help="Compare each dbt model's dev build with production.")
@click.option("--dbt-profiles-dir", help="Directory holding profiles.yml.")
@click.option(
    "--dbt-project-dir", help="Directory holding dbt_project.yml (default: current or a parent)."
)
@click.option("--select", help="dbt models to compare (a subset of dbt's selection syntax).")
@click.option(
    "--state",
    type=click.Path(dir_okay=False),
    help="Production manifest.json to take production locations from.",
)
@click.option("-pd", "--prod-database", help="Override the production database.")
@click.option("-ps", "--prod-schema", help="Override the production schema.")
@click.option(
    "--fail-on-diff",
    is_flag=True,
    help="Exit 1 when differences are found (data-diff always exited 0).",
)
@click.option("--version", "show_version", is_flag=True, help="Print the version and exit.")
@_handle_errors
def data_diff_command(**kw: Any) -> None:
    """Drop-in replacement for the archived data-diff command line.

    \b
    reconkit data-diff DB1_URI TABLE1 DB2_URI TABLE2 [OPTIONS]
    reconkit data-diff --conf datadiff.toml --run NAME
    reconkit data-diff --dbt [--select MODELS]

    URIs: postgresql://, redshift://, snowflake://, bigquery://, duckdb://.
    See docs/migrating-from-data-diff.md.
    """
    from reconkit.compat import datadiff, dbt

    if kw.pop("show_version"):
        click.echo(f"reconkit {__version__}")
        return
    if kw.pop("interactive"):
        raise ConfigError("--interactive is not supported")
    for key, flag in datadiff.UNSUPPORTED_OPTIONS.items():
        if kw.pop(key):
            raise ConfigError(
                f"{flag} is not supported by reconkit (see docs/migrating-from-data-diff.md)"
            )
    verbose = bool(kw.pop("verbose")) | bool(kw.pop("debug"))
    fail_on_diff = kw.pop("fail_on_diff")
    if kw.pop("table_write_limit") is not None:
        raise ConfigError("--table-write-limit is not supported by reconkit (no output tables)")
    for ignored in ("threads", "no_tracking"):
        kw.pop(ignored)
    err = Console(stderr=True, highlight=False)

    if kw.pop("dbt"):
        for positional in ("database1", "table1", "database2", "table2"):
            if kw[positional]:
                raise click.UsageError("--dbt takes no database or table arguments")
        models = dbt.dbt_diff(
            dbt.DbtDiffArgs(
                project_dir=kw["dbt_project_dir"],
                profiles_dir=kw["dbt_profiles_dir"],
                select=kw["select"],
                state=kw["state"],
                prod_database=kw["prod_database"],
                prod_schema=kw["prod_schema"],
                where=kw["where"],
                columns=list(kw["columns"]),
                algorithm=kw["algorithm"] or "auto",
                limit=kw["limit"],
            )
        )
        for md in models:
            click.echo(dbt.render_model(md))
        differs = any(md.differs for md in models)
        sys.exit(EXIT_DIFFERENCES if fail_on_diff and differs else EXIT_OK)

    for dbt_only in (
        "dbt_profiles_dir",
        "dbt_project_dir",
        "select",
        "state",
        "prod_database",
        "prod_schema",
    ):
        if kw.pop(dbt_only):
            raise click.UsageError(f"--{dbt_only.replace('_', '-')} needs --dbt")

    conf_path, run_name = kw.pop("conf"), kw.pop("run_name")
    values: dict[str, Any] = {}
    if conf_path:
        values.update(datadiff.load_conf(conf_path, run_name))
    elif run_name:
        raise click.UsageError("--run needs --conf")
    for ignored in (
        "threads",
        "verbose",
        "debug",
        "interactive",
        "no_tracking",
    ):
        values.pop(ignored, None)
    if values.pop("table_write_limit", None) is not None:
        raise ConfigError("table_write_limit (from --conf) is not supported by reconkit")
    for key, flag in datadiff.UNSUPPORTED_OPTIONS.items():
        if values.pop(key, None):
            raise ConfigError(f"{flag} (from --conf) is not supported by reconkit")
    positionals = [kw.pop(k) for k in ("database1", "table1", "database2", "table2")]
    if any(positionals):
        if not all(positionals):
            raise click.UsageError(
                "give all four of DB1_URI TABLE1 DB2_URI TABLE2, or none with --conf"
            )
        values["database1"] = datadiff.connection(positionals[0])
        values["table1"] = positionals[1]
        values["database2"] = datadiff.connection(positionals[2])
        values["table2"] = positionals[3]
    for key, value in kw.items():
        if value not in (None, False, ()):
            values[key] = list(value) if isinstance(value, tuple) else value
    for key in ("key_columns", "columns"):
        if isinstance(values.get(key), str):
            values[key] = [values[key]]
        elif key in values:
            values[key] = list(values[key])
    if not values.get("key_columns"):
        values.pop("key_columns", None)
    missing = [k for k in ("database1", "table1", "database2", "table2") if not values.get(k)]
    if missing:
        raise click.UsageError(
            "give DB1_URI TABLE1 DB2_URI TABLE2, or --conf with a run that names both sides"
        )
    try:
        args = datadiff.DataDiffArgs(**values, fail_on_diff=fail_on_diff)
    except TypeError as exc:
        raise ConfigError(str(exc)) from exc
    outcome = datadiff.run_data_diff(args)
    if verbose:
        err.print(
            f"Diffing using columns: key={outcome.columns[: len(args.key_columns)]} "
            f"compared={outcome.columns[len(args.key_columns) :]}",
            markup=False,
        )
    datadiff.print_outcome(outcome, args)
    if verbose:
        err.print(f"Duration: {outcome.result.duration_seconds:.2f} seconds.", markup=False)
    sys.exit(EXIT_DIFFERENCES if fail_on_diff and not outcome.result.is_match else EXIT_OK)


if __name__ == "__main__":  # pragma: no cover
    main()

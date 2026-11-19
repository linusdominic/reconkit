"""``reconkit data-diff --dbt``: compare each dbt model's dev build with production.

Reads the dbt project's own files, so dbt does not have to be installed:

* ``dbt_project.yml`` for the profile name, ``target-path`` and data-diff's settings under
  ``vars: data_diff: {prod_database, prod_schema, prod_custom_schema}``;
* ``profiles.yml`` for the connection (``env_var()`` is resolved; other Jinja is refused);
* ``target/manifest.json`` for each model's relation, columns, meta and tests;
* ``target/run_results.json`` to pick the models the last run built, unless ``--select``
  is given; ``--state`` points at a production manifest.

Primary keys come from, in order: model meta ``primary-key`` (a column or a list), column
meta or tags ``primary-key``, and ``unique`` / ``dbt_utils.unique_combination_of_columns``
tests, as in data-diff. Model meta ``datafold: datadiff: {filter, include_columns,
exclude_columns}`` is honoured, and ``reconkit:`` is accepted as an alias for
``datafold: datadiff:``.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from reconkit.adapters import Adapter, create_adapter
from reconkit.compat.datadiff import ALGORITHMS
from reconkit.compat.uri import Connection
from reconkit.diff.engine import TableSpec, diff_tables
from reconkit.diff.model import DiffOptions, DiffResult
from reconkit.errors import ConfigError, ReconkitError
from reconkit.types import TableRef

PK_TAG = "primary-key"
_BARE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# -- reading the project ----------------------------------------------------------------------

_ENV_VAR = re.compile(
    r"""\{\{\s*env_var\(\s*(['"])(?P<name>[^'"]+)\1\s*(?:,\s*(['"])(?P<default>[^'"]*)\3\s*)?\)"""
    r"""\s*(?:\|\s*(?P<filter>int|as_number|as_text|as_bool)\s*)?\}\}"""
)


def render(value: Any, env: Mapping[str, str]) -> Any:
    """Resolve dbt's ``{{ env_var('X', 'default') }}`` in every string of a structure."""
    if isinstance(value, str):
        cast: str | None = None

        def sub(match: re.Match[str]) -> str:
            nonlocal cast
            name, default = match.group("name"), match.group("default")
            if name in env:
                text = env[name]
            elif default is not None:
                text = default
            else:
                raise ConfigError(f"profiles.yml: environment variable {name} is not set")
            cast = match.group("filter")
            return text

        out = _ENV_VAR.sub(sub, value)
        if "{{" in out or "{%" in out:
            raise ConfigError(
                f"unsupported Jinja in dbt configuration: {value!r} (only env_var() is resolved)"
            )
        whole = _ENV_VAR.fullmatch(value.strip()) is not None
        if whole and cast in ("int", "as_number"):
            return int(out) if out.lstrip("-").isdigit() else float(out)
        if whole and cast == "as_bool":
            return out.strip().lower() in ("true", "1", "yes")
        return out
    if isinstance(value, dict):
        return {k: render(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, env) for v in value]
    return value


def find_project_dir(start: Path) -> Path:
    for directory in (start, *start.parents):
        if (directory / "dbt_project.yml").is_file():
            return directory
    raise ConfigError(f"no dbt_project.yml in {start} or any parent directory")


def _yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"not found: {path} (run dbt first, e.g. `dbt build`)") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON: {exc}") from exc


@dataclass(frozen=True)
class ProdSettings:
    database: str | None = None
    schema: str | None = None
    custom_schema: str | None = None


@dataclass
class DbtProject:
    root: Path
    profile: str
    target_path: Path
    prod: ProdSettings
    manifest: dict[str, Any]

    @classmethod
    def load(cls, project_dir: Path, env: Mapping[str, str]) -> DbtProject:
        root = find_project_dir(project_dir.resolve())
        doc = render(_yaml(root / "dbt_project.yml") or {}, env)
        profile = doc.get("profile")
        if not profile:
            raise ConfigError(f"{root / 'dbt_project.yml'} has no 'profile'")
        target_path = root / str(doc.get("target-path") or "target")
        dd = ((doc.get("vars") or {}).get("data_diff")) or {}
        prod = ProdSettings(
            database=dd.get("prod_database"),
            schema=dd.get("prod_schema"),
            custom_schema=dd.get("prod_custom_schema"),
        )
        manifest = _json(target_path / "manifest.json")
        return cls(root, str(profile), target_path, prod, manifest)

    @property
    def nodes(self) -> dict[str, dict[str, Any]]:
        nodes: dict[str, dict[str, Any]] = self.manifest.get("nodes") or {}
        return nodes

    def models(self) -> dict[str, dict[str, Any]]:
        return {uid: n for uid, n in self.nodes.items() if n.get("resource_type") == "model"}


def profiles_dir(explicit: str | None, project_root: Path, env: Mapping[str, str]) -> Path:
    """dbt's own search order: flag, ``DBT_PROFILES_DIR``, the project, then ``~/.dbt``."""
    if explicit:
        return Path(explicit).expanduser()
    if env.get("DBT_PROFILES_DIR"):
        return Path(env["DBT_PROFILES_DIR"]).expanduser()
    if (project_root / "profiles.yml").is_file():
        return project_root
    return Path("~/.dbt").expanduser()


def connection_from_profile(
    profiles: Path, profile: str, env: Mapping[str, str], target: str | None = None
) -> tuple[Connection, dict[str, Any]]:
    """Return the reconkit connection for a dbt profile's target, and the raw output."""
    path = profiles / "profiles.yml"
    doc = _yaml(path) or {}
    if profile not in doc:
        raise ConfigError(f"profile {profile!r} not found in {path}")
    entry = render(doc[profile], env)
    target = target or env.get("DBT_TARGET") or entry.get("target")
    outputs = entry.get("outputs") or {}
    if not target or target not in outputs:
        raise ConfigError(f"{path}: profile {profile!r} has no output {target!r}")
    out = dict(outputs[target])
    return _connection(out, f"{profile}.{target}"), out


def _connection(out: dict[str, Any], label: str) -> Connection:
    kind = str(out.get("type", "")).lower()

    def pick(*keys: str) -> Any:
        for key in keys:
            if out.get(key) not in (None, ""):
                return out[key]
        return None

    def copy(cfg: dict[str, Any], mapping: Mapping[str, Sequence[str]]) -> dict[str, Any]:
        for dest, sources in mapping.items():
            value = pick(*sources)
            if value is not None:
                cfg[dest] = value
        return cfg

    if kind == "postgres":
        return Connection(
            "postgres",
            copy(
                {},
                {
                    "host": ["host"],
                    "port": ["port"],
                    "user": ["user"],
                    "password": ["password", "pass"],
                    "dbname": ["dbname", "database"],
                    "sslmode": ["sslmode"],
                    "connect_timeout": ["connect_timeout"],
                },
            ),
        )
    if kind == "redshift":
        if str(out.get("method", "database")).lower() != "database":
            raise ConfigError(f"{label}: Redshift method {out['method']!r} is not supported")
        return Connection(
            "redshift",
            copy(
                {},
                {
                    "host": ["host"],
                    "port": ["port"],
                    "user": ["user"],
                    "password": ["password", "pass"],
                    "database": ["dbname", "database"],
                    "sslmode": ["sslmode"],
                },
            ),
        )
    if kind == "snowflake":
        return Connection(
            "snowflake",
            copy(
                {},
                {
                    "account": ["account"],
                    "user": ["user"],
                    "password": ["password"],
                    "role": ["role"],
                    "warehouse": ["warehouse"],
                    "database": ["database"],
                    "schema": ["schema"],
                    "authenticator": ["authenticator"],
                    "private_key_file": ["private_key_path"],
                    "private_key_file_pwd": ["private_key_passphrase"],
                },
            ),
        )
    if kind == "bigquery":
        method = str(out.get("method", "oauth")).lower()
        cfg = copy({}, {"project": ["execution_project", "project", "database"]})
        if pick("location"):
            cfg["location"] = out["location"]
        if method == "service-account":
            if not out.get("keyfile"):
                raise ConfigError(f"{label}: method service-account needs a keyfile")
            cfg["keyfile"] = str(Path(str(out["keyfile"])).expanduser())
        elif method != "oauth":
            raise ConfigError(
                f"{label}: BigQuery method {method!r} is not supported "
                "(use oauth or service-account)"
            )
        return Connection("bigquery", cfg)
    if kind == "duckdb":
        return Connection("duckdb", {"path": str(out.get("path") or ":memory:")})
    raise ConfigError(
        f"{label}: dbt adapter type {kind!r} is not supported "
        "(supported: postgres, redshift, snowflake, bigquery, duckdb)"
    )


# -- model selection --------------------------------------------------------------------------


def run_results_models(project: DbtProject) -> list[str]:
    doc = _json(project.target_path / "run_results.json")
    models = project.models()
    return [
        r["unique_id"]
        for r in doc.get("results") or []
        if r.get("status") == "success" and r.get("unique_id") in models
    ]


_GRAPH = re.compile(r"^(?P<up>(?P<updepth>\d*)\+)?(?P<body>.+?)(?P<down>\+(?P<downdepth>\d*))?$")


def _walk(start: Iterable[str], edges: Mapping[str, Sequence[str]], depth: int | None) -> set[str]:
    seen: set[str] = set()
    frontier = set(start)
    level = 0
    while frontier and (depth is None or level < depth):
        nxt = {n for f in frontier for n in edges.get(f, ()) if n not in seen}
        seen |= nxt
        frontier = nxt
        level += 1
    return seen


def _match_atom(atom: str, models: Mapping[str, dict[str, Any]]) -> set[str]:
    method, sep, value = atom.partition(":")
    if not sep:
        method, value = "", atom
    method = method.lower()
    hits = set()
    for uid, node in models.items():
        name = str(node.get("name", ""))
        if method == "tag":
            ok = value in (node.get("tags") or [])
        elif method == "path":
            path = str(node.get("original_file_path") or node.get("path") or "")
            ok = fnmatch.fnmatch(path, value) or path.startswith(value.rstrip("/") + "/")
            ok = ok or path == value
        elif method == "fqn":
            fqn = ".".join(node.get("fqn") or [])
            ok = fnmatch.fnmatch(fqn, value) or fqn == value
        elif method in ("", "model"):
            fqn = ".".join(node.get("fqn") or [])
            ok = fnmatch.fnmatch(name, value) or fqn == value or fqn.startswith(value + ".")
        else:
            raise ConfigError(
                f"selector method {method!r} is not supported without dbt; supported: "
                "names (with * wildcards), tag:, path:, fqn:, graph operators (+), "
                "unions (space) and intersections (comma)"
            )
        if ok:
            hits.add(uid)
    return hits


def select_models(project: DbtProject, selector: str) -> list[str]:
    """A subset of dbt's selection syntax, evaluated against the manifest.

    Supports model names (``*`` wildcards), ``tag:``, ``path:``, ``fqn:``, the graph
    operators ``+model``, ``model+``, ``2+model``, space for union and comma for
    intersection. Anything else (``state:``, ``@``, ``source:``...) is refused.
    """
    models = project.models()
    parents: dict[str, list[str]] = project.manifest.get("parent_map") or {}
    children: dict[str, list[str]] = project.manifest.get("child_map") or {}
    chosen: set[str] = set()
    for term in selector.split():
        if term.startswith("@"):
            raise ConfigError("the @ selector operator is not supported without dbt")
        inter: set[str] | None = None
        for atom in term.split(","):
            m = _GRAPH.match(atom)
            if not m:
                raise ConfigError(f"cannot parse selector {atom!r}")
            base = _match_atom(m.group("body"), models)
            if not base:
                raise ConfigError(f"selector {atom!r} matched no models")
            hits = set(base)
            if m.group("up"):
                depth = int(m.group("updepth")) if m.group("updepth") else None
                hits |= _walk(base, parents, depth)
            if m.group("down"):
                depth = int(m.group("downdepth")) if m.group("downdepth") else None
                hits |= _walk(base, children, depth)
            hits &= set(models)
            inter = hits if inter is None else inter & hits
        chosen |= inter or set()
    return sorted(chosen)


# -- per-model settings -----------------------------------------------------------------------


def _meta(node: Mapping[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    meta.update((node.get("config") or {}).get("meta") or {})
    meta.update(node.get("meta") or {})
    return meta


def _parse_concat(definition: str) -> list[str]:
    """Column names in a ``unique`` test's expression: ``a``, ``a || '-' || b``, ``concat(a, b)``.

    String literals and anything that is not a plain identifier are left out.
    """
    text = definition.strip()
    if text.lower().startswith("concat(") and text.endswith(")"):
        parts = text[7:-1].split(",")
    else:
        parts = text.split("||")
    cols = []
    for part in parts:
        token = part.strip().strip("()").strip()
        if not token or token[0] == "'":
            continue
        token = token.strip('"`')
        if _BARE.match(token):
            cols.append(token)
    return cols


def unique_columns(manifest: Mapping[str, Any]) -> dict[str, list[str]]:
    """Columns covered by uniqueness tests, per model unique id."""
    out: dict[str, list[str]] = {}
    for node in (manifest.get("nodes") or {}).values():
        if node.get("resource_type") != "test" or not node.get("test_metadata"):
            continue
        target = (
            node.get("attached_node") or ((node.get("depends_on") or {}).get("nodes") or [None])[0]
        )
        if not target or not str(target).startswith("model."):
            continue
        meta = node["test_metadata"]
        kwargs = meta.get("kwargs") or {}
        name = meta.get("name")
        if name == "unique" and kwargs.get("column_name"):
            cols = _parse_concat(str(kwargs["column_name"]))
        elif name == "unique_combination_of_columns":
            cols = [str(c) for c in kwargs.get("combination_of_columns") or []]
        else:
            continue
        # Prefer the narrowest unique key found.
        if cols and (target not in out or len(cols) < len(out[target])):
            out[target] = cols
    return out


def primary_key(node: Mapping[str, Any], uniques: Mapping[str, list[str]]) -> list[str]:
    meta = _meta(node)
    if PK_TAG in meta:
        value = meta[PK_TAG]
        cols = [value] if isinstance(value, str) else [str(c) for c in value or []]
        if cols:
            return cols
    columns: dict[str, Any] = node.get("columns") or {}
    from_meta = [n for n, c in columns.items() if PK_TAG in (c.get("meta") or {})]
    if from_meta:
        return from_meta
    from_tags = [n for n, c in columns.items() if PK_TAG in (c.get("tags") or [])]
    if from_tags:
        return from_tags
    return list(uniques.get(str(node.get("unique_id")), []))


@dataclass(frozen=True)
class ModelSettings:
    where: str | None = None
    include_columns: list[str] = field(default_factory=list)
    exclude_columns: list[str] = field(default_factory=list)


def model_settings(node: Mapping[str, Any]) -> ModelSettings:
    meta = _meta(node)
    cfg = (meta.get("datafold") or {}).get("datadiff") or meta.get("reconkit") or {}
    return ModelSettings(
        where=cfg.get("filter"),
        include_columns=list(cfg.get("include_columns") or []),
        exclude_columns=list(cfg.get("exclude_columns") or []),
    )


# -- relations --------------------------------------------------------------------------------


def _part(value: str, kind: str) -> str:
    if _BARE.match(value):
        return value
    if kind == "bigquery":
        return f"`{value}`"
    return '"' + value.replace('"', '""') + '"'


@dataclass(frozen=True)
class Relation:
    database: str | None
    schema: str
    identifier: str

    def render(self, kind: str, with_database: bool) -> str:
        parts = [self.database] if with_database and self.database else []
        parts += [self.schema, self.identifier]
        return ".".join(_part(p, kind) for p in parts)

    def __str__(self) -> str:
        return ".".join(p for p in (self.database, self.schema, self.identifier) if p)


def dev_relation(node: Mapping[str, Any]) -> Relation:
    return Relation(
        node.get("database"), str(node["schema"]), str(node.get("alias") or node["name"])
    )


def prod_relation(
    node: Mapping[str, Any],
    prod: ProdSettings,
    prod_manifest: Mapping[str, Any] | None,
    database_flag: str | None,
    schema_flag: str | None,
) -> Relation:
    """data-diff's rules for where the production copy of a model lives."""
    dev = dev_relation(node)
    if prod_manifest is not None:
        prod_node = (prod_manifest.get("nodes") or {}).get(node["unique_id"])
        if prod_node is None:
            raise ReconkitError(f"{node['name']} is not in the --state manifest")
        rel = dev_relation(prod_node)
    else:
        config = node.get("config") or {}
        database = config.get("database") or prod.database or dev.database
        if prod.schema:
            custom = config.get("schema")
            if custom:
                if not prod.custom_schema:
                    raise ConfigError(
                        f"model {node['name']} has a custom schema but dbt_project.yml has no "
                        "vars.data_diff.prod_custom_schema (or use --state)"
                    )
                schema = prod.custom_schema.replace("<custom_schema>", str(custom))
            else:
                schema = prod.schema
        else:
            schema = dev.schema
        rel = Relation(database, schema, dev.identifier)
    return Relation(database_flag or rel.database, schema_flag or rel.schema, rel.identifier)


# -- running ----------------------------------------------------------------------------------


@dataclass
class ModelDiff:
    name: str
    prod: Relation
    dev: Relation
    primary_keys: list[str]
    settings: ModelSettings
    result: DiffResult | None = None
    rows_prod: int = 0
    rows_dev: int = 0
    columns_only_prod: list[str] = field(default_factory=list)
    columns_only_dev: list[str] = field(default_factory=list)
    #: Columns in both whose declared type differs; left out of the value comparison,
    #: as data-diff did.
    type_changed: list[str] = field(default_factory=list)
    skipped: str | None = None
    error: str | None = None

    @property
    def differs(self) -> bool:
        if self.result is None:
            return self.error is not None
        return (
            not self.result.is_match
            or bool(self.columns_only_prod)
            or bool(self.columns_only_dev)
            or bool(self.type_changed)
        )


@dataclass
class DbtDiffArgs:
    project_dir: str | None = None
    profiles_dir: str | None = None
    select: str | None = None
    state: str | None = None
    prod_database: str | None = None
    prod_schema: str | None = None
    where: str | None = None
    columns: list[str] = field(default_factory=list)
    algorithm: str = "auto"
    limit: int | None = None


def _count(adapter: Adapter, table: str, where: str | None) -> int:
    sql = f"SELECT COUNT(*) FROM {adapter.dialect.table(TableRef.parse(table))}"
    if where:
        sql += f" WHERE {where}"
    return int(adapter.query(sql)[0][0])


def dbt_diff(
    args: DbtDiffArgs,
    *,
    env: Mapping[str, str] | None = None,
    adapter: Adapter | None = None,
) -> list[ModelDiff]:
    """Compare production and dev for every selected model. ``adapter`` is for tests."""
    env = os.environ if env is None else env
    project = DbtProject.load(Path(args.project_dir or "."), env)
    prod_manifest = _json(Path(args.state)) if args.state else None
    if prod_manifest is None and not (
        project.prod.database or project.prod.schema or args.prod_database or args.prod_schema
    ):
        raise ConfigError(
            "no production location: set vars.data_diff.prod_database and/or prod_schema in "
            "dbt_project.yml, pass --prod-database/--prod-schema, or pass --state"
        )
    uids = select_models(project, args.select) if args.select else run_results_models(project)
    if not uids:
        raise ConfigError("no models selected (run dbt first, or pass --select)")
    conn, _ = connection_from_profile(
        profiles_dir(args.profiles_dir, project.root, env), project.profile, env
    )
    uniques = unique_columns(project.manifest)
    own = adapter is None
    db = adapter or create_adapter(conn.type, conn.config, "dbt")
    diffs: list[ModelDiff] = []
    try:
        for uid in uids:
            node = project.nodes[uid]
            materialized = (node.get("config") or {}).get("materialized")
            dev = dev_relation(node)
            settings = model_settings(node)
            md = ModelDiff(str(node["name"]), dev, dev, primary_key(node, uniques), settings)
            if materialized == "ephemeral":
                md.skipped = "ephemeral model; nothing to compare"
                diffs.append(md)
                continue
            try:
                md.prod = prod_relation(
                    node, project.prod, prod_manifest, args.prod_database, args.prod_schema
                )
            except ReconkitError as exc:
                md.error = str(exc)
                diffs.append(md)
                continue
            if not md.primary_keys:
                md.skipped = "unknown primary key; add uniqueness tests, meta, or tags"
                diffs.append(md)
                continue
            _run_model(md, db, conn.type, args)
            diffs.append(md)
    finally:
        if own:
            db.close()
    return diffs


def _run_model(md: ModelDiff, db: Adapter, kind: str, args: DbtDiffArgs) -> None:
    with_db = kind in ("snowflake", "bigquery") or md.prod.database != md.dev.database
    prod_table = md.prod.render(kind, with_db)
    dev_table = md.dev.render(kind, with_db)
    try:
        prod_info = db.get_columns(TableRef.parse(prod_table))
        dev_info = db.get_columns(TableRef.parse(dev_table))
        prod_cols = [c.name for c in prod_info]
        dev_cols = [c.name for c in dev_info]
        if not prod_cols:
            raise ReconkitError(f"production table {md.prod} not found")
        if not dev_cols:
            raise ReconkitError(f"dev table {md.dev} not found")
        lower_dev = {c.lower() for c in dev_cols}
        lower_prod = {c.lower() for c in prod_cols}
        md.columns_only_prod = [c for c in prod_cols if c.lower() not in lower_dev]
        md.columns_only_dev = [c for c in dev_cols if c.lower() not in lower_prod]
        include = args.columns or md.settings.include_columns
        exclude = [] if args.columns else md.settings.exclude_columns
        mutual = [c for c in prod_cols if c.lower() in lower_dev]
        dev_types = {c.name.lower(): c.type.native.lower() for c in dev_info}
        md.type_changed = [
            c.name
            for c in prod_info
            if c.name.lower() in dev_types and c.type.native.lower() != dev_types[c.name.lower()]
        ]
        changed_types = {c.lower() for c in md.type_changed}
        mutual = [c for c in mutual if c.lower() not in changed_types]
        keys = {k.lower() for k in md.primary_keys}
        if include:
            wanted = {c.lower() for c in include}
            columns = [c for c in mutual if c.lower() in wanted and c.lower() not in keys]
        else:
            columns = [c for c in mutual if c.lower() not in keys]
        dropped = {c.lower() for c in exclude}
        columns = [c for c in columns if c.lower() not in dropped]
        where = args.where or md.settings.where
        options = DiffOptions(strategy=ALGORITHMS[args.algorithm], limit=args.limit)
        md.result = diff_tables(
            TableSpec(db, prod_table, where=where),
            TableSpec(db, dev_table, where=where),
            md.primary_keys,
            columns=columns,
            options=options,
        )
        r = md.result
        md.rows_prod = (
            r.left.row_count if r.left.row_count is not None else _count(db, prod_table, where)
        )
        md.rows_dev = (
            r.right.row_count if r.right.row_count is not None else _count(db, dev_table, where)
        )
    except ReconkitError as exc:
        md.error = str(exc)


# -- output -----------------------------------------------------------------------------------


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[str(h) for h in headers], *[[str(c) for c in r] for r in rows]]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(cells[0], widths, strict=True)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in cells[1:]:
        lines.append("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip())
    return "\n".join(lines)


def _set(names: Sequence[str]) -> str:
    return "{" + ", ".join(repr(n) for n in sorted(names)) + "}"


def _signed(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


def render_model(md: ModelDiff) -> str:
    head = f"{md.prod} <> {md.dev}"
    lines = [head]
    if md.skipped:
        lines.append(f"Skipped: {md.skipped}")
        return "\n".join(lines) + "\n"
    if md.error:
        lines.append(f"Error: {md.error}")
        return "\n".join(lines) + "\n"
    lines.append(f"Primary Keys: {md.primary_keys}")
    if md.settings.where:
        lines.append(f"Where Filter: {md.settings.where!r}")
    if md.settings.include_columns:
        lines.append(f"Included Columns: {md.settings.include_columns}")
    if md.settings.exclude_columns:
        lines.append(f"Excluded Columns: {md.settings.exclude_columns}")
    if md.columns_only_prod:
        lines.append(
            f"Columns removed [-{len(md.columns_only_prod)}]: {_set(md.columns_only_prod)}"
        )
    if md.columns_only_dev:
        lines.append(f"Columns added [+{len(md.columns_only_dev)}]: {_set(md.columns_only_dev)}")
    if md.type_changed:
        lines.append(f"Type changed [{len(md.type_changed)}]: {_set(md.type_changed)}")
    r = md.result
    assert r is not None
    if not r.diffs and not r.truncated:
        lines.append("No row differences")
    else:
        added, removed, changed = r.missing_in_left, r.missing_in_right, r.changed
        unchanged = md.rows_prod - removed - changed
        total_dev = f"{md.rows_dev} [{_signed(md.rows_dev - md.rows_prod)}]"
        lines.append("")
        lines.append(
            _table(
                ["rows", "PROD", "<>", "DEV"],
                [
                    ["Total", md.rows_prod, "", total_dev],
                    ["Added", "", _signed(added), ""],
                    ["Removed", "", _signed(-removed), ""],
                    ["Different", "", changed, ""],
                    ["Unchanged", "", unchanged, ""],
                ],
            )
        )
        counts = r.column_change_counts()
        compared = sorted(r.compared_columns)
        if compared:
            lines.append("")
            lines.append(
                _table(["columns", "# diff values"], [[c, counts.get(c, 0)] for c in compared])
            )
    if r.duplicate_keys:
        lines.append(f"\nWarning: {len(r.duplicate_keys)} duplicate key value(s) were not compared")
    if r.truncated:
        lines.append("\nStopped at --limit; counts are lower bounds")
    return "\n".join(lines) + "\n"

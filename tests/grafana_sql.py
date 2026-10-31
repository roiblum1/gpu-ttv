"""
Render Grafana Postgres panel SQL the way Grafana does, so tests can execute dashboard queries.

Two steps, as in Grafana: the frontend interpolates template variables, then the backend
expands `$__` macros (pkg/tsdb/grafana-postgresql-datasource/macros.go). Test helper only.
Fixture-tested 2026-10-31: for every macro here, `render` equals the `executedQueryString`
Grafana 12.4.14 reports from `/api/ds/query`, including Grafana's limits (no `)` inside macro
arguments; `$__timeGroup(...)` directly followed by `,` gets `AS "time"`).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

POSTGRES_TYPE = "grafana-postgresql-datasource"
POSTGRES_UID = "ttv-postgres"
ALL = "$__all"

# Grafana's templateSrv pattern: $var, [[var:fmt]], ${var.field:fmt}.
_VARIABLE = re.compile(
    r"\$(\w+)|\[\[(\w+?)(?::(\w+))?\]\]|\$\{(\w+)(?:\.([^:^\}]+))?(?::([^\}]+))?\}"
)
# Grafana's macro pattern: arguments cannot contain ")" (nested calls are not supported).
_MACRO = re.compile(r"\$([_a-zA-Z0-9]+)\(([^\)]*)\)")
_INTERVAL = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w)")
_RELATIVE = re.compile(r"now(?:([+-])(\d+)([smhdwy]))?")
_LEFTOVER = re.compile(r"\$\{[^}]*\}|\[\[\w+(?::\w+)?\]\]|\$__\w+|\$[A-Za-z_]\w*")
_UNIT_SECONDS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "y": 31536000,
}


@dataclass(frozen=True)
class Variable:
    """
    A dashboard variable's current value, as Grafana interpolates it into SQL.

    Args:
        name (str): Variable name.
        value (str | tuple[str, ...]): One value, several values, or a custom All value.
        quoted (bool): True for multi-value or include-All variables (values become 'a','b').
        custom_all (bool): True when `value` is the variable's custom All value (inserted raw).
    """

    name: str
    value: str | tuple[str, ...]
    quoted: bool = False
    custom_all: bool = False


@dataclass(frozen=True)
class QueryContext:
    """
    Everything Grafana substitutes into one query.

    Args:
        time_from (datetime): Start of the dashboard time range (aware).
        time_to (datetime): End of the dashboard time range (aware).
        variables (Mapping[str, Variable]): Current dashboard variables by name.
        interval (timedelta | None): `$__interval`; None computes it like Grafana.
        max_data_points (int): Resolution used to compute the interval (panel width in Grafana).
        min_interval (str | None): Panel "Min interval" (e.g. "5m"), the interval's low limit.
    """

    time_from: datetime
    time_to: datetime
    variables: Mapping[str, Variable] = field(default_factory=dict)
    interval: timedelta | None = None
    max_data_points: int = 1000
    min_interval: str | None = None

    def interval_ms(self) -> int:
        """
        Return `$__interval_ms` for this context.

        Returns:
            int: The interval in milliseconds.
        """
        if self.interval is not None:
            return int(self.interval / timedelta(milliseconds=1))
        return calculate_interval_ms(
            self.time_from, self.time_to, self.max_data_points, self.min_interval
        )


@dataclass(frozen=True)
class PanelQuery:
    """
    One SQL query found in a dashboard.

    Args:
        dashboard (str): Dashboard UID.
        panel_id (int | None): Panel id, or None for a variable query.
        title (str): Panel title, or `$<name>` for a variable query.
        ref_id (str): Target refId, or the variable name.
        sql (str): Raw SQL as stored in the dashboard.
        format (str): Target format (`table` or `time_series`).
        min_interval (str | None): Panel "Min interval", if set.
        max_data_points (int | None): Panel max data points, if set.
    """

    dashboard: str
    panel_id: int | None
    title: str
    ref_id: str
    sql: str
    format: str = "table"
    min_interval: str | None = None
    max_data_points: int | None = None

    @property
    def label(self) -> str:
        """
        Return a short id for test names, e.g. `ttv-queues#7A` or `ttv-queues$site`.

        Returns:
            str: The label.
        """
        if self.panel_id is None:
            return f"{self.dashboard}${self.ref_id}"
        return f"{self.dashboard}#{self.panel_id}{self.ref_id}"


def iter_panels(dashboard: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """
    Yield every panel, including rows and panels nested in collapsed rows.

    Args:
        dashboard (Mapping[str, Any]): Dashboard JSON.

    Yields:
        dict[str, Any]: Each panel, depth-first.
    """
    stack = list(reversed(dashboard.get("panels", [])))
    while stack:
        panel = stack.pop()
        yield panel
        stack.extend(reversed(panel.get("panels", [])))


def _is_postgres(datasource: Any) -> bool:
    """
    Tell whether a datasource reference points at the ledger Postgres.

    Args:
        datasource (Any): A panel or target `datasource` value.

    Returns:
        bool: True for the Postgres type or the `ttv-postgres` uid.
    """
    if isinstance(datasource, Mapping):
        return datasource.get("type") == POSTGRES_TYPE or datasource.get("uid") == POSTGRES_UID
    return datasource == POSTGRES_UID


def panel_queries(dashboard: Mapping[str, Any]) -> Iterator[PanelQuery]:
    """
    Yield the Postgres targets of every panel (code-mode `rawSql`).

    Args:
        dashboard (Mapping[str, Any]): Dashboard JSON.

    Yields:
        PanelQuery: One per Postgres target; builder-mode targets yield an empty `sql`.
    """
    for panel in iter_panels(dashboard):
        for target in panel.get("targets", []):
            if not _is_postgres(target.get("datasource") or panel.get("datasource")):
                continue
            yield PanelQuery(
                dashboard=dashboard["uid"],
                panel_id=panel["id"],
                title=panel.get("title", ""),
                ref_id=target.get("refId", "A"),
                sql=target.get("rawSql") or "",
                format=target.get("format", "table"),
                min_interval=panel.get("interval") or None,
                max_data_points=panel.get("maxDataPoints"),
            )


def variable_queries(dashboard: Mapping[str, Any]) -> Iterator[PanelQuery]:
    """
    Yield the SQL of every Postgres query variable.

    Args:
        dashboard (Mapping[str, Any]): Dashboard JSON.

    Yields:
        PanelQuery: One per query variable, with `panel_id` None.
    """
    for var in dashboard.get("templating", {}).get("list", []):
        if var.get("type") != "query" or not _is_postgres(var.get("datasource")):
            continue
        yield PanelQuery(
            dashboard=dashboard["uid"],
            panel_id=None,
            title=f"${var['name']}",
            ref_id=var["name"],
            sql=_variable_sql(var),
        )


def _variable_sql(var: Mapping[str, Any]) -> str:
    """
    Return a query variable's SQL (stored as a string or as `{"rawSql": ...}`).

    Args:
        var (Mapping[str, Any]): Variable JSON.

    Returns:
        str: The SQL.
    """
    query = var.get("query")
    if isinstance(query, Mapping):
        return str(query.get("rawSql") or query.get("query") or "")
    return str(query or "")


def parse_time(expr: str, now: datetime) -> datetime:
    """
    Resolve a dashboard time such as `now`, `now-7d` or an ISO timestamp.

    Args:
        expr (str): Grafana time expression (no rounding such as `now/d`).
        now (datetime): The current time.

    Returns:
        datetime: The resolved time.

    Raises:
        ValueError: For an expression this helper does not support.
    """
    m = _RELATIVE.fullmatch(expr)
    if m is None:
        try:
            return datetime.fromisoformat(expr)
        except ValueError as exc:
            raise ValueError(f"unsupported Grafana time {expr!r}") from exc
    if m.group(1) is None:
        return now
    delta = timedelta(seconds=int(m.group(2)) * _UNIT_SECONDS[m.group(3)])
    return now - delta if m.group(1) == "-" else now + delta


def time_range(dashboard: Mapping[str, Any], now: datetime) -> tuple[datetime, datetime]:
    """
    Return a dashboard's default time range.

    Args:
        dashboard (Mapping[str, Any]): Dashboard JSON.
        now (datetime): The current time.

    Returns:
        tuple[datetime, datetime]: (from, to).
    """
    t = dashboard.get("time") or {"from": "now-6h", "to": "now"}
    return parse_time(t["from"], now), parse_time(t["to"], now)


def parse_interval(text: str) -> timedelta:
    """
    Parse a Grafana interval such as `5m`, `1h30m`, `1d` or `500ms` (gtime.ParseInterval).

    Args:
        text (str): Interval text; a leading `>` (minimum) is ignored.

    Returns:
        timedelta: The interval.

    Raises:
        ValueError: If the text is not an interval.
    """
    text = text.strip().strip("'").lstrip(">")
    parts = _INTERVAL.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError(f"error parsing interval {text!r}")
    return timedelta(seconds=sum(float(n) * _UNIT_SECONDS[u] for n, u in parts))


def _round_interval_ms(ms: float) -> int:
    """
    Round a raw interval the way Grafana's `rangeutil.roundInterval` does.

    Args:
        ms (float): Raw interval in milliseconds.

    Returns:
        int: The rounded interval in milliseconds.
    """
    steps = (
        (15, 10), (35, 20), (75, 50), (150, 100), (350, 200), (750, 500), (1500, 1000),
        (3500, 2000), (7500, 5000), (12500, 10000), (17500, 15000), (25000, 20000),
        (45000, 30000), (90000, 60000), (210000, 120000), (450000, 300000), (750000, 600000),
        (1050000, 900000), (1500000, 1200000), (2700000, 1800000), (5400000, 3600000),
        (9000000, 7200000), (16200000, 10800000), (32400000, 21600000), (86400000, 43200000),
        (604800000, 86400000), (1814400000, 604800000), (3628800000, 2592000000),
    )  # fmt: skip
    for limit, rounded in steps:
        if ms < limit:
            return rounded
    return 31536000000


def calculate_interval_ms(
    time_from: datetime, time_to: datetime, max_data_points: int, min_interval: str | None
) -> int:
    """
    Compute `$__interval_ms` like Grafana's `rangeutil.calculateInterval`.

    Args:
        time_from (datetime): Range start.
        time_to (datetime): Range end.
        max_data_points (int): Resolution (panel width or max data points).
        min_interval (str | None): Low limit, e.g. the panel's "Min interval".

    Returns:
        int: Interval in milliseconds.
    """
    span_ms = (time_to - time_from) / timedelta(milliseconds=1)
    interval = _round_interval_ms(span_ms / max(max_data_points, 1))
    if min_interval:
        interval = max(interval, int(parse_interval(min_interval) / timedelta(milliseconds=1)))
    return interval


def format_interval(ms: int) -> str:
    """
    Format an interval like Grafana's `gtime.FormatInterval` (largest unit only).

    Args:
        ms (int): Interval in milliseconds.

    Returns:
        str: e.g. `5m`, `1h`, `2d`.
    """
    for unit, size in (("y", 31536000000), ("d", 86400000), ("h", 3600000), ("m", 60000)):
        if ms >= size:
            return f"{ms // size}{unit}"
    if ms >= 1000:
        return f"{ms // 1000}s"
    return f"{max(ms, 1)}ms"


def _rfc3339(t: datetime) -> str:
    """
    Format a time like Go's `time.RFC3339Nano` in UTC (trailing fraction zeros trimmed).

    Args:
        t (datetime): An aware datetime.

    Returns:
        str: e.g. `2026-10-01T00:00:00Z`.
    """
    t = t.astimezone(UTC)
    frac = f"{t.microsecond:06d}".rstrip("0")
    return t.strftime("%Y-%m-%dT%H:%M:%S") + (f".{frac}" if frac else "") + "Z"


def _unix_nano(t: datetime) -> int:
    """
    Return Go's `time.UnixNano()` for an aware datetime, without float rounding.

    Args:
        t (datetime): An aware datetime.

    Returns:
        int: Nanoseconds since the Unix epoch.
    """
    return (t - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1) * 1000


def _go_float(x: float) -> str:
    """
    Format a float like Go's `%v` (shortest `%g`, exponent from 1e+06), as macros print seconds.

    Args:
        x (float): Value.

    Returns:
        str: e.g. `300`, `0.5`, `1e+06`.
    """
    if x == 0:
        return "0"
    exp = math.floor(math.log10(abs(x)))
    if exp < -4 or exp >= 6:
        mantissa, _, power = f"{x:e}".partition("e")
        mantissa = mantissa.rstrip("0").rstrip(".")
        sign, digits = power[0], power[1:].lstrip("0").rjust(2, "0")
        return f"{mantissa}e{sign}{digits}"
    return repr(float(x)).removesuffix(".0")


def _quote(value: str) -> str:
    """
    Quote a value as an SQL string literal (Grafana's `quoteLiteral`).

    Args:
        value (str): Raw value.

    Returns:
        str: `'value'` with single quotes doubled.
    """
    return "'" + value.replace("'", "''") + "'"


def _format_value(var: Variable, fmt: str | None) -> str:
    """
    Format a variable value for SQL the way the Grafana SQL datasource does.

    Args:
        var (Variable): The variable.
        fmt (str | None): Explicit format (`${var:fmt}`), or None for the datasource default.

    Returns:
        str: The text to insert.

    Raises:
        NotImplementedError: For a format this helper does not implement.
    """
    values = (var.value,) if isinstance(var.value, str) else var.value
    if var.custom_all and fmt not in ("text", "percentencode"):
        return str(var.value)
    if fmt is None:
        if var.quoted or not isinstance(var.value, str):
            return ",".join(_quote(v) for v in values)
        return var.value.replace("'", "''")
    if fmt in ("raw", "csv"):
        return ",".join(values)
    if fmt == "sqlstring":
        return ",".join(_quote(v) for v in values)
    if fmt == "singlequote":
        return ",".join("'" + v.replace("'", "\\'") + "'" for v in values)
    if fmt == "text":
        return "All" if var.custom_all else " + ".join(values)
    raise NotImplementedError(f"variable format {fmt!r} for ${var.name}")


def _builtin(name: str, fmt: str | None, ctx: QueryContext) -> str | None:
    """
    Return a Grafana global variable (`__interval`, `__from`, ...), or None if not one.

    Args:
        name (str): Variable name.
        fmt (str | None): Explicit format, e.g. `date:iso` for `${__from:date:iso}`.
        ctx (QueryContext): Query context.

    Returns:
        str | None: The value, or None when `name` is not a supported global variable.

    Raises:
        NotImplementedError: For an unsupported format of `__from`/`__to`.
    """
    interval_ms = ctx.interval_ms()
    span_ms = int((ctx.time_to - ctx.time_from) / timedelta(milliseconds=1))
    simple = {
        "__interval": format_interval(interval_ms),
        "__interval_ms": str(interval_ms),
        "__range": format_interval(span_ms),
        "__range_s": str(span_ms // 1000),
        "__range_ms": str(span_ms),
    }
    if name in simple:
        return simple[name]
    if name not in ("__from", "__to"):
        return None
    t = ctx.time_from if name == "__from" else ctx.time_to
    if fmt is None:
        return str(int(t.timestamp() * 1000))
    if fmt in ("date", "date:iso"):
        return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if fmt == "date:seconds":
        return str(int(t.timestamp()))
    raise NotImplementedError(f"format {fmt!r} for ${name}")


def interpolate_variables(sql: str, ctx: QueryContext) -> str:
    """
    Replace template variables as Grafana's frontend does; unknown names stay untouched.

    Args:
        sql (str): Raw SQL.
        ctx (QueryContext): Query context.

    Returns:
        str: SQL with variables replaced.
    """

    def repl(m: re.Match[str]) -> str:
        """
        Replace one variable reference.

        Args:
            m (re.Match[str]): The match.

        Returns:
            str: Replacement text, or the original text for an unknown name.
        """
        name = m.group(1) or m.group(2) or m.group(4)
        fmt = m.group(3) or m.group(6)
        builtin = _builtin(name, fmt, ctx)
        if builtin is not None:
            return builtin
        var = ctx.variables.get(name)
        if var is None or m.group(5):
            return m.group(0)
        return _format_value(var, fmt)

    return _VARIABLE.sub(repl, sql)


def _macro(name: str, args: list[str], ctx: QueryContext) -> str:
    """
    Expand one Postgres datasource macro.

    Args:
        name (str): Macro name without `$`, e.g. `__timeFilter`.
        args (list[str]): Arguments, split on commas and trimmed as Grafana does.
        ctx (QueryContext): Query context.

    Returns:
        str: The expansion.

    Raises:
        ValueError: For an unknown macro or wrong arguments (Grafana fails the query too).
    """
    t0, t1 = ctx.time_from.astimezone(UTC), ctx.time_to.astimezone(UTC)
    if name in ("__time", "__timeEpoch", "__timeFilter", "__unixEpochFilter") and not args[0]:
        raise ValueError(f"missing time column argument for macro {name}")
    match name:
        case "__time":
            return f'{args[0]} AS "time"'
        case "__timeEpoch":
            return f'extract(epoch from {args[0]}) as "time"'
        case "__timeFilter":
            return f"{args[0]} BETWEEN '{_rfc3339(t0)}' AND '{_rfc3339(t1)}'"
        case "__timeFrom":
            return f"'{_rfc3339(t0)}'"
        case "__timeTo":
            return f"'{_rfc3339(t1)}'"
        case "__timeGroup" | "__unixEpochGroup":
            if len(args) < 2:
                raise ValueError(f"macro {name} needs time column and interval")
            secs = _go_float(parse_interval(args[1]).total_seconds())
            if name == "__unixEpochGroup":
                return f"floor(({args[0]})/{secs})*{secs}"
            return f"floor(extract(epoch from {args[0]})/{secs})*{secs}"
        case "__timeGroupAlias" | "__unixEpochGroupAlias":
            return _macro(name.removesuffix("Alias"), args, ctx) + ' AS "time"'
        case "__unixEpochFilter":
            a, b = int(t0.timestamp()), int(t1.timestamp())
            return f"{args[0]} >= {a} AND {args[0]} <= {b}"
        case "__unixEpochNanoFilter":
            return f"{args[0]} >= {_unix_nano(t0)} AND {args[0]} <= {_unix_nano(t1)}"
        case "__unixEpochNanoFrom":
            return str(_unix_nano(t0))
        case "__unixEpochNanoTo":
            return str(_unix_nano(t1))
        case _:
            raise ValueError(f"unknown macro {name!r}")


def expand_macros(sql: str, ctx: QueryContext) -> str:
    """
    Expand `$__` macros as Grafana's Postgres backend does (after variable interpolation).

    Args:
        sql (str): SQL with variables already interpolated.
        ctx (QueryContext): Query context.

    Returns:
        str: Executable SQL.
    """
    interval_ms = ctx.interval_ms()
    sql = sql.replace("$__interval_ms", str(interval_ms))
    sql = sql.replace("$__interval", format_interval(interval_ms))
    sql = sql.replace("$__unixEpochFrom()", str(int(ctx.time_from.timestamp())))
    sql = sql.replace("$__unixEpochTo()", str(int(ctx.time_to.timestamp())))

    def repl(m: re.Match[str]) -> str:
        """
        Expand one macro call; `$__timeGroup(...)` directly followed by `,` gets an alias.

        Args:
            m (re.Match[str]): The match.

        Returns:
            str: The expansion.
        """
        name = m.group(1)
        if name == "__timeGroup" and sql[m.end() : m.end() + 1] == ",":
            name = "__timeGroupAlias"
        return _macro(name, [a.strip(" ") for a in m.group(2).split(",")], ctx)

    return _MACRO.sub(repl, sql)


def render(sql: str, ctx: QueryContext) -> str:
    """
    Turn stored dashboard SQL into the SQL Grafana sends to Postgres.

    Args:
        sql (str): Raw SQL from a panel target or a query variable.
        ctx (QueryContext): Time range, interval and variables.

    Returns:
        str: Executable SQL.
    """
    return expand_macros(interpolate_variables(sql, ctx), ctx)


def leftover_placeholders(sql: str) -> list[str]:
    """
    List `$name`, `${...}`, `[[...]]` or `$__` tokens left after `render` (unknown variables).

    Args:
        sql (str): Rendered SQL.

    Returns:
        list[str]: Leftover tokens; empty when everything was substituted.
    """
    return _LEFTOVER.findall(sql)


def _custom_options(var: Mapping[str, Any]) -> list[str]:
    """
    Return the option values of a custom or interval variable (`a,b` or `label : value`).

    Args:
        var (Mapping[str, Any]): Variable JSON.

    Returns:
        list[str]: Option values.
    """
    out = []
    for item in re.split(r"(?<!\\),", str(var.get("query") or "")):
        item = item.strip().replace("\\,", ",")
        if item:
            out.append(item.split(" : ", 1)[-1])
    return out


def _options(
    var: Mapping[str, Any], ctx: QueryContext, resolve: Callable[[str], list[str]] | None
) -> list[str]:
    """
    List a variable's options: its custom list, or its query rendered and run by `resolve`.

    Args:
        var (Mapping[str, Any]): Variable JSON.
        ctx (QueryContext): Time range and the variables resolved before this one.
        resolve (Callable[[str], list[str]] | None): Runs SQL and returns the first column.

    Returns:
        list[str]: Options.

    Raises:
        ValueError: If the options need a resolver and none was given.
    """
    if var.get("type") in ("custom", "interval"):
        return _custom_options(var)
    if resolve is None:
        raise ValueError(f"${var['name']} needs a resolver to list its options")
    return resolve(render(_variable_sql(var), ctx))


def dashboard_variables(
    dashboard: Mapping[str, Any],
    values: Mapping[str, str | Sequence[str]] | None = None,
    *,
    ctx: QueryContext | None = None,
    resolve: Callable[[str], list[str]] | None = None,
) -> dict[str, Variable]:
    """
    Build each variable's current value: an override, else All, else the saved or first option.

    Variables resolve in list order, so a chained variable's query sees the ones before it.

    Args:
        dashboard (Mapping[str, Any]): Dashboard JSON.
        values (Mapping[str, str | Sequence[str]] | None): Overrides by name (`ALL` selects All).
        ctx (QueryContext | None): Time range for variable queries (default: the dashboard's).
        resolve (Callable[[str], list[str]] | None): Runs rendered variable SQL, returns options.

    Returns:
        dict[str, Variable]: Variables by name (datasource and ad hoc variables are skipped).
    """
    values = values or {}
    base = ctx or QueryContext(*time_range(dashboard, datetime.now(UTC)))
    out: dict[str, Variable] = {}
    for var in dashboard.get("templating", {}).get("list", []):
        name, kind = var["name"], var.get("type")
        quoted = bool(var.get("multi") or var.get("includeAll"))
        scoped = QueryContext(base.time_from, base.time_to, dict(out), base.interval)
        given = values.get(name)
        if kind in ("constant", "textbox"):
            given = str(var.get("query") or "") if given is None else given
        elif kind not in ("query", "custom", "interval"):
            continue
        elif given == ALL or (given is None and var.get("includeAll")):
            if var.get("allValue"):
                out[name] = Variable(name, str(var["allValue"]), quoted, custom_all=True)
            else:
                out[name] = Variable(name, tuple(_options(var, scoped, resolve)), quoted)
            continue
        elif given is None:
            current = (var.get("current") or {}).get("value")
            if current in (None, "", [], ALL, [ALL]):
                opts = _options(var, scoped, resolve)
                current = opts[0] if opts else ""
            given = current
        value = given if isinstance(given, str) else tuple(given)
        out[name] = Variable(name, value, quoted)
    return out

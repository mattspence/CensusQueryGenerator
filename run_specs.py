"""Build one master query from a list of small query specs.

Specs live in query_specs.csv, one row per output row of the final table.
Each spec becomes a subquery labeled with its table_id / row_id / row_desc,
then all subqueries are combined with UNION ALL BY NAME and run as a single
statement.

Spec CSV columns: table_id, row_id, row_desc, sql, and optional universe.
Multi-line SQL is fine — quote the field and embed real newlines; Excel and
the csv module both round-trip this correctly.

Usage:
    python run_specs.py                         # run with defaults
    python run_specs.py --specs other.csv       # different spec file
    python run_specs.py --source data/2020.csv  # different input records
    python run_specs.py --check                 # validate only, run nothing
    python run_specs.py --print-sql             # dump the master query
    python run_specs.py --geo StateGEOID        # fill the {geo} placeholder
    python run_specs.py --geo CountyGEOID       # same specs, finer geography
    python run_specs.py --geo CountyGEOID --spine counties.csv
                                                # zero-fill every line for
                                                # every geography in the spine
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, fields, replace
from pathlib import Path

import duckdb

# Label columns prepended to every subquery, in output order.
LABEL_COLUMNS = ("table_id", "row_id", "row_desc", "universe", "geo_level")

# Specs generated with a deferred geography carry this token; --geo fills it.
# The resulting column comes out of the subquery, so it is not a label column.
GEO_PLACEHOLDER = "{geo}"
GEO_COLUMN = "geoid"


# --------------------------------------------------------------------------
# Spec model
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class QuerySpec:
    table_id: str
    row_id: int
    row_desc: str
    sql: str
    universe: str | None = None

    @property
    def key(self) -> str:
        return f"{self.table_id}/{self.row_id}"


@dataclass
class SourceConfig:
    """Where the individual records live. Set from the command line, since the
    spec CSV holds only the per-row query definitions."""

    path: str
    view: str = "recs"
    materialize: bool = True


class SpecError(Exception):
    """Raised with every problem found, not just the first one."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(
            f"{len(problems)} problem(s) in the spec file:\n  - "
            + "\n  - ".join(problems)
        )


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_specs(path: Path) -> list[QuerySpec]:
    """Parse the spec CSV into typed objects. Structural errors are collected.

    utf-8-sig strips the byte-order mark Excel writes, which would otherwise
    turn the first header into '\ufefftable_id' and fail the header check.
    """
    problems: list[str] = []
    allowed = {f.name for f in fields(QuerySpec)}
    required = {"table_id", "row_id", "row_desc", "sql"}
    specs: list[QuerySpec] = []

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = set(reader.fieldnames or [])

        missing = required - header
        if missing:
            raise SpecError([f"{path}: missing column(s) {sorted(missing)}"])
        unknown = header - allowed
        if unknown:
            # Ignored rather than fatal: a hand-maintained spreadsheet usually
            # grows scratch columns (notes, source, reviewed_by). A misspelled
            # required column still fails the check above.
            print(
                f"warning: {path}: ignoring extra column(s) {sorted(unknown)}",
                file=sys.stderr,
            )

        for raw in reader:
            where = f"line {reader.line_num}"

            # Skip blank lines and '#' comment rows so the file stays annotatable.
            if not any((v or "").strip() for v in raw.values()):
                continue
            if (raw.get("table_id") or "").lstrip().startswith("#"):
                continue

            if any(v is None for v in raw.values()):
                problems.append(f"{where}: too few fields for the header")
                continue

            blank = sorted(k for k in required if not (raw[k] or "").strip())
            if blank:
                problems.append(f"{where}: empty required field(s) {blank}")
                continue

            row_id = (raw["row_id"] or "").strip()
            if not re.fullmatch(r"-?\d+", row_id):
                problems.append(f"{where}: row_id must be an integer, got {row_id!r}")
                continue

            universe = (raw.get("universe") or "").strip()
            specs.append(
                QuerySpec(
                    table_id=raw["table_id"].strip(),
                    row_id=int(row_id),
                    row_desc=raw["row_desc"].strip(),
                    sql=raw["sql"].strip(),
                    universe=universe or None,
                )
            )

    if not specs:
        problems.append(f"{path}: no specs found")
    if problems:
        raise SpecError(problems)
    return specs


def apply_geo(specs: list[QuerySpec], geo: str | None) -> tuple[list[QuerySpec], list[str]]:
    """Fill the {geo} placeholder, or report specs that still need one."""
    problems: list[str] = []
    if geo is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", geo):
        return specs, [f"--geo must be a plain column name, got {geo!r}"]

    out: list[QuerySpec] = []
    filled = 0
    for s in specs:
        if GEO_PLACEHOLDER in s.sql:
            if geo is None:
                problems.append(
                    f"{s.key}: sql contains {GEO_PLACEHOLDER} but no --geo was given"
                )
                out.append(s)
                continue
            out.append(replace(s, sql=s.sql.replace(GEO_PLACEHOLDER, geo)))
            filled += 1
        else:
            out.append(s)

    if geo is not None and filled == 0:
        print(
            f"warning: --geo {geo} given but no spec contains {GEO_PLACEHOLDER}; "
            f"the geography was baked in at build time or is absent",
            file=sys.stderr,
        )
    return out, problems


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|copy|install|load)\b", re.I
)


def validate_static(specs: list[QuerySpec]) -> list[str]:
    """Checks that need no database connection."""
    problems: list[str] = []
    seen: dict[tuple[str, int], int] = {}

    for i, s in enumerate(specs, start=1):
        dup = seen.get((s.table_id, s.row_id))
        if dup:
            problems.append(f"{s.key}: duplicate table_id/row_id (also spec #{dup})")
        else:
            seen[(s.table_id, s.row_id)] = i

        sql = s.sql.strip().rstrip(";").strip()
        if not sql:
            problems.append(f"{s.key}: sql is empty")
            continue
        if ";" in sql:
            problems.append(f"{s.key}: sql must be a single statement (found ';')")
        if not re.match(r"^\s*(select|with)\b", sql, re.I):
            problems.append(f"{s.key}: sql must start with SELECT or WITH")
        if _FORBIDDEN.search(sql):
            problems.append(f"{s.key}: sql contains a data-modifying keyword")
        if not s.row_desc.strip():
            problems.append(f"{s.key}: row_desc is empty")

    return problems


def validate_against_db(
    con: duckdb.DuckDBPyConnection, specs: list[QuerySpec]
) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """Compile each subquery on its own and compare output columns.

    Returns (errors, warnings, columns-per-spec). Compiling individually is
    the whole point: a
    typo in spec 37 reports as 'T4/3: Referenced column "incom" not found'
    instead of one opaque failure on a 200-line master query.
    """
    errors: list[str] = []
    column_sets: dict[str, list[str]] = {}
    seen_columns: dict[str, str] = {}   # column name -> first spec that used it

    for s in specs:
        sql = s.sql.strip().rstrip(";")
        try:
            # DESCRIBE compiles and returns the output schema without scanning.
            rows = con.execute(f"DESCRIBE ({sql})").fetchall()
        except duckdb.Error as exc:
            first_line = str(exc).splitlines()[0]
            errors.append(f"{s.key}: {first_line}")
            continue

        cols = [r[0] for r in rows]
        column_sets[s.key] = cols

        clash = [c for c in cols if c in LABEL_COLUMNS]
        if clash:
            errors.append(
                f"{s.key}: measure column(s) {clash} collide with label columns"
            )
        for c in cols:
            seen_columns.setdefault(c, s.key)

    # UNION ALL BY NAME silently fills mismatched names with NULL, so a typo
    # in a measure alias produces a column of nulls rather than an error.
    warnings: list[str] = []
    rare = {c: k for c, k in seen_columns.items()
            if c not in LABEL_COLUMNS
            and sum(c in cols for cols in column_sets.values()) == 1
            and len(column_sets) > 1}
    for col, key in sorted(rare.items()):
        warnings.append(
            f"{key}: column '{col}' appears in only this spec — "
            f"it will be NULL for every other row (typo, or intentional?)"
        )

    return errors, warnings, column_sets


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------

def sql_literal(value: str | None) -> str:
    """Single-quote a string for SQL, doubling embedded quotes."""
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def build_master(specs: list[QuerySpec], geo: str | None = None,
                 grouped: bool = False) -> str:
    parts = []
    for s in specs:
        labels = (
            f"{sql_literal(s.table_id)} AS table_id, "
            f"{s.row_id} AS row_id, "
            f"{sql_literal(s.row_desc)} AS row_desc, "
            f"{sql_literal(s.universe)} AS universe, "
            f"{sql_literal(geo)} AS geo_level"
        )
        body = s.sql.strip().rstrip(";")
        parts.append(f"-- {s.key}\nSELECT {labels}, sub.*\nFROM (\n{body}\n) AS sub")

    # With a geography, one table's lines for a single area should read
    # together, so the area sorts first.
    order = f"{GEO_COLUMN}, table_id, row_id" if grouped else "table_id, row_id"
    return "\n\nUNION ALL BY NAME\n\n".join(parts) + f"\n\nORDER BY {order}"


def load_spine(path: Path) -> list[str]:
    """Read the geography list: a one-column CSV, or one with a geoid column."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise SpecError([f"{path}: spine file is empty"])
    header = [c.strip() for c in rows[0]]
    if GEO_COLUMN in [c.lower() for c in header]:
        idx = [c.lower() for c in header].index(GEO_COLUMN)
        values = [r[idx].strip() for r in rows[1:]]
    elif len(header) == 1:
        # Single column: treat the first row as data unless it looks like a name.
        values = [r[0].strip() for r in rows]
        if values and not re.fullmatch(r"[0-9]+", values[0]):
            values = values[1:]
    else:
        raise SpecError(
            [f"{path}: expected a single column or a '{GEO_COLUMN}' column, "
             f"got {header}"]
        )
    values = [v for v in values if v]
    if not values:
        raise SpecError([f"{path}: no geographies found"])
    dupes = {v for v in values if values.count(v) > 1}
    if dupes:
        raise SpecError([f"{path}: duplicate geoid(s) {sorted(dupes)[:5]}"])
    return values


def build_densified(specs: list[QuerySpec], master: str, spine: list[str],
                    geo: str | None, zero_fill: list[str]) -> str:
    """Wrap the master query so every (line, geography) pair yields a row.

    The line grid comes from the SPECS, not from the results: a line that is
    zero in every geography contributes nothing to the union, so deriving the
    grid from results would drop it entirely. Likewise the spine is external
    because the microdata can only name geographies that already contain
    someone. Count measures are coalesced to 0; everything else stays NULL,
    since an empty cell has a count of zero but an undefined mean.
    """
    body = master.rsplit("ORDER BY", 1)[0].rstrip()

    line_rows = ",\n    ".join(
        f"({sql_literal(s.table_id)}, {s.row_id}, {sql_literal(s.row_desc)}, "
        f"{sql_literal(s.universe)})"
        for s in specs
    )
    spine_rows = ",\n    ".join(f"({sql_literal(g)})" for g in spine)

    fill = ",\n       ".join(
        f"coalesce(r.{c}, 0) AS {c}" for c in zero_fill
    )
    passthrough = "r.* EXCLUDE (table_id, row_id, row_desc, universe, geo_level, "
    passthrough += f"{GEO_COLUMN}, {', '.join(zero_fill)})"

    return f"""WITH results AS (
{body}
),
lines(table_id, row_id, row_desc, universe) AS (
  VALUES
    {line_rows}
),
spine({GEO_COLUMN}) AS (
  VALUES
    {spine_rows}
),
grid AS (
  SELECT l.table_id, l.row_id, l.row_desc, l.universe, s.{GEO_COLUMN}
  FROM lines l CROSS JOIN spine s
)
SELECT g.table_id, g.row_id, g.row_desc, g.universe,
       {sql_literal(geo)} AS geo_level,
       g.{GEO_COLUMN},
       {fill},
       {passthrough}
FROM grid g
LEFT JOIN results r
  ON  r.table_id = g.table_id
  AND r.row_id   = g.row_id
  AND CAST(r.{GEO_COLUMN} AS VARCHAR) = g.{GEO_COLUMN}
ORDER BY g.{GEO_COLUMN}, g.table_id, g.row_id"""


def register_source(con: duckdb.DuckDBPyConnection, source: SourceConfig) -> None:
    kind = "TABLE" if source.materialize else "VIEW"
    con.execute(
        f"CREATE OR REPLACE {kind} {source.view} AS "
        f"SELECT * FROM read_csv_auto(?)",
        [source.path],
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--specs", type=Path, default=Path("query_specs.csv"),
                    help="CSV of query specs")
    ap.add_argument("--source", default="data/individuals.csv",
                    help="CSV of individual records")
    ap.add_argument("--view", default="recs",
                    help="name the specs' SQL refers to")
    ap.add_argument("--no-materialize", action="store_true",
                    help="register source as a VIEW instead of a TABLE")
    ap.add_argument("--out", type=Path, help="write results to this CSV")
    ap.add_argument("--check", action="store_true", help="validate only")
    ap.add_argument("--print-sql", action="store_true", help="print the master query")
    ap.add_argument("--geo",
                    help="geographic identifier column to fill into the {geo} "
                         "placeholder; every query then groups by it")
    ap.add_argument("--spine", type=Path,
                    help="CSV listing every geography that must appear in the "
                         "output; requires --geo")
    ap.add_argument("--zero-fill", default="n",
                    help="comma-separated count columns coalesced to 0 when a "
                         "cell is empty (default: n); other measures stay NULL")
    ap.add_argument("--strict", action="store_true", help="treat warnings as errors")
    args = ap.parse_args(argv)

    source = SourceConfig(
        path=args.source, view=args.view, materialize=not args.no_materialize
    )

    try:
        specs = load_specs(args.specs)
    except SpecError as exc:
        print(exc, file=sys.stderr)
        return 2

    specs, geo_problems = apply_geo(specs, args.geo)
    problems = geo_problems + validate_static(specs)
    if problems:
        print(SpecError(problems), file=sys.stderr)
        return 2

    con = duckdb.connect()
    register_source(con, source)

    errors, warnings, column_sets = validate_against_db(con, specs)
    grouped = any(GEO_COLUMN in cols for cols in column_sets.values())
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if errors or (warnings and args.strict):
        print(SpecError(errors or warnings), file=sys.stderr)
        return 2

    master = build_master(specs, args.geo, grouped)

    if args.spine:
        if not grouped:
            print("--spine requires geography-grouped specs (use --geo with "
                  "specs built for a geography)", file=sys.stderr)
            return 2
        try:
            spine = load_spine(args.spine)
        except SpecError as exc:
            print(exc, file=sys.stderr)
            return 2
        zero_fill = [c.strip() for c in args.zero_fill.split(",") if c.strip()]
        all_measures = {c for cols in column_sets.values() for c in cols}
        bad = [c for c in zero_fill if c not in all_measures]
        if bad:
            print(f"--zero-fill column(s) {bad} not produced by any spec "
                  f"(measures found: {sorted(all_measures - {GEO_COLUMN})})",
                  file=sys.stderr)
            return 2
        master = build_densified(specs, master, spine, args.geo, zero_fill)
    if args.print_sql:
        print(master)
    if args.check:
        print(f"{len(specs)} spec(s) OK", file=sys.stderr)
        return 0

    df = con.execute(master).df()

    if args.spine:
        zf = [c for c in zero_fill if c in df.columns]
        if zf:
            dead = sorted(
                g for g, sub in df.groupby(GEO_COLUMN)
                if (sub[zf] == 0).all().all()
            )
            if dead:
                shown = ", ".join(dead[:5]) + (" ..." if len(dead) > 5 else "")
                print(f"warning: {len(dead)} spine geograph"
                      f"{'y is' if len(dead) == 1 else 'ies are'} zero on "
                      f"every line: {shown} — real if truly empty, but also "
                      f"the signature of a mistyped geoid", file=sys.stderr)
        expected = len(specs) * len(spine)
        if len(df) != expected:
            print(f"error: densified output has {len(df)} rows, expected "
                  f"{len(specs)} lines x {len(spine)} geographies = {expected}; "
                  f"check for spine/microdata geoid mismatches",
                  file=sys.stderr)
            return 2

    if args.out:
        df.to_csv(args.out, index=False)
        print(f"wrote {len(df)} rows to {args.out}", file=sys.stderr)
    else:
        print(df.to_string(index=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # Output was piped to something like `head` that closed early.
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(0)
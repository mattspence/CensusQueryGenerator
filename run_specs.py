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
    python run_specs.py --source data/2020.parquet   # CSV or Parquet, by extension
    python run_specs.py --check                 # validate only, run nothing
    python run_specs.py --print-sql             # dump the master query
    python run_specs.py --geo StateGEOID        # fill the {geo} placeholder
    python run_specs.py --geo CountyGEOID       # same specs, finer geography
    python run_specs.py --geo CountyGEOID --spine counties.csv
                                                # zero-fill every line for
                                                # every geography in the spine
    python run_specs.py --geo BlockGEOID --out results.parquet \
                        --labels lines.csv --no-sort
                                                # large run: stream to Parquet,
                                                # line labels in a lookup file

Results are streamed to --out by DuckDB (Parquet or CSV, by extension), so
the result set never has to fit in memory. Only when there is no --out are
the rows collected and printed.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, fields, replace
from pathlib import Path

import duckdb

from microdata import PARQUET_SUFFIXES, SourceError, source_reader

# Label columns prepended to every subquery, in output order.
LABEL_COLUMNS = ("table_id", "row_id", "row_desc", "universe", "geo_level")

# With --labels the descriptive text moves to a lookup file keyed by
# table_id/row_id, and the results carry only these.
SLIM_LABEL_COLUMNS = ("table_id", "row_id", "geo_level")

# The lookup file's columns: one row per line, not one per result row.
LINE_COLUMNS = ("table_id", "row_id", "row_desc", "universe")

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
    # None picks by format: see register_source.
    materialize: bool | None = None


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


def label_values(s: QuerySpec, geo: str | None) -> dict[str, str]:
    """SQL literal for each label column of one spec."""
    return {
        "table_id": sql_literal(s.table_id),
        "row_id": str(s.row_id),
        "row_desc": sql_literal(s.row_desc),
        "universe": sql_literal(s.universe),
        "geo_level": sql_literal(geo),
    }


def build_master(specs: list[QuerySpec], geo: str | None = None,
                 grouped: bool = False,
                 labels: tuple[str, ...] = LABEL_COLUMNS,
                 sort: bool = True) -> str:
    parts = []
    for s in specs:
        values = label_values(s, geo)
        select = ", ".join(f"{values[c]} AS {c}" for c in labels)
        body = s.sql.strip().rstrip(";")
        parts.append(f"-- {s.key}\nSELECT {select}, sub.*\nFROM (\n{body}\n) AS sub")

    master = "\n\nUNION ALL BY NAME\n\n".join(parts)
    if sort:
        # With a geography, one table's lines for a single area should read
        # together, so the area sorts first.
        order = f"{GEO_COLUMN}, table_id, row_id" if grouped else "table_id, row_id"
        master += f"\n\nORDER BY {order}"
    return master


def write_labels(specs: list[QuerySpec], path: Path) -> None:
    """Write the line lookup that --labels leaves out of the results."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(LINE_COLUMNS)
        for s in specs:
            writer.writerow([s.table_id, s.row_id, s.row_desc, s.universe or ""])


# --------------------------------------------------------------------------
# Spine densification
#
# Everything here is SQL over three temporary tables: `spine` (the --spine
# file), `lines` (one row per spec) and `results` (the union of every spec,
# before zero-filling). The spine and the results are never pulled into
# Python, so a spine of millions of blocks costs the same few lines of code
# as one of fifty states.
#
# They are real tables rather than inline VALUES lists or CTEs because DuckDB
# then knows their sizes, and so plans the big join around the small side.
# --------------------------------------------------------------------------

def load_spine(con: duckdb.DuckDBPyConnection, path: Path) -> int:
    """Load the geography list into the temp table `spine`; return its size.

    Accepts a one-column CSV (header optional) or any CSV with a geoid column.
    Python reads only the first row, to find the column; DuckDB reads the rest.
    """
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        first = next((r for r in reader if any(c.strip() for c in r)), None)
        lines_before_data = reader.line_num
    if first is None:
        raise SpecError([f"{path}: spine file is empty"])

    header = [c.strip() for c in first]
    if GEO_COLUMN in [c.lower() for c in header]:
        idx = [c.lower() for c in header].index(GEO_COLUMN)
    elif len(header) == 1:
        idx = 0
        # Single column: the first row is data unless it looks like a name.
        if re.fullmatch(r"[0-9]+", header[0]):
            lines_before_data -= 1
    else:
        raise SpecError(
            [f"{path}: expected a single column or a '{GEO_COLUMN}' column, "
             f"got {header}"]
        )

    names = [f"c{i}" for i in range(len(header))]
    con.execute(
        f"""CREATE OR REPLACE TEMP TABLE spine AS
            SELECT trim(c{idx}) AS {GEO_COLUMN}
            FROM read_csv({sql_literal(str(path))}, header = false,
                          all_varchar = true, delim = ',', names = {names},
                          skip = {lines_before_data})
            WHERE trim(c{idx}) <> ''"""
    )

    size = con.execute("SELECT count(*) FROM spine").fetchone()[0]
    if size == 0:
        raise SpecError([f"{path}: no geographies found"])
    dupes = con.execute(
        f"""SELECT {GEO_COLUMN} FROM spine GROUP BY {GEO_COLUMN}
            HAVING count(*) > 1 ORDER BY {GEO_COLUMN} LIMIT 5"""
    ).fetchall()
    if dupes:
        raise SpecError([f"{path}: duplicate geoid(s) {[d[0] for d in dupes]}"])
    return size


def count_and_sample(con: duckdb.DuckDBPyConnection, query: str) -> tuple[int, str]:
    """How many rows a one-column query returns, and the first few as text."""
    rows = con.execute(
        f"SELECT count(*) OVER (), * FROM ({query}) ORDER BY 2 LIMIT 5"
    ).fetchall()
    if not rows:
        return 0, ""
    total = rows[0][0]
    return total, ", ".join(r[1] for r in rows) + (" ..." if total > 5 else "")


def check_results_against_spine(
    con: duckdb.DuckDBPyConnection, zero_fill: list[str]
) -> tuple[list[str], list[str]]:
    """Compare the `results` and `spine` tables. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []

    # Each spec must give one row per geography: the densifying join would
    # otherwise repeat the cell, and the output would no longer be exactly
    # lines x geographies.
    repeated = con.execute(
        f"""SELECT table_id, row_id, count(*) FROM (
                SELECT table_id, row_id, {GEO_COLUMN} FROM results
                GROUP BY ALL HAVING count(*) > 1)
            GROUP BY ALL ORDER BY ALL LIMIT 5"""
    ).fetchall()
    for table_id, row_id, n in repeated:
        errors.append(
            f"{table_id}/{row_id}: returns more than one row for {n} "
            f"geograph{'y' if n == 1 else 'ies'}; with --spine each spec must "
            f"give exactly one row per {GEO_COLUMN}"
        )

    # One pass over the results: which geographies appear at all, and which
    # have something other than zero on at least one line.
    nonzero = " OR ".join(f"coalesce({c}, 0) <> 0" for c in zero_fill)
    con.execute(
        f"""CREATE OR REPLACE TEMP TABLE result_geos AS
            SELECT CAST({GEO_COLUMN} AS VARCHAR) AS {GEO_COLUMN},
                   bool_or({nonzero}) AS has_nonzero
            FROM results WHERE {GEO_COLUMN} IS NOT NULL GROUP BY ALL"""
    )

    n, shown = count_and_sample(con, f"""
        SELECT {GEO_COLUMN} FROM spine WHERE {GEO_COLUMN} NOT IN
            (SELECT {GEO_COLUMN} FROM result_geos WHERE has_nonzero)""")
    if n:
        warnings.append(
            f"{n} spine geograph{'y is' if n == 1 else 'ies are'} zero on "
            f"every line: {shown}. OK if empty geos, but also the signature "
            f"of a mistyped geoid"
        )

    n, shown = count_and_sample(con, f"""
        SELECT {GEO_COLUMN} FROM result_geos WHERE {GEO_COLUMN} NOT IN
            (SELECT {GEO_COLUMN} FROM spine)""")
    if n:
        warnings.append(
            f"{n} geograph{'y' if n == 1 else 'ies'} in the microdata "
            f"{'is' if n == 1 else 'are'} not in the spine and "
            f"{'is' if n == 1 else 'are'} left out of the output: {shown}"
        )
    return errors, warnings


def build_lines_table(specs: list[QuerySpec]) -> str:
    """Statement creating the temp table `lines`, one row per spec."""
    rows = ",\n    ".join(
        "(" + ", ".join(label_values(s, None)[c] for c in LINE_COLUMNS) + ")"
        for s in specs
    )
    return (f"CREATE OR REPLACE TEMP TABLE lines({', '.join(LINE_COLUMNS)}) AS\n"
            f"  VALUES\n    {rows}")


def build_densified(geo: str | None, zero_fill: list[str],
                    labels: tuple[str, ...] = LABEL_COLUMNS,
                    sort: bool = True) -> str:
    """Query giving a row for every (line, spine geography) pair.

    Reads the temp tables `lines`, `spine` and `results`. The line grid comes
    from the SPECS, not from the results: a line that is zero in every
    geography contributes nothing to the union, so deriving the grid from
    results would drop it entirely. Likewise the spine is external because
    the microdata can only name geographies that already contain someone.
    Count measures are coalesced to 0; everything else stays NULL, since an
    empty cell has a count of zero but an undefined mean.
    """
    select = ", ".join(
        f"{sql_literal(geo)} AS geo_level" if c == "geo_level" else f"l.{c}"
        for c in labels
    )
    fill = ",\n       ".join(f"coalesce(r.{c}, 0) AS {c}" for c in zero_fill)
    order = f"\nORDER BY s.{GEO_COLUMN}, l.table_id, l.row_id" if sort else ""

    return f"""SELECT {select},
       s.{GEO_COLUMN},
       {fill},
       r.* EXCLUDE (table_id, row_id, {GEO_COLUMN}, {", ".join(zero_fill)})
FROM lines l
CROSS JOIN spine s
LEFT JOIN results r
  ON  r.table_id = l.table_id
  AND r.row_id   = l.row_id
  AND CAST(r.{GEO_COLUMN} AS VARCHAR) = s.{GEO_COLUMN}{order}"""


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

def register_source(con: duckdb.DuckDBPyConnection, source: SourceConfig) -> None:
    """Make the microdata visible to the specs under the name `source.view`.

    Parquet is left on disk as a VIEW: DuckDB then reads only the columns the
    specs mention, so a wide national file never has to fit in memory. CSV is
    loaded once into a TABLE, because re-parsing the text for every spec
    would dominate the run. `source.materialize` overrides either default.
    """
    reader = source_reader(source.path).replace("?", sql_literal(source.path))
    materialize = source.materialize
    if materialize is None:
        materialize = not reader.startswith("read_parquet")
    kind = "TABLE" if materialize else "VIEW"
    con.execute(f"CREATE OR REPLACE {kind} {source.view} AS SELECT * FROM {reader}")


def write_results(con: duckdb.DuckDBPyConnection, query: str, out: Path) -> int:
    """Stream a query's rows straight to a Parquet or CSV file; return how many.

    DuckDB writes as it computes, so the rows never pass through Python.
    """
    parquet = out.suffix.lower() in PARQUET_SUFFIXES
    file_format = "FORMAT PARQUET" if parquet else "FORMAT CSV, HEADER"
    return con.execute(
        f"COPY (\n{query}\n) TO {sql_literal(str(out))} ({file_format})"
    ).fetchone()[0]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--specs", type=Path, default=Path("query_specs.csv"),
                    help="CSV of query specs")
    ap.add_argument("--source", default="data/individuals.csv",
                    help="microdata file of individual records, CSV or "
                         "Parquet (by extension)")
    ap.add_argument("--view", default="recs",
                    help="name the specs' SQL refers to")
    ap.add_argument("--materialize", action=argparse.BooleanOptionalAction,
                    help="load the source into an in-memory TABLE, or with "
                         "--no-materialize query the file in place as a VIEW "
                         "(default: table for CSV, view for Parquet)")
    ap.add_argument("--out", type=Path,
                    help="stream results to this file: Parquet if it ends in "
                         ".parquet, otherwise CSV")
    ap.add_argument("--labels", type=Path,
                    help="write row_desc and universe once per line to this "
                         "CSV, keyed by table_id/row_id, and leave them out "
                         "of the results")
    ap.add_argument("--no-sort", action="store_true",
                    help="skip the final ORDER BY, which is costly on very "
                         "large results")
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
        path=args.source, view=args.view, materialize=args.materialize
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
    try:
        register_source(con, source)
    except SourceError as exc:
        print(exc, file=sys.stderr)
        return 2

    errors, warnings, column_sets = validate_against_db(con, specs)
    grouped = any(GEO_COLUMN in cols for cols in column_sets.values())
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if errors or (warnings and args.strict):
        print(SpecError(errors or warnings), file=sys.stderr)
        return 2

    labels = SLIM_LABEL_COLUMNS if args.labels else LABEL_COLUMNS
    sort = not args.no_sort
    setup: list[str] = []    # statements that must run before `query`

    if not args.spine:
        query = build_master(specs, args.geo, grouped, labels, sort)
    else:
        if not grouped:
            print("--spine requires geography-grouped specs (use --geo with "
                  "specs built for a geography)", file=sys.stderr)
            return 2
        try:
            spine_size = load_spine(con, args.spine)
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
        # The un-filled results go to a temp table first, so they can be
        # checked against the spine before anything is written.
        setup = [
            build_lines_table(specs),
            "CREATE OR REPLACE TEMP TABLE results AS\n"
            + build_master(specs, labels=("table_id", "row_id"), sort=False),
        ]
        query = build_densified(args.geo, zero_fill, labels, sort)

    if args.print_sql:
        print(";\n\n".join(setup + [query]))
    if args.check:
        print(f"{len(specs)} spec(s) OK", file=sys.stderr)
        return 0

    if not sort:
        # Lets DuckDB write rows as they are produced instead of buffering
        # them to keep their order, which matters once results outgrow memory.
        con.execute("SET preserve_insertion_order = false")

    if args.spine:
        for statement in setup:
            con.execute(statement)
        errors, warnings = check_results_against_spine(con, zero_fill)
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        if errors:
            print(SpecError(errors), file=sys.stderr)
            return 2

    if args.labels:
        write_labels(specs, args.labels)
        print(f"wrote {len(specs)} line labels to {args.labels}", file=sys.stderr)

    if args.out:
        rows = write_results(con, query, args.out)
        shape = (f" ({len(specs)} lines x {spine_size} geographies)"
                 if args.spine else "")
        print(f"wrote {rows} rows{shape} to {args.out}", file=sys.stderr)
    else:
        print(con.execute(query).df().to_string(index=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # Output was piped to something like `head` that closed early.
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(0)
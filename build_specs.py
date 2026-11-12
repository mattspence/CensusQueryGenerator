"""Turn table shell metadata into query specs for run_specs.py.

Input is a CSV of shell rows with these columns:

    TBLID      table identifier
    LTYPE      H = header (skipped), U = universe, S / D = published lines
    HIERARCHY  indent level; a line's parent is the nearest preceding line
               with a strictly smaller HIERARCHY
    LINENO     line number within the table
    LDESC      published line label
    LDEF       source spec, translated into the WHERE clause

Each published line's WHERE clause is the conjunction of the table's universe
LDEF, every ancestor's LDEF from root down, and the line's own LDEF. Exact
duplicate predicates are dropped; redundant-but-different ones are kept, since
narrowing an already-narrow predicate is never wrong.

LDEF grammar understood:

    VAR=value            ->  VAR = 'value'
    VAR=v1,v2,v3         ->  VAR IN ('v1','v2','v3')
    VAR=a:b              ->  inclusive range, expanded to an IN list
    VAR!=value           ->  VAR <> 'value'
    VAR>=value           ->  VAR >= 'value'   (also <=, >, <)
    A AND B / A OR B     ->  combined, AND binding tighter than OR
    (A OR B) AND C       ->  parentheses respected

Anything else is an error naming the table and line, never a silent guess.

With --source, LDEF variables are checked against the microdata's columns and
literals are typed from them (numeric columns unquoted, text quoted). The
microdata may be CSV or Parquet, chosen by file extension.

With --geo, every generated query aggregates by a geographic identifier
instead of returning a single national figure. Pass a concrete column name to
bake it in, or the literal "{geo}" to leave a placeholder that run_specs.py
fills at run time, so one spec file serves every geography level.

Usage:
    python build_specs.py --shell table_shell.csv --out query_specs.csv
    python build_specs.py --shell table_shell.csv --source data/individuals.csv
    python build_specs.py --shell table_shell.csv --source data/individuals.parquet
    python build_specs.py --shell table_shell.csv --geo "{geo}"   # geography at run time
    python build_specs.py --shell table_shell.csv --geo StateGEOID
    python build_specs.py --shell table_shell.csv --audit lineage.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from microdata import SourceError, is_numeric_type, source_reader

MAX_RANGE = 500          # refuse to expand absurd ranges into IN lists
COMPARISONS = (">=", "<=", ">", "<")

# Non-fatal notes raised during translation, reported once at the end.
WARNINGS: set[str] = set()
SHELL_COLUMNS = {"TBLID", "LTYPE", "HIERARCHY", "LINENO", "LDESC", "LDEF"}


class ShellError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(
            f"{len(problems)} problem(s) in the shell file:\n  - "
            + "\n  - ".join(problems)
        )


@dataclass
class ShellLine:
    tblid: str
    ltype: str
    hierarchy: int
    lineno: int | None
    ldesc: str
    ldef: str
    source_row: int

    @property
    def key(self) -> str:
        return f"{self.tblid}/{self.lineno}"


@dataclass
class Table:
    tblid: str
    universe_desc: str = ""
    universe_ldef: str = ""
    lines: list[ShellLine] = field(default_factory=list)


# --------------------------------------------------------------------------
# LDEF -> SQL
# --------------------------------------------------------------------------

class LdefError(ValueError):
    pass


def _split_top_level(text: str, op: str) -> list[str]:
    """Split on a boolean operator that is not inside parentheses."""
    parts, depth, current = [], 0, []
    tokens = re.split(rf"(\(|\)|\b{op}\b)", text, flags=re.I)
    for tok in tokens:
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0 and tok.upper() == op:
            parts.append("".join(current))
            current = []
            continue
        current.append(tok)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _expand_range(lo: str, hi: str, var: str,
                  text_column: bool = False) -> list[str]:
    if not (lo.isdigit() and hi.isdigit()):
        raise LdefError(f"{var}: range endpoints must be numeric, got {lo}:{hi}")
    lo_i, hi_i = int(lo), int(hi)
    if hi_i < lo_i:
        raise LdefError(f"{var}: range {lo}:{hi} runs backwards")
    if hi_i - lo_i + 1 > MAX_RANGE:
        raise LdefError(f"{var}: range {lo}:{hi} expands to over {MAX_RANGE} values")
    width = max(len(lo), len(hi))
    if text_column and len(lo) != len(hi):
        # '5:12' against a text column yields '5','6',...,'12', and '5' can
        # never match a zero-padded code like '05'. Refuse rather than guess.
        raise LdefError(
            f"{var}: range {lo}:{hi} has mixed-width endpoints but {var} is "
            f"text in the source; write both endpoints at the code width "
            f"(e.g. {lo.zfill(len(hi))}:{hi})"
        )
    # Preserve zero-padding so '01' style codes keep matching.
    pad = text_column or lo.startswith("0") or hi.startswith("0")
    return [str(v).zfill(width) if pad else str(v) for v in range(lo_i, hi_i + 1)]


def _literal(value: str, var: str, numeric_columns: set[str]) -> str:
    if var.upper() in numeric_columns:
        if not re.fullmatch(r"-?\d+(\.\d+)?", value):
            raise LdefError(f"{var} is numeric in the source but LDEF has {value!r}")
        return value
    return "'" + value.replace("'", "''") + "'"


def _atom_to_sql(atom: str, numeric_columns: set[str], known: set[str] | None) -> str:
    # Longest operators first so <> and >= are not read as < and >.
    m = re.fullmatch(
        r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(!=|<>|>=|<=|=|>|<)\s*(.+?)\s*", atom
    )
    if not m:
        raise LdefError(f"cannot parse {atom!r}")
    var, op, rhs = m.group(1), m.group(2), m.group(3)

    if known is not None and var.upper() not in known:
        raise LdefError(f"{var} is not a column in the source file")

    if op in COMPARISONS:
        value = rhs.strip()
        if "," in value or ":" in value:
            raise LdefError(f"{var}: {op} takes a single value, got {rhs!r}")
        if known is not None and var.upper() not in numeric_columns:
            # Fine for fixed-width codes like CENRACE, wrong if the column
            # holds variable-width strings, where '9' sorts above '10'.
            WARNINGS.add(
                f"{var} {op} {value}: {var} is text in the source, so this "
                f"compares lexically — correct only if its codes are fixed width"
            )
        return f"{var} {op} {_literal(value, var, numeric_columns)}"

    text_column = known is not None and var.upper() not in numeric_columns
    values: list[str] = []
    for item in rhs.split(","):
        item = item.strip()
        if ":" in item:
            lo, hi = item.split(":", 1)
            values.extend(_expand_range(lo.strip(), hi.strip(), var, text_column))
        elif item:
            values.append(item)
    if not values:
        raise LdefError(f"{var}: no values given")

    lits = [_literal(v, var, numeric_columns) for v in values]
    negate = op in ("!=", "<>")

    if len(lits) == 1:
        return f"{var} {'<>' if negate else '='} {lits[0]}"
    return f"{var} {'NOT IN' if negate else 'IN'} ({', '.join(lits)})"


def _fully_wrapped(text: str) -> bool:
    """True if the first '(' pairs with the final ')'. Distinguishes
    '(A OR B)' from '(A) AND (B)'."""
    if not (text.startswith("(") and text.endswith(")")):
        return False
    depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i == len(text) - 1
    return False


def ldef_to_sql(ldef: str, numeric_columns: set[str], known: set[str] | None) -> str:
    """Translate one LDEF expression into a SQL boolean expression."""
    text = ldef.strip()
    if not text:
        raise LdefError("empty LDEF")

    or_parts = _split_top_level(text, "OR")
    if len(or_parts) > 1:
        inner = " OR ".join(ldef_to_sql(p, numeric_columns, known) for p in or_parts)
        return f"({inner})"

    and_parts = _split_top_level(text, "AND")
    if len(and_parts) > 1:
        return " AND ".join(ldef_to_sql(p, numeric_columns, known) for p in and_parts)

    if _fully_wrapped(text):
        inner = ldef_to_sql(text[1:-1].strip(), numeric_columns, known)
        return inner if _fully_wrapped(inner) else f"({inner})"

    return _atom_to_sql(text, numeric_columns, known)


# --------------------------------------------------------------------------
# Shell parsing
# --------------------------------------------------------------------------

def load_shell(path: Path) -> tuple[list[Table], list[str]]:
    problems: list[str] = []
    tables: dict[str, Table] = {}

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = set(reader.fieldnames or [])
        missing = SHELL_COLUMNS - header
        if missing:
            raise ShellError([f"{path}: missing column(s) {sorted(missing)}"])

        for raw in reader:
            where = f"row {reader.line_num}"
            overflow = raw.pop(None, None)
            if not any((v or "").strip() for v in raw.values()):
                continue
            if overflow:
                # An unquoted comma inside LDEF ('RTYPE=3,5') splits into an
                # extra field; without this check the LDEF silently truncates
                # to 'RTYPE=3' and the counts are quietly wrong.
                problems.append(
                    f"{where}: {len(overflow)} value(s) past the last column "
                    f"({overflow}) — is a comma inside LDEF missing quotes?"
                )
                continue

            tblid = (raw["TBLID"] or "").strip()
            ltype = (raw["LTYPE"] or "").strip().upper()
            if not tblid:
                problems.append(f"{where}: blank TBLID")
                continue
            if ltype not in {"H", "U", "S", "D", "T", "I"}:
                problems.append(f"{where}: unexpected LTYPE {ltype!r}")
                continue

            table = tables.setdefault(tblid, Table(tblid))

            if ltype == "H":
                continue

            ldef = (raw["LDEF"] or "").strip()
            ldesc = (raw["LDESC"] or "").strip()

            if ltype == "U":
                if table.universe_ldef:
                    problems.append(f"{where}: {tblid} has more than one universe row")
                table.universe_ldef = ldef
                table.universe_desc = re.sub(r"^universe:\s*", "", ldesc, flags=re.I)
                continue

            lineno_raw = (raw["LINENO"] or "").strip()
            if not re.fullmatch(r"\d+", lineno_raw):
                problems.append(f"{where}: LINENO must be an integer, got {lineno_raw!r}")
                continue

            hierarchy_raw = (raw["HIERARCHY"] or "").strip()
            if not re.fullmatch(r"\d+", hierarchy_raw):
                problems.append(
                    f"{where}: HIERARCHY must be an integer, got {hierarchy_raw!r}"
                )
                continue

            table.lines.append(
                ShellLine(tblid, ltype, int(hierarchy_raw), int(lineno_raw),
                          ldesc, ldef, reader.line_num)
            )

    return list(tables.values()), problems


def resolve_ancestors(lines: list[ShellLine]) -> tuple[dict[int, list[ShellLine]], list[str]]:
    """Walk the indent levels, returning each line's ancestor chain (root first)."""
    problems: list[str] = []
    stack: list[ShellLine] = []
    chains: dict[int, list[ShellLine]] = {}

    for line in lines:
        while stack and stack[-1].hierarchy >= line.hierarchy:
            stack.pop()
        if not stack and line.hierarchy != 0:
            problems.append(
                f"{line.key} (row {line.source_row}): HIERARCHY {line.hierarchy} "
                f"has no parent above it"
            )
        elif stack and line.hierarchy > stack[-1].hierarchy + 1:
            problems.append(
                f"{line.key} (row {line.source_row}): HIERARCHY jumps from "
                f"{stack[-1].hierarchy} to {line.hierarchy}"
            )
        chains[id(line)] = list(stack)
        stack.append(line)

    return chains, problems


# --------------------------------------------------------------------------
# Spec generation
# --------------------------------------------------------------------------

@dataclass
class GeneratedSpec:
    table_id: str
    row_id: int
    row_desc: str
    universe: str
    sql: str
    lineage: str


def build_specs(
    tables: list[Table],
    measure: str,
    view: str,
    numeric_columns: set[str],
    known_columns: set[str] | None,
    geo: str | None = None,
) -> tuple[list[GeneratedSpec], list[str]]:
    specs: list[GeneratedSpec] = []
    problems: list[str] = []

    for table in tables:
        if not table.lines:
            continue
        if not table.universe_ldef:
            problems.append(f"{table.tblid}: no universe (U) row")

        seen: dict[int, int] = {}
        for line in table.lines:
            dup = seen.get(line.lineno)
            if dup:
                problems.append(
                    f"{line.key} (row {line.source_row}): LINENO already used "
                    f"by row {dup}"
                )
            else:
                seen[line.lineno] = line.source_row

        chains, chain_problems = resolve_ancestors(table.lines)
        problems.extend(chain_problems)

        for line in table.lines:
            pieces: list[tuple[str, str]] = []   # (origin, ldef)
            if table.universe_ldef:
                pieces.append(("universe", table.universe_ldef))
            for anc in chains[id(line)]:
                pieces.append((f"line {anc.lineno}", anc.ldef))
            pieces.append(("self", line.ldef))

            predicates: list[str] = []
            failed = False
            for origin, ldef in pieces:
                if not ldef:
                    continue
                try:
                    sql = ldef_to_sql(ldef, numeric_columns, known_columns)
                except LdefError as exc:
                    problems.append(
                        f"{line.key} (row {line.source_row}, from {origin}): {exc}"
                    )
                    failed = True
                    continue
                if sql not in predicates:      # drop exact duplicates only
                    predicates.append(sql)
            if failed:
                continue
            if not predicates:
                problems.append(f"{line.key} (row {line.source_row}): no usable LDEF")
                continue

            where = "\n  AND ".join(predicates)
            select_list = f"{geo} AS geoid, {measure}" if geo else measure
            group_by = f"\nGROUP BY {geo}" if geo else ""
            specs.append(
                GeneratedSpec(
                    table_id=line.tblid,
                    row_id=line.lineno,
                    row_desc=line.ldesc,
                    universe=table.universe_desc,
                    sql=f"SELECT {select_list}\nFROM {view}\nWHERE {where}{group_by}",
                    lineage=" > ".join(
                        f"{o}:{d}" for o, d in pieces if d
                    ),
                )
            )

    return specs, problems


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def write_csv(specs: list[GeneratedSpec], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["table_id", "row_id", "row_desc", "universe", "sql"])
        for s in specs:
            w.writerow([s.table_id, s.row_id, s.row_desc, s.universe, s.sql])


def write_audit(specs: list[GeneratedSpec], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["table_id", "row_id", "row_desc", "ldef_lineage", "where_clause"])
        for s in specs:
            where = s.sql.split("WHERE", 1)[1].strip()
            w.writerow([s.table_id, s.row_id, s.row_desc, s.lineage, where])


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def column_types(source: Path, view: str) -> tuple[set[str], set[str]]:
    """Return (all column names upper-cased, numeric column names upper-cased)."""
    import duckdb

    con = duckdb.connect()
    rows = con.execute(
        f"DESCRIBE SELECT * FROM {source_reader(source)}", [str(source)]
    ).fetchall()
    known = {r[0].upper() for r in rows}
    numeric = {r[0].upper() for r in rows if is_numeric_type(r[1])}
    return known, numeric


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--shell", type=Path, default=Path("table_shell.csv"))
    ap.add_argument("--out", type=Path, default=Path("query_specs.csv"))
    ap.add_argument("--source", type=Path,
                    help="microdata file, CSV or Parquet (by extension); if "
                         "given, LDEF variables are checked against its columns "
                         "and literals typed accordingly")
    ap.add_argument("--view", default="recs",
                    help="table name the generated SQL refers to")
    ap.add_argument("--measure", default="count(*) AS n",
                    help="select list for every generated query")
    ap.add_argument("--geo",
                    help='group every query by this geographic identifier; '
                         'pass the literal "{geo}" to defer the choice to '
                         "run_specs.py --geo")
    ap.add_argument("--audit", type=Path,
                    help="also write a lineage file for review")
    ap.add_argument("--check", action="store_true", help="validate only")
    args = ap.parse_args(argv)

    try:
        tables, problems = load_shell(args.shell)
    except ShellError as exc:
        print(exc, file=sys.stderr)
        return 2

    known_columns = numeric_columns = None
    if args.source:
        try:
            known_columns, numeric_columns = column_types(args.source, args.view)
        except SourceError as exc:
            print(exc, file=sys.stderr)
            return 2
    numeric_columns = numeric_columns or set()

    if args.geo and args.geo != "{geo}":
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.geo):
            print(f"--geo must be a plain column name, got {args.geo!r}", file=sys.stderr)
            return 2
        if known_columns is not None and args.geo.upper() not in known_columns:
            print(f"--geo {args.geo} is not a column in {args.source}", file=sys.stderr)
            return 2

    specs, gen_problems = build_specs(
        tables, args.measure, args.view, numeric_columns, known_columns, args.geo
    )
    problems.extend(gen_problems)

    for w in sorted(WARNINGS):
        print(f"warning: {w}", file=sys.stderr)

    if problems:
        print(ShellError(problems), file=sys.stderr)
        return 2

    if args.check:
        print(f"{len(specs)} spec(s) generated OK", file=sys.stderr)
        return 0

    write_csv(specs, args.out)
    if args.audit:
        write_audit(specs, args.audit)

    print(f"wrote {len(specs)} spec(s) to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # Output was piped to something like `head` that closed early.
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(0)
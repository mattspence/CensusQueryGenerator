"""Microdata (--source) file handling shared by build_specs.py and run_specs.py.

Kept separate so that the two scripts agree on how a source file is read:
build_specs types LDEF literals from the same reader run_specs executes
against. Importing this module does not import duckdb, so build_specs can
keep deferring that import until a --source is actually given.
"""

from __future__ import annotations

from pathlib import Path

PARQUET_SUFFIXES = (".parquet", ".pq", ".parq")
CSV_SUFFIXES = (".csv", ".tsv", ".txt")
COMPRESSION_SUFFIXES = (".gz", ".zst")        # read_csv_auto decompresses these itself

# DuckDB type names (parameters such as DECIMAL(18,3) stripped) for which
# LDEF literals are emitted unquoted. A closed set, so that e.g. INTERVAL is
# not mistaken for an integer by substring matching.
NUMERIC_TYPES = frozenset({
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
    "DECIMAL", "NUMERIC", "FLOAT", "REAL", "DOUBLE",
})


class SourceError(ValueError):
    """The --source path is not a file this tool knows how to read."""


def source_reader(path) -> str:
    """DuckDB table function (with a `?` placeholder) for a microdata file,
    chosen by extension. Specs, table shells and spines are always CSV; this
    applies only to --source. Unrecognised extensions fail here, with a clear
    message, instead of falling through to the CSV sniffer and failing there
    with an opaque parse error."""
    suffixes = [s.lower() for s in Path(path).suffixes]
    if suffixes and suffixes[-1] in COMPRESSION_SUFFIXES:
        suffixes.pop()
    ext = suffixes[-1] if suffixes else ""
    if ext in PARQUET_SUFFIXES:
        return "read_parquet(?)"
    if ext in CSV_SUFFIXES:
        return "read_csv_auto(?)"
    accepted = ", ".join(PARQUET_SUFFIXES + CSV_SUFFIXES)
    raise SourceError(
        f"cannot tell the format of --source {path}: expected a file ending "
        f"in one of {accepted} (CSV may also be .gz or .zst compressed)"
    )


def is_numeric_type(duckdb_type: str) -> bool:
    """True if a DESCRIBE type name denotes a numeric column."""
    return duckdb_type.split("(", 1)[0].strip().upper() in NUMERIC_TYPES

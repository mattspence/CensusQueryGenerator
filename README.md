# Table Shell Query Pipeline

Turns table shell metadata into tabulated results from person-level microdata,
in two stages:

```
table_shell.csv  ──►  build_specs.py  ──►  query_specs.csv  ──►  run_specs.py  ──►  results.csv
(shell metadata)      (LDEF → SQL)         (one query per line)  (one master query)
```

`build_specs.py` translates each published line's LDEF into a SQL query,
composing the table universe and the line's ancestors in the shell hierarchy
into a WHERE clause. `run_specs.py` combines every query into a single
`UNION ALL BY NAME` statement and runs it against the microdata with DuckDB.

The intermediate spec file is a build artifact: regenerate it from the shell
rather than editing it. Both stages validate aggressively and report *every*
problem at once, naming the table, line, and source row — the design goal
throughout is that mistakes fail loudly rather than producing silently wrong
counts.

## Installation

```bash
pip install -r requirements.txt        # duckdb, pandas
pip install -r requirements-dev.txt    # pytest, for the test suite
```

## Quick start

```bash
# 1. Generate specs from a shell, typing literals against the microdata
python build_specs.py --shell table_shell.csv --source data/persons.csv \
       --geo "{geo}" --out query_specs.csv --audit lineage.csv

# 2. Tabulate at any geography level
python run_specs.py --specs query_specs.csv --source data/persons.csv \
       --geo CountyGEOID --spine counties.csv --out results.csv
```

For national block-level runs, see [Running at scale](#running-at-scale).

## Microdata formats

`--source` accepts CSV or Parquet, dispatched by extension: `.parquet`,
`.pq` or `.parq` is read as Parquet; `.csv`, `.tsv` or `.txt` (optionally
`.gz` or `.zst` compressed) as CSV. Any other extension is rejected up front
with a message naming the accepted ones, rather than being fed to the CSV
sniffer. Everything downstream is identical; Parquet is faster to load and
carries explicit column types, so DuckDB never has to sniff whether a
zero-padded code column is text. A Parquet `--source` may also be a glob
(`"data/state=06/*.parquet"`) covering many files. Specs, table shells, and
spine files are always CSV. The dispatch lives in `microdata.py`, shared by both scripts so
they can never disagree about how a source is read.

## Input: the table shell

A CSV with these columns:

| Column    | Meaning |
|-----------|---------|
| TBLID     | Table identifier (e.g. `PCT17A`) |
| LTYPE     | `H` header (skipped), `U` universe, `S`/`D`/`T`/`I` published lines |
| HIERARCHY | Indent level; a line's parent is the nearest preceding line with a strictly smaller value |
| LINENO    | Line number within the table; must be unique per table |
| LDESC     | Published line label |
| LDEF      | Source spec, translated into the WHERE clause |

A published line's WHERE clause is the conjunction of the table's universe
LDEF, every ancestor's LDEF from the root down, and the line's own LDEF.
Exact duplicate predicates are dropped; redundant-but-narrower ones are kept
so the SQL traces visibly back to the shell.

**Quote LDEFs containing commas** (`"RTYPE=3,5 AND CENRACE=01"`). An unquoted
comma splits the CSV field and is detected as an error rather than silently
truncating the condition.

### LDEF grammar

| Form            | Translation |
|-----------------|-------------|
| `VAR=v`         | `VAR = v` |
| `VAR=v1,v2,v3`  | `VAR IN (v1, v2, v3)` |
| `VAR=a:b`       | inclusive range, expanded to an `IN` list |
| `VAR!=v` / `VAR<>v` | `VAR <> v` (lists become `NOT IN`) |
| `VAR>=v`        | `VAR >= v` (also `<=`, `>`, `<`; single value only) |
| `A AND B`, `A OR B` | combined; `AND` binds tighter than `OR` |
| `(A OR B) AND C` | parentheses respected |

Anything else is an error naming the table and line. Ranges refuse to expand
past 500 values or run backwards.

### Typing against the source (`--source`)

Passing `--source` to `build_specs.py` reads the microdata's column types and:

- quotes literals for text columns, leaves them bare for numeric ones —
  `QAGE >= 18` but `CENRACE = '07'` (leading zeros make DuckDB sniff a code
  column as VARCHAR);
- rejects LDEF variables that aren't columns in the file;
- rejects text values against numeric columns (`QAGE=adult`);
- rejects mixed-width ranges against text columns (`CENRACE=5:12` can never
  match zero-padded codes; write `05:12`);
- warns when a comparison lands on a text column, since `>=` then compares
  lexically — correct only for fixed-width codes.

Strongly recommended: without `--source`, every literal is quoted, which is
wrong for numeric columns.

## Intermediate: the query specs

CSV with columns `table_id, row_id, row_desc, universe, sql`. Multi-line SQL
is stored with embedded newlines inside a quoted field; Excel and Python's
`csv` module both round-trip this. Blank rows and rows whose `table_id`
starts with `#` are skipped, and unrecognized extra columns (notes, review
flags) are tolerated with a warning. Hand-written spec files are fine too —
the format is deliberately simple — but files generated from a shell should
be regenerated, not edited.

## Output stage: run_specs.py

Builds one labeled subquery per spec, joins them with `UNION ALL BY NAME`
(so specs may have different measure columns; missing ones become NULL), and
runs the result as a single statement.

### Validation layers

1. **Load**: header and field checks, per-line error messages.
2. **Static**: duplicate `table_id`/`row_id`, multi-statement SQL, non-SELECT
   statements, data-modifying keywords.
3. **Against DuckDB**: each subquery is compiled individually with
   `DESCRIBE`, so a typo reports against its own spec key instead of an
   opaque failure on the master query. Measure columns may not collide with
   label columns, and any measure appearing in exactly one spec draws a
   warning — with `BY NAME`, a misspelled alias produces a column of NULLs,
   not an error. `--strict` promotes warnings to failures.

`--check` runs all validation and stops; `--print-sql` dumps the master
query for inspection (with `--spine`, every statement that would run).

### Output (`--out`, `--labels`, `--no-sort`)

`--out` is written by DuckDB as the query runs, so the results never have to
fit in memory: Parquet if the name ends in `.parquet`, `.pq` or `.parq`,
otherwise CSV. Without `--out` the rows are collected and printed, which is
only sensible for small runs.

By default every result row repeats its line's `row_desc` and `universe`.
`--labels lines.csv` writes those once per line to a small lookup file
instead, leaving the results as `table_id, row_id, geo_level, geoid` plus
the measures:

```sql
SELECT * FROM 'results.parquet' JOIN 'lines.csv' USING (table_id, row_id)
```

`--no-sort` skips the final `ORDER BY`. Sorting is what makes each area's
table read as a block, but it is the one step whose memory grows with the
size of the output.

### How the microdata is loaded (`--materialize`)

A Parquet source is queried in place: DuckDB reads only the columns the
specs mention, so the file can be far larger than memory. A CSV source is
loaded once into an in-memory table, because re-parsing the text for every
spec would dominate the run. `--materialize` and `--no-materialize` override
either default.

### Geography (`--geo`)

Specs built with `--geo "{geo}"` carry a placeholder:

```sql
SELECT {geo} AS geoid, count(*) AS n FROM recs WHERE ... GROUP BY {geo}
```

`run_specs.py --geo StateGEOID` fills it at run time, so one spec file serves
every geography level. The output column is always `geoid`; a `geo_level`
column records which identifier produced it. Output is sorted
`geoid, table_id, row_id` so each area's table reads as a block.
Alternatively, bake a concrete column in at build time with
`build_specs.py --geo StateGEOID`.

`--geo` must be a plain column name (`[A-Za-z_][A-Za-z0-9_]*`) — it cannot
carry SQL.

### Zero-filling empty cells (`--spine`)

`GROUP BY` produces no row at all for an area with no qualifying records.
For published tables you usually want an explicit zero instead:

```bash
python run_specs.py ... --geo CountyGEOID --spine counties.csv
```

The spine is a one-column CSV of GEOIDs (header optional, or any CSV with a
`geoid` column) listing every geography that must appear. Results are
cross-joined against a grid of (every spec line) x (every spine geography),
guaranteeing a row per cell — including lines that are zero in *every*
geography, which would vanish from any grid derived from the results
themselves.

The spine, the line list and the un-filled results are DuckDB temporary
tables (`spine`, `lines`, `results`), and every check below is a SQL query
over them, so a spine of millions of blocks is handled the same way as one
of fifty states.

Only columns named in `--zero-fill` (default `n`) are coalesced to 0; other
measures stay NULL, because an empty cell has a count of zero but an
undefined mean. Guardrails, all applied before anything is written:

- duplicate spine GEOIDs are rejected (the cross join would multiply counts);
- a spec that returns more than one row for a geography is rejected, which
  is what guarantees the output is exactly lines x geographies;
- a geography that is zero on every line draws a warning — real if the area
  is truly empty, but also the signature of a mistyped GEOID;
- a geography present in the microdata but absent from the spine draws a
  warning, because its people are left out of the output.

Use a canonical geography list (e.g. derived from TIGER) rather than a
hand-typed one; the spine must be external because the microdata can only
name areas that already contain someone.

## Running at scale

For something like every block in the country (~330 million person records,
~8 million blocks, hundreds of lines):

1. **Convert the microdata to Parquet once**, ideally one file or directory
   per state. Parquet is read in place and only the needed columns are
   touched; a national CSV would be loaded whole into memory.

   ```sql
   COPY (SELECT * FROM read_csv_auto('persons.csv'))
   TO 'persons' (FORMAT PARQUET, PARTITION_BY (StateGEOID));
   ```

2. **Write Parquet, move the labels out, skip the sort**:

   ```bash
   python run_specs.py --specs query_specs.csv --source "persons/*/*.parquet" \
          --geo BlockGEOID --out results.parquet --labels lines.csv --no-sort
   ```

3. **Or run one state at a time.** Blocks nest within states, so the pieces
   simply sit side by side. Each run is small, a failure costs one state
   rather than the whole job, and per-state sorting is cheap:

   ```bash
   for dir in persons/StateGEOID=*; do
       python run_specs.py --specs query_specs.csv --source "$dir/*.parquet" \
              --geo BlockGEOID --labels lines.csv \
              --out "results/$(basename "$dir").parquet"
   done
   ```

   `SELECT * FROM 'results/*.parquet'` then reads them as one table.

4. **Think twice about `--spine` at block level.** A dense grid is
   lines x 8 million blocks — billions of rows, mostly zeros. Without a
   spine the output holds only non-empty cells, and a missing cell *is* a
   zero; fill them in when a table is assembled for publication. If you do
   want dense block output, run it per state with a per-state spine.

Measured on a 4-core, 16 GB machine with 20 million synthetic records,
500,000 blocks and 100 lines: 13 seconds and 0.3 GB of memory for the
sparse Parquet output (step 2), and 19 seconds for the dense 50-million-row
grid unsorted. Sorting that dense grid needed about 4 GB, which is why
`--no-sort` or per-state runs are the advice for anything large. Time
grows roughly in proportion to records x lines.

## Reviewing the translation (`--audit`)

`build_specs.py --audit lineage.csv` writes one row per generated line
showing its full LDEF lineage (universe, each ancestor, self) next to the
final WHERE clause — the artifact to put in front of a reviewer before
publishing.

## Testing

```bash
pytest -q      # 130 tests, a few seconds
```

`test_build_specs.py` covers LDEF translation (every operator and every
rejected form), hierarchy resolution (dedents, sibling replacement, jumps,
orphans), source typing, and end-to-end generation. `test_run_specs.py`
covers spec loading, all validation layers, geography, spine densification
(including the empty-geography and line-zero-everywhere cases), streamed
CSV and Parquet output, the label lookup, and table-versus-view loading.

## Flag reference

`build_specs.py`: `--shell` `--out` `--source` `--view` `--measure`
`--geo` `--audit` `--check`

`run_specs.py`: `--specs` `--source` `--view` `--materialize` /
`--no-materialize` `--out` `--labels` `--no-sort` `--geo` `--spine`
`--zero-fill` `--check` `--print-sql` `--strict`

Run either with `--help` for details. `--measure` (default `count(*) AS n`)
sets the select list for every generated query — e.g. a weighted sum.

## Known limitations

- Every subquery scans the microdata, so run time grows with records x
  lines. A single-scan `count(*) FILTER (WHERE ...)` formulation was tried
  and was slower (11.7 s against 6.2 s for 100 lines over 20 million
  records): one query per line lets DuckDB discard non-matching rows before
  aggregating, and keeps each line's SQL readable on its own.
- The final sort needs memory in proportion to the output. Use `--no-sort`,
  or run state by state, when the output is large.
- With `--spine`, the un-filled results are held in a temporary table
  (spilling to disk when large) so they can be checked before writing.
- A mistyped spine GEOID yields a plausible block of zeros; the
  dead-geography warning is the only guard, so source spines from a
  canonical list.
- Comparisons against text columns are lexical; the warning fires, but
  correctness depends on fixed-width codes.
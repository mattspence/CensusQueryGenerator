"""Tests for run_specs.py — spec loading, validation, geo, spine densification.

Run:  pytest -q
"""

import csv
from pathlib import Path

import pytest

import run_specs as rs


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def write_persons(tmp_path: Path) -> Path:
    """60 people: 3 counties x (12 in-scope + 8 out-of-scope)."""
    p = tmp_path / "persons.csv"
    with p.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["person_id", "RTYPE", "QSEX", "CountyGEOID"])
        pid = 0
        for county in ("11001", "11003", "24001"):
            for _ in range(6):
                w.writerow([pid, 3, 1, county]); pid += 1
            for _ in range(6):
                w.writerow([pid, 3, 2, county]); pid += 1
            for _ in range(8):
                w.writerow([pid, 5, 1, county]); pid += 1
    return p


def write_specs(tmp_path: Path, rows, name="specs.csv") -> Path:
    p = tmp_path / name
    with p.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["table_id", "row_id", "row_desc", "universe", "sql"])
        w.writerows(rows)
    return p


PLAIN_SPECS = [
    ("T1", 1, "Total", "", "SELECT count(*) AS n FROM recs WHERE RTYPE = 3"),
    ("T1", 2, "Male", "", "SELECT count(*) AS n FROM recs WHERE RTYPE = 3 AND QSEX = 1"),
    ("T1", 3, "Female", "", "SELECT count(*) AS n FROM recs WHERE RTYPE = 3 AND QSEX = 2"),
]

GEO_SPECS = [
    (t, r, d, u, s.replace("SELECT ", "SELECT {geo} AS geoid, ")
                  .replace("FROM recs", "FROM recs")
     + "\nGROUP BY {geo}")
    for t, r, d, u, s in PLAIN_SPECS
]


def run_main(tmp_path, specs_rows, *extra, persons=None):
    persons = persons or write_persons(tmp_path)
    specs = write_specs(tmp_path, specs_rows)
    out = tmp_path / "out.csv"
    rc = rs.main(["--specs", str(specs), "--source", str(persons),
                  "--out", str(out), *extra])
    rows = list(csv.DictReader(out.open())) if out.exists() else []
    return rc, rows


# --------------------------------------------------------------------------
# spec loading
# --------------------------------------------------------------------------

class TestLoading:
    def test_loads_and_runs(self, tmp_path):
        rc, rows = run_main(tmp_path, PLAIN_SPECS)
        assert rc == 0
        assert [r["n"] for r in rows] == ["36", "18", "18"]

    def test_bom_tolerated(self, tmp_path):
        specs = write_specs(tmp_path, PLAIN_SPECS)
        specs.write_text("\ufeff" + specs.read_text(), encoding="utf-8")
        rc = rs.main(["--specs", str(specs),
                      "--source", str(write_persons(tmp_path)), "--check"])
        assert rc == 0

    def test_extra_columns_warn_not_fail(self, tmp_path, capsys):
        p = tmp_path / "specs.csv"
        with p.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["table_id", "row_id", "row_desc", "universe", "sql", "notes"])
            w.writerow(["T1", 1, "Total", "", "SELECT count(*) AS n FROM recs", "hi"])
        rc = rs.main(["--specs", str(p),
                      "--source", str(write_persons(tmp_path)), "--check"])
        assert rc == 0
        assert "ignoring extra column" in capsys.readouterr().err

    def test_comment_and_blank_rows_skipped(self, tmp_path):
        rows = PLAIN_SPECS + [("# comment", "", "", "", ""), ("", "", "", "", "")]
        rc, out = run_main(tmp_path, rows)
        assert rc == 0 and len(out) == 3

    def test_parquet_source(self, tmp_path):
        import duckdb
        csv_path = write_persons(tmp_path)
        pq = tmp_path / "persons.parquet"
        duckdb.connect().execute(
            f"COPY (SELECT * FROM read_csv_auto('{csv_path}')) "
            f"TO '{pq}' (FORMAT PARQUET)"
        )
        rc, rows = run_main(tmp_path, PLAIN_SPECS, persons=pq)
        assert rc == 0
        assert [r["n"] for r in rows] == ["36", "18", "18"]

    def test_unrecognised_source_extension_fails(self, tmp_path, capsys):
        import duckdb
        csv_path = write_persons(tmp_path)
        dat = tmp_path / "persons.dat"
        duckdb.connect().execute(
            f"COPY (SELECT * FROM read_csv_auto('{csv_path}')) "
            f"TO '{dat}' (FORMAT PARQUET)"
        )
        rc, rows = run_main(tmp_path, PLAIN_SPECS, persons=dat)
        assert rc == 2 and rows == []
        err = capsys.readouterr().err
        assert "persons.dat" in err and ".parquet" in err and ".csv" in err

    def test_help_mentions_parquet(self, capsys):
        with pytest.raises(SystemExit) as exc:
            rs.main(["--help"])
        assert exc.value.code == 0
        assert "Parquet" in capsys.readouterr().out

    @pytest.mark.parametrize("row,fragment", [
        (("T1", "x", "Bad", "", "SELECT 1 AS n"), "row_id must be an integer"),
        (("T1", 9, "", "", "SELECT 1 AS n"), "empty required field"),
        (("T1", 9, "Bad", "", "DROP TABLE recs"), "must start with SELECT"),
        (("T1", 9, "Bad", "", "SELECT 1 AS n; SELECT 2"), "single statement"),
        (("T1", 1, "Dup", "", "SELECT 1 AS n"), "duplicate table_id/row_id"),
    ])
    def test_rejected_specs(self, tmp_path, capsys, row, fragment):
        rc, _ = run_main(tmp_path, PLAIN_SPECS + [row], "--check")
        assert rc == 2
        assert fragment in capsys.readouterr().err


# --------------------------------------------------------------------------
# db validation
# --------------------------------------------------------------------------

class TestDbValidation:
    def test_bad_column_named_per_spec(self, tmp_path, capsys):
        bad = PLAIN_SPECS + [("T1", 9, "Typo", "",
                              "SELECT count(NOPE) AS n FROM recs")]
        rc, _ = run_main(tmp_path, bad, "--check")
        assert rc == 2
        assert "T1/9" in capsys.readouterr().err

    def test_lonely_column_warns(self, tmp_path, capsys):
        rows = PLAIN_SPECS + [("T1", 9, "Odd", "",
                               "SELECT count(*) AS nn FROM recs")]
        rc, _ = run_main(tmp_path, rows, "--check")
        assert rc == 0
        assert "only this spec" in capsys.readouterr().err

    def test_strict_promotes_warnings(self, tmp_path):
        rows = PLAIN_SPECS + [("T1", 9, "Odd", "",
                               "SELECT count(*) AS nn FROM recs")]
        rc, _ = run_main(tmp_path, rows, "--check", "--strict")
        assert rc == 2

    def test_label_collision_rejected(self, tmp_path, capsys):
        rows = PLAIN_SPECS + [("T1", 9, "Clash", "",
                               "SELECT 1 AS row_id FROM recs")]
        rc, _ = run_main(tmp_path, rows, "--check")
        assert rc == 2
        assert "collide" in capsys.readouterr().err


# --------------------------------------------------------------------------
# geography
# --------------------------------------------------------------------------

class TestGeo:
    def test_placeholder_filled_and_grouped(self, tmp_path):
        rc, rows = run_main(tmp_path, GEO_SPECS, "--geo", "CountyGEOID")
        assert rc == 0
        assert len(rows) == 9                          # 3 lines x 3 counties
        assert {r["geoid"] for r in rows} == {"11001", "11003", "24001"}
        assert all(r["geo_level"] == "CountyGEOID" for r in rows)

    def test_placeholder_without_geo_fails(self, tmp_path, capsys):
        rc, _ = run_main(tmp_path, GEO_SPECS, "--check")
        assert rc == 2
        assert "no --geo was given" in capsys.readouterr().err

    def test_geo_must_be_plain_name(self, tmp_path):
        rc, _ = run_main(tmp_path, GEO_SPECS, "--geo", "County; DROP")
        assert rc == 2

    def test_geo_on_plain_specs_warns(self, tmp_path, capsys):
        rc, _ = run_main(tmp_path, PLAIN_SPECS, "--geo", "CountyGEOID", "--check")
        assert rc == 0
        assert "no spec contains" in capsys.readouterr().err

    def test_grouped_output_sorted_geo_first(self, tmp_path):
        _, rows = run_main(tmp_path, GEO_SPECS, "--geo", "CountyGEOID")
        assert [r["geoid"] for r in rows[:3]] == ["11001"] * 3


# --------------------------------------------------------------------------
# spine densification
# --------------------------------------------------------------------------

def write_spine(tmp_path, geos, header=True) -> Path:
    p = tmp_path / "spine.csv"
    p.write_text(("geoid\n" if header else "") + "\n".join(geos) + "\n")
    return p


class TestSpine:
    def test_empty_geography_zero_filled(self, tmp_path):
        spine = write_spine(tmp_path, ["11001", "11003", "24001", "51999"])
        rc, rows = run_main(tmp_path, GEO_SPECS, "--geo", "CountyGEOID",
                            "--spine", str(spine))
        assert rc == 0
        assert len(rows) == 12                         # 3 lines x 4 geos
        empty = [r for r in rows if r["geoid"] == "51999"]
        assert len(empty) == 3 and all(r["n"] == "0" for r in empty)

    def test_headerless_spine(self, tmp_path):
        spine = write_spine(tmp_path, ["11001", "51999"], header=False)
        rc, rows = run_main(tmp_path, GEO_SPECS, "--geo", "CountyGEOID",
                            "--spine", str(spine))
        assert rc == 0 and len(rows) == 6

    def test_mean_stays_null_when_count_zero_filled(self, tmp_path):
        specs = [("T1", 1, "Total", "",
                  "SELECT {geo} AS geoid, count(*) AS n, avg(QSEX) AS m "
                  "FROM recs WHERE RTYPE = 3 GROUP BY {geo}")]
        spine = write_spine(tmp_path, ["11001", "51999"])
        rc, rows = run_main(tmp_path, specs, "--geo", "CountyGEOID",
                            "--spine", str(spine))
        assert rc == 0
        empty = next(r for r in rows if r["geoid"] == "51999")
        assert empty["n"] == "0" and empty["m"] == ""

    def test_line_zero_everywhere_still_present(self, tmp_path):
        # the case a DISTINCT-from-results grid would silently drop
        specs = GEO_SPECS + [("T1", 4, "Nobody", "",
                              "SELECT {geo} AS geoid, count(*) AS n FROM recs "
                              "WHERE RTYPE = 99 GROUP BY {geo}")]
        spine = write_spine(tmp_path, ["11001", "11003", "24001"])
        rc, rows = run_main(tmp_path, specs, "--geo", "CountyGEOID",
                            "--spine", str(spine))
        assert rc == 0
        nobody = [r for r in rows if r["row_id"] == "4"]
        assert len(nobody) == 3 and all(r["n"] == "0" for r in nobody)

    def test_spine_without_geo_fails(self, tmp_path, capsys):
        spine = write_spine(tmp_path, ["11001"])
        rc, _ = run_main(tmp_path, PLAIN_SPECS, "--spine", str(spine))
        assert rc == 2
        assert "requires geography-grouped" in capsys.readouterr().err

    def test_duplicate_spine_geoid_fails(self, tmp_path, capsys):
        spine = write_spine(tmp_path, ["11001", "11001"])
        rc, _ = run_main(tmp_path, GEO_SPECS, "--geo", "CountyGEOID",
                         "--spine", str(spine))
        assert rc == 2
        assert "duplicate geoid" in capsys.readouterr().err

    def test_bad_zero_fill_column_fails(self, tmp_path, capsys):
        spine = write_spine(tmp_path, ["11001"])
        rc, _ = run_main(tmp_path, GEO_SPECS, "--geo", "CountyGEOID",
                         "--spine", str(spine), "--zero-fill", "n,typo")
        assert rc == 2
        assert "typo" in capsys.readouterr().err

    def test_dead_geography_warns(self, tmp_path, capsys):
        spine = write_spine(tmp_path, ["11001", "51999"])
        rc, _ = run_main(tmp_path, GEO_SPECS, "--geo", "CountyGEOID",
                         "--spine", str(spine))
        assert rc == 0
        err = capsys.readouterr().err
        assert "zero on every line" in err and "51999" in err

    def test_microdata_geography_missing_from_spine_warns(self, tmp_path, capsys):
        spine = write_spine(tmp_path, ["11001", "11003"])      # no 24001
        rc, rows = run_main(tmp_path, GEO_SPECS, "--geo", "CountyGEOID",
                            "--spine", str(spine))
        assert rc == 0 and {r["geoid"] for r in rows} == {"11001", "11003"}
        err = capsys.readouterr().err
        assert "not in the spine" in err and "24001" in err

    def test_spec_with_two_rows_per_geography_fails(self, tmp_path, capsys):
        # grouping by more than the geography would repeat cells in the join
        specs = [("T1", 1, "Total", "",
                  "SELECT {geo} AS geoid, count(*) AS n FROM recs "
                  "GROUP BY {geo}, QSEX")]
        spine = write_spine(tmp_path, ["11001", "11003", "24001"])
        rc, rows = run_main(tmp_path, specs, "--geo", "CountyGEOID",
                            "--spine", str(spine))
        assert rc == 2 and rows == []                  # nothing written
        assert "T1/1: returns more than one row" in capsys.readouterr().err

    def test_spine_with_geoid_among_other_columns(self, tmp_path):
        spine = tmp_path / "spine.csv"
        spine.write_text("name,GEOID\nA,11001\nB,51999\n")
        rc, rows = run_main(tmp_path, GEO_SPECS, "--geo", "CountyGEOID",
                            "--spine", str(spine))
        assert rc == 0 and {r["geoid"] for r in rows} == {"11001", "51999"}

    def test_spine_bom_blank_lines_and_padding_tolerated(self, tmp_path):
        spine = tmp_path / "spine.csv"
        spine.write_text("﻿geoid\n11001 \n\n51999\n", encoding="utf-8")
        rc, rows = run_main(tmp_path, GEO_SPECS, "--geo", "CountyGEOID",
                            "--spine", str(spine))
        assert rc == 0 and {r["geoid"] for r in rows} == {"11001", "51999"}

    @pytest.mark.parametrize("text, fragment", [
        ("", "empty"),
        ("geoid\n", "no geographies"),
        ("a,b\n1,2\n", "expected a single column"),
    ])
    def test_bad_spine_files_rejected(self, tmp_path, capsys, text, fragment):
        spine = tmp_path / "spine.csv"
        spine.write_text(text)
        rc, _ = run_main(tmp_path, GEO_SPECS, "--geo", "CountyGEOID",
                         "--spine", str(spine))
        assert rc == 2
        assert fragment in capsys.readouterr().err

    def test_large_spine(self, tmp_path):
        # 100,000 geographies: took minutes when the spine was a Python list
        # checked for duplicates pairwise and pasted into the SQL as literals.
        geos = ["11001", "11003", "24001"] + [f"{i:05d}" for i in range(60000, 99997)]
        spine = write_spine(tmp_path, geos)
        rc, rows = run_main(tmp_path, GEO_SPECS[:1], "--geo", "CountyGEOID",
                            "--spine", str(spine))
        assert rc == 0 and len(rows) == 40000
        assert sum(int(r["n"]) for r in rows) == 36

    def test_print_sql_shows_every_statement(self, tmp_path, capsys):
        spine = write_spine(tmp_path, ["11001"])
        rc, rows = run_main(tmp_path, GEO_SPECS, "--geo", "CountyGEOID",
                            "--spine", str(spine), "--print-sql", "--check")
        assert rc == 0 and rows == []
        out = capsys.readouterr().out
        assert "TEMP TABLE lines" in out and "TEMP TABLE results" in out
        assert "CROSS JOIN spine" in out


# --------------------------------------------------------------------------
# output: streaming, Parquet, label lookup, sorting
# --------------------------------------------------------------------------

def read_parquet(path):
    import duckdb
    rel = duckdb.connect().execute(f"SELECT * FROM read_parquet('{path}')")
    names = [d[0] for d in rel.description]
    return [dict(zip(names, row)) for row in rel.fetchall()]


class TestOutput:
    def test_parquet_output(self, tmp_path):
        out = tmp_path / "out.parquet"
        rc, _ = run_main(tmp_path, GEO_SPECS, "--geo", "CountyGEOID",
                         "--out", str(out))
        assert rc == 0
        rows = read_parquet(out)
        assert len(rows) == 9
        assert rows[0] == {"table_id": "T1", "row_id": 1, "row_desc": "Total",
                           "universe": None, "geo_level": "CountyGEOID",
                           "geoid": 11001, "n": 12}

    def test_labels_move_to_lookup_file(self, tmp_path):
        labels = tmp_path / "lines.csv"
        specs = [("T1", 1, "Total, all ages", "Total population",
                  "SELECT count(*) AS n FROM recs")]
        rc, rows = run_main(tmp_path, specs, "--labels", str(labels))
        assert rc == 0
        assert rows == [{"table_id": "T1", "row_id": "1", "geo_level": "", "n": "60"}]
        assert list(csv.DictReader(labels.open())) == [
            {"table_id": "T1", "row_id": "1", "row_desc": "Total, all ages",
             "universe": "Total population"}]

    def test_labels_with_spine(self, tmp_path):
        spine = write_spine(tmp_path, ["11001", "51999"])
        rc, rows = run_main(tmp_path, GEO_SPECS, "--geo", "CountyGEOID",
                            "--spine", str(spine),
                            "--labels", str(tmp_path / "lines.csv"))
        assert rc == 0 and len(rows) == 6
        assert list(rows[0]) == ["table_id", "row_id", "geo_level", "geoid", "n"]

    def test_labels_not_written_on_check(self, tmp_path):
        labels = tmp_path / "lines.csv"
        rc, _ = run_main(tmp_path, PLAIN_SPECS, "--labels", str(labels), "--check")
        assert rc == 0 and not labels.exists()

    @pytest.mark.parametrize("spine", [False, True])
    def test_no_sort_gives_same_rows(self, tmp_path, spine):
        extra = ["--geo", "CountyGEOID"]
        if spine:
            extra += ["--spine", str(write_spine(tmp_path, ["11001", "51999"]))]
        _, ordered = run_main(tmp_path, GEO_SPECS, *extra)
        _, unordered = run_main(tmp_path, GEO_SPECS, *extra, "--no-sort")
        key = lambda r: (r["geoid"], r["row_id"])
        assert ordered == sorted(ordered, key=key)
        assert sorted(unordered, key=key) == ordered

    def test_stdout_when_no_out(self, tmp_path, capsys):
        specs = write_specs(tmp_path, PLAIN_SPECS)
        rc = rs.main(["--specs", str(specs), "--source", str(write_persons(tmp_path))])
        assert rc == 0
        assert "Female" in capsys.readouterr().out


# --------------------------------------------------------------------------
# source registration: table or view
# --------------------------------------------------------------------------

def write_persons_parquet(tmp_path: Path, name="persons.parquet") -> Path:
    import duckdb
    pq = tmp_path / name
    duckdb.connect().execute(
        f"COPY (SELECT * FROM read_csv_auto('{write_persons(tmp_path)}')) "
        f"TO '{pq}' (FORMAT PARQUET)"
    )
    return pq


class TestSourceRegistration:
    @pytest.mark.parametrize("parquet, materialize, expected", [
        (True, None, "VIEW"),          # Parquet stays on disk
        (False, None, "BASE TABLE"),   # CSV is parsed once
        (True, True, "BASE TABLE"),
        (False, False, "VIEW"),
    ])
    def test_table_or_view(self, tmp_path, parquet, materialize, expected):
        import duckdb
        path = write_persons_parquet(tmp_path) if parquet else write_persons(tmp_path)
        con = duckdb.connect()
        rs.register_source(con, rs.SourceConfig(str(path), materialize=materialize))
        kind = con.execute("SELECT table_type FROM information_schema.tables "
                           "WHERE table_name = 'recs'").fetchone()[0]
        assert kind == expected
        assert con.execute("SELECT count(*) FROM recs").fetchone()[0] == 60

    @pytest.mark.parametrize("flag", ["--materialize", "--no-materialize"])
    def test_flags_run_end_to_end(self, tmp_path, flag):
        rc, rows = run_main(tmp_path, PLAIN_SPECS, flag)
        assert rc == 0 and [r["n"] for r in rows] == ["36", "18", "18"]

    def test_parquet_glob_source(self, tmp_path):
        # one file per state is the natural layout for a national run
        data = tmp_path / "data"
        data.mkdir()
        write_persons_parquet(data, "a.parquet")
        write_persons_parquet(data, "b.parquet")
        rc, rows = run_main(tmp_path, PLAIN_SPECS, persons=data / "*.parquet")
        assert rc == 0 and [r["n"] for r in rows] == ["72", "36", "36"]

    def test_source_path_with_quote(self, tmp_path):
        odd = tmp_path / "it's here"
        odd.mkdir()
        rc, rows = run_main(tmp_path, PLAIN_SPECS, persons=write_persons(odd))
        assert rc == 0 and len(rows) == 3

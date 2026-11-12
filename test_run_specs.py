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

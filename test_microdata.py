"""Tests for microdata.py — the --source reader shared by both scripts."""

import pytest

import microdata as md


class TestSourceReader:
    @pytest.mark.parametrize("name", ["p.parquet", "p.pq", "p.parq", "P.PARQUET"])
    def test_parquet_extensions(self, name):
        assert md.source_reader(name) == "read_parquet(?)"

    @pytest.mark.parametrize("name", ["p.csv", "p.tsv", "p.txt", "p.csv.gz", "p.csv.zst"])
    def test_csv_extensions(self, name):
        assert md.source_reader(name) == "read_csv_auto(?)"

    def test_unrecognised_extension_fails_fast(self):
        with pytest.raises(md.SourceError) as exc:
            md.source_reader("data/persons.dat")
        msg = str(exc.value)
        assert "persons.dat" in msg
        assert ".parquet" in msg and ".csv" in msg

    def test_extensionless_fails_fast(self):
        with pytest.raises(md.SourceError):
            md.source_reader("data/persons")


class TestNumericTypes:
    @pytest.mark.parametrize("t", [
        "INTEGER", "BIGINT", "HUGEINT", "UBIGINT", "TINYINT",
        "DECIMAL(18,3)", "DOUBLE", "FLOAT", "integer",
    ])
    def test_numeric(self, t):
        assert md.is_numeric_type(t)

    @pytest.mark.parametrize("t", ["INTERVAL", "VARCHAR", "DATE", "BOOLEAN", "POINT"])
    def test_not_numeric(self, t):
        assert not md.is_numeric_type(t)

"""Tests for build_specs.py — LDEF translation, hierarchy, shell validation.

Run:  pytest -q
"""

from pathlib import Path

import pytest

import build_specs as bs


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

SHELL_HEADER = "TBLID,LTYPE,HIERARCHY,LINENO,LDESC,LDEF\n"


def write_shell(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "shell.csv"
    p.write_text(SHELL_HEADER + body, encoding="utf-8")
    return p


def write_persons(tmp_path: Path) -> Path:
    """Tiny microdata file: RTYPE/QSEX numeric, CENRACE text (leading zero)."""
    p = tmp_path / "persons.csv"
    p.write_text(
        "person_id,RTYPE,CENRACE,RELSHIP,QSEX,QAGE\n"
        "1,3,01,20,1,34\n"
        "2,3,02,21,2,36\n"
        "3,5,01,38,1,7\n",
        encoding="utf-8",
    )
    return p


def run_main(tmp_path, shell_body, *extra):
    shell = write_shell(tmp_path, shell_body)
    out = tmp_path / "specs.csv"
    rc = bs.main(["--shell", str(shell), "--out", str(out), *extra])
    return rc, out


# --------------------------------------------------------------------------
# ldef_to_sql — no source typing (all values quoted)
# --------------------------------------------------------------------------

class TestLdefUntyped:
    def to_sql(self, ldef):
        return bs.ldef_to_sql(ldef, numeric_columns=set(), known=None)

    def test_single_value(self):
        assert self.to_sql("RELSHIP=20") == "RELSHIP = '20'"

    def test_comma_list(self):
        assert self.to_sql("RTYPE=3,5") == "RTYPE IN ('3', '5')"

    def test_range_expands_inclusive(self):
        assert self.to_sql("RELSHIP=23:25") == "RELSHIP IN ('23', '24', '25')"

    def test_range_preserves_zero_padding(self):
        assert self.to_sql("CENRACE=01:03") == "CENRACE IN ('01', '02', '03')"

    def test_mixed_list_and_range(self):
        assert self.to_sql("X=1,3:5") == "X IN ('1', '3', '4', '5')"

    def test_not_equal_forms(self):
        assert self.to_sql("RELSHIP!=21") == "RELSHIP <> '21'"
        assert self.to_sql("RELSHIP<>21") == "RELSHIP <> '21'"

    def test_not_equal_list(self):
        assert self.to_sql("X!=1,2") == "X NOT IN ('1', '2')"

    @pytest.mark.parametrize("op", [">=", "<=", ">", "<"])
    def test_comparisons(self, op):
        assert self.to_sql(f"QAGE{op}18") == f"QAGE {op} '18'"

    def test_and_binds_tighter_than_or(self):
        assert (
            self.to_sql("A=1 AND B=2 OR C=3")
            == "(A = '1' AND B = '2' OR C = '3')"
        )

    def test_parentheses_respected(self):
        assert (
            self.to_sql("(A=1 OR B=2) AND C=3")
            == "(A = '1' OR B = '2') AND C = '3'"
        )

    def test_quote_escaping(self):
        assert self.to_sql("NAME=O'BRIEN") == "NAME = 'O''BRIEN'"

    @pytest.mark.parametrize("bad", [
        "RELSHIP 20",          # no operator
        "QAGE>=18,20",         # comparison with a list
        "QAGE>=1:5",           # comparison with a range
        "X=36:23",             # backwards range
        "X=1:9999",            # absurd range
        "X=a:b",               # non-numeric endpoints
        "X=",                  # no values
        "",                    # empty
    ])
    def test_rejected_forms(self, bad):
        with pytest.raises(bs.LdefError):
            self.to_sql(bad)


# --------------------------------------------------------------------------
# ldef_to_sql — typed against source columns
# --------------------------------------------------------------------------

class TestLdefTyped:
    KNOWN = {"QAGE", "CENRACE", "RTYPE"}
    NUMERIC = {"QAGE", "RTYPE"}

    def to_sql(self, ldef):
        return bs.ldef_to_sql(ldef, self.NUMERIC, self.KNOWN)

    def test_numeric_unquoted(self):
        assert self.to_sql("QAGE>=18") == "QAGE >= 18"

    def test_text_quoted(self):
        assert self.to_sql("CENRACE=01") == "CENRACE = '01'"

    def test_unknown_variable_rejected(self):
        with pytest.raises(bs.LdefError, match="not a column"):
            self.to_sql("NOPE=1")

    def test_text_value_against_numeric_rejected(self):
        with pytest.raises(bs.LdefError, match="numeric in the source"):
            self.to_sql("QAGE=adult")

    def test_mixed_width_range_on_text_column_rejected(self):
        with pytest.raises(bs.LdefError, match="mixed-width"):
            self.to_sql("CENRACE=5:12")

    def test_equal_width_range_on_text_column_ok(self):
        assert self.to_sql("CENRACE=05:07") == "CENRACE IN ('05', '06', '07')"

    def test_mixed_width_range_on_numeric_column_ok(self):
        assert self.to_sql("QAGE=5:12").startswith("QAGE IN (5")

    def test_text_comparison_warns(self):
        bs.WARNINGS.clear()
        self.to_sql("CENRACE>=07")
        assert any("lexically" in w for w in bs.WARNINGS)


# --------------------------------------------------------------------------
# hierarchy resolution
# --------------------------------------------------------------------------

def line(lineno, hierarchy, ldef="X=1"):
    return bs.ShellLine("T", "D", hierarchy, lineno, f"line {lineno}", ldef, lineno)


class TestHierarchy:
    def test_ancestors_root_first(self):
        lines = [line(1, 0), line(2, 1), line(3, 2), line(4, 2)]
        chains, problems = bs.resolve_ancestors(lines)
        assert problems == []
        assert [a.lineno for a in chains[id(lines[2])]] == [1, 2]
        assert [a.lineno for a in chains[id(lines[3])]] == [1, 2]

    def test_dedent_pops_stack(self):
        # 0,1,2 then back to 1: the level-2 line must not remain an ancestor
        lines = [line(1, 0), line(2, 1), line(3, 2), line(4, 1)]
        chains, _ = bs.resolve_ancestors(lines)
        assert [a.lineno for a in chains[id(lines[3])]] == [1]

    def test_sibling_at_same_level_replaced(self):
        # Male(3) then Female(3): Female's children must not inherit Male
        lines = [line(1, 0), line(2, 1), line(3, 1), line(4, 2)]
        chains, _ = bs.resolve_ancestors(lines)
        assert [a.lineno for a in chains[id(lines[3])]] == [1, 3]

    def test_hierarchy_jump_reported(self):
        _, problems = bs.resolve_ancestors([line(1, 0), line(2, 3)])
        assert any("jumps" in p for p in problems)

    def test_orphan_nonzero_start_reported(self):
        _, problems = bs.resolve_ancestors([line(1, 2)])
        assert any("no parent" in p for p in problems)


# --------------------------------------------------------------------------
# end-to-end through main()
# --------------------------------------------------------------------------

GOOD_SHELL = """T1,H,0,,HEADER,10 CELLS
T1,U,0,,Universe: Everyone,"RTYPE=3,5"
T1,S,0,1,Total,"RTYPE=3,5"
T1,S,1,2,Householder,RELSHIP=20
T1,D,2,3,Male,QSEX=1
T1,D,2,4,Female,QSEX=2
"""


class TestEndToEnd:
    def test_good_shell_generates_specs(self, tmp_path, capsys):
        rc, out = run_main(tmp_path, GOOD_SHELL)
        assert rc == 0
        text = out.read_text()
        assert "UNION" not in text                    # specs, not the master
        assert text.count("SELECT") == 4              # one per S/D line
        # child carries universe + ancestors + self
        assert "RELSHIP = '20'" in text and "QSEX = '1'" in text

    def test_universe_applied_to_all_lines(self, tmp_path):
        _, out = run_main(tmp_path, GOOD_SHELL)
        import csv
        specs = list(csv.DictReader(out.open()))
        assert all("RTYPE IN ('3', '5')" in s["sql"] for s in specs)

    def test_duplicate_lineno_fails(self, tmp_path, capsys):
        body = GOOD_SHELL + "T1,D,2,4,Duplicate,QSEX=2\n"
        rc, _ = run_main(tmp_path, body)
        assert rc == 2
        assert "already used" in capsys.readouterr().err

    def test_missing_universe_fails(self, tmp_path, capsys):
        body = "T2,S,0,1,Total,RTYPE=3\n"
        rc, _ = run_main(tmp_path, body)
        assert rc == 2
        assert "no universe" in capsys.readouterr().err

    def test_header_rows_skipped(self, tmp_path):
        _, out = run_main(tmp_path, GOOD_SHELL)
        assert "HEADER" not in out.read_text()

    def test_bad_ltype_fails(self, tmp_path, capsys):
        rc, _ = run_main(tmp_path, "T1,Z,0,1,Bad,X=1\n")
        assert rc == 2
        assert "LTYPE" in capsys.readouterr().err

    def test_geo_placeholder(self, tmp_path):
        rc, out = run_main(tmp_path, GOOD_SHELL, "--geo", "{geo}")
        assert rc == 0
        text = out.read_text()
        assert "{geo} AS geoid" in text and "GROUP BY {geo}" in text

    def test_geo_bad_name_fails(self, tmp_path, capsys):
        rc, _ = run_main(tmp_path, GOOD_SHELL, "--geo", "bad name")
        assert rc == 2

    def test_source_typing(self, tmp_path):
        persons = write_persons(tmp_path)
        rc, out = run_main(tmp_path, GOOD_SHELL, "--source", str(persons))
        assert rc == 0
        text = out.read_text()
        assert "RTYPE IN (3, 5)" in text              # numeric: unquoted
        assert "QSEX = 1" in text

    def test_source_rejects_unknown_variable(self, tmp_path, capsys):
        persons = write_persons(tmp_path)
        body = GOOD_SHELL + "T1,D,1,5,Bad,NOPE=1\n"
        rc, _ = run_main(tmp_path, body, "--source", str(persons))
        assert rc == 2
        assert "not a column" in capsys.readouterr().err

    def test_unquoted_comma_in_ldef_rejected(self, tmp_path, capsys):
        # unquoted 'RTYPE=3,5' would silently truncate to 'RTYPE=3'
        body = GOOD_SHELL + "T1,D,1,5,Sneaky,RTYPE=3,5\n"
        rc, _ = run_main(tmp_path, body)
        assert rc == 2
        assert "missing quotes" in capsys.readouterr().err

    def test_errors_are_collected_not_first_only(self, tmp_path, capsys):
        body = GOOD_SHELL + "T1,D,1,5,Bad1,NOPE=1\nT1,D,1,6,Bad2,ALSO WRONG\n"
        rc, _ = run_main(tmp_path, body)
        err = capsys.readouterr().err
        assert rc == 2
        assert "cannot parse" in err
"""Hostile and oversized inputs. None of these may crash with a raw Python
error (RecursionError, TypeError, ...); each must either work or raise a
normal SQLError / SQLSyntaxError, and the database must stay usable."""

import pytest

import cinder
from cinder import SQLError, SQLSyntaxError


@pytest.fixture
def db(tmp_path):
    d = cinder.connect(str(tmp_path / "x.db"))
    d.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, x TEXT, n INTEGER)")
    d.execute("INSERT INTO t (x, n) VALUES ('a', 1), ('b', 2)")
    yield d
    assert d.execute("SELECT count(*) FROM t").scalar() >= 2  # still works afterwards
    d.close()


def test_thousands_of_and_conditions_just_work(db):
    where = " AND ".join(["n >= 1"] * 5000)
    assert db.execute(f"SELECT x FROM t WHERE {where} ORDER BY id").rows == [("a",), ("b",)]
    where = " OR ".join(["n = 99"] * 5000 + ["n = 2"])
    assert db.execute(f"SELECT x FROM t WHERE {where}").rows == [("b",)]


def test_long_in_lists(db):
    items = ", ".join(map(str, range(10000)))
    assert db.execute(f"SELECT count(*) FROM t WHERE n IN ({items})").scalar() == 2


@pytest.mark.parametrize("sql", [
    "SELECT " + "(" * 5000 + "1" + ")" * 5000,
    "SELECT " + "- " * 5000 + "1",   # spaced out: "--" would be a comment
    "SELECT " + "CASE WHEN 1 THEN " * 1000 + "1" + " END" * 1000,
    "SELECT " + "abs(" * 1000 + "1" + ")" * 1000,
])
def test_deep_nesting_is_a_syntax_error(db, sql):
    with pytest.raises(SQLSyntaxError, match="nested more than 64 levels"):
        db.execute(sql)


def test_reasonable_nesting_still_works(db):
    assert db.execute("SELECT " + "(" * 60 + "7" + ")" * 60).scalar() == 7
    # NOT chains are parsed in a loop and collapse to one or two NOTs
    assert db.execute("SELECT " + "NOT " * 5000 + "5").scalar() == 1
    assert db.execute("SELECT " + "NOT " * 5001 + "5").scalar() == 0


def test_huge_expression_chain_is_an_error_not_a_crash(db):
    with pytest.raises(SQLError, match="too complex"):
        db.execute("SELECT " + " + ".join(["1"] * 100000))


def test_big_numbers(db):
    assert db.execute("SELECT 99999999999999999999999 + 1").scalar() == 1e23
    with pytest.raises(SQLError, match="integer overflow"):
        db.execute("INSERT INTO t (n) VALUES (99999999999999999999999)")
    assert db.execute("SELECT 1e999 - 1e999").scalar() is None  # no NaN in SQL


def test_strings_with_odd_bytes(db):
    db.execute("INSERT INTO t (x) VALUES (?), ('a' || char(0) || 'b')", ["nul\x00inside 🔥"])
    assert db.execute("SELECT x FROM t WHERE x = ?", ["nul\x00inside 🔥"]).scalar() == "nul\x00inside 🔥"
    assert db.execute("SELECT length(x) FROM t WHERE x LIKE 'a_b'").scalar() == 3


def test_like_treats_regex_characters_literally(db):
    assert db.execute("SELECT 'a.b' LIKE 'a.b', 'axb' LIKE 'a.b', '(x)' LIKE '(x)', 'a' LIKE '[a]'").rows == [(1, 0, 1, 0)]


@pytest.mark.parametrize("sql, message", [
    ("", "expected one statement"),
    ("SELECT 1 /* never closed", "unterminated comment"),
    ("SELECT 'never closed", "unterminated string"),
])
def test_malformed_sql(db, sql, message):
    with pytest.raises(SQLSyntaxError, match=message):
        db.execute(sql)


def test_unsupported_parameter_types(db):
    with pytest.raises(SQLError, match="unsupported parameter type bytes"):
        db.execute("SELECT ?", [b"raw"])


def test_a_huge_cross_join_of_small_tables(db):
    assert db.execute("SELECT count(*) FROM " + ", ".join(f"t t{i}" for i in range(12))).scalar() == 2 ** 12


def test_failing_statement_in_a_transaction_leaves_it_usable(db):
    db.execute("BEGIN")
    db.execute("INSERT INTO t (x) VALUES ('kept')")
    with pytest.raises(SQLError):
        db.execute("SELECT " + " + ".join(["1"] * 100000))
    db.execute("COMMIT")
    assert db.execute("SELECT count(*) FROM t WHERE x = 'kept'").scalar() == 1

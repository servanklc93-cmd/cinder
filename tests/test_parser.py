import pytest

from cinder.sql import ast
from cinder.sql.lexer import SQLSyntaxError, tokenize
from cinder.sql.parser import parse, parse_one


def show(e):
    """Compact prefix form of an expression, to check precedence."""
    if isinstance(e, ast.Literal):
        return repr(e.value)
    if isinstance(e, ast.Column):
        return str(e)
    if isinstance(e, ast.Param):
        return f"?{e.index}"
    if isinstance(e, ast.Unary):
        return f"({e.op} {show(e.operand)})"
    if isinstance(e, ast.Binary):
        return f"({e.op} {show(e.left)} {show(e.right)})"
    if isinstance(e, ast.IsNull):
        return f"({'IS NOT' if e.negated else 'IS'} NULL {show(e.operand)})"
    if isinstance(e, ast.Between):
        return f"(BETWEEN {show(e.operand)} {show(e.low)} {show(e.high)})"
    if isinstance(e, ast.InList):
        return f"({'NOT IN' if e.negated else 'IN'} {show(e.operand)} {' '.join(map(show, e.items))})"
    if isinstance(e, ast.Call):
        return f"{e.name}({'*' if e.star else ' '.join(map(show, e.args))})"
    raise TypeError(e)


def where(sql):
    return show(parse_one("SELECT * FROM t WHERE " + sql).where)


def test_tokens():
    kinds = [(t.kind, t.value) for t in tokenize("SELECT 'it''s', 1.5e3, \"weird name\" <> ? -- comment")]
    assert kinds == [("kw", "SELECT"), ("str", "it's"), ("op", ","), ("num", 1500.0), ("op", ","),
                     ("ident", "weird name"), ("op", "!="), ("param", 0), ("eof", None)]


def test_precedence():
    assert where("a = 1 OR b = 2 AND c = 3") == "(OR (= a 1) (AND (= b 2) (= c 3)))"
    assert where("NOT a = 1 AND b") == "(AND (NOT (= a 1)) b)"
    assert where("a + b * c - d") == "(- (+ a (* b c)) d)"
    assert where("a < b = c > d") == "(= (< a b) (> c d))"
    assert where("a || b || c = 'x'") == "(= (|| (|| a b) c) 'x')"
    assert where("-a * -2") == "(* (- a) -2)"


def test_special_forms():
    assert where("x IS NOT NULL AND y IS NULL") == "(AND (IS NOT NULL x) (IS NULL y))"
    assert where("x BETWEEN 1 AND 5 AND y") == "(AND (BETWEEN x 1 5) y)"
    assert where("x NOT IN (1, 2, ?)") == "(NOT IN x 1 2 ?0)"
    assert where("name NOT LIKE 'a%'") == "(NOT (LIKE name 'a%'))"
    assert where("COUNT(*) > 1 AND t.x = upper(y)") == "(AND (> COUNT(*) 1) (= t.x UPPER(y)))"


def test_select_parts():
    s = parse_one("""
        SELECT DISTINCT u.name AS n, count(*) c, p.*
        FROM users u LEFT JOIN posts AS p ON p.user_id = u.id
        WHERE u.age > 18 GROUP BY u.name HAVING c > 2
        ORDER BY c DESC, n LIMIT 10 OFFSET 5
    """)
    assert s.distinct
    assert [i.alias for i in s.items] == ["n", "c", None]
    assert s.items[2].star_table == "p"
    assert s.table.ref == "u" and s.joins[0].table.ref == "p" and s.joins[0].left
    assert [o.desc for o in s.order_by] == [True, False]
    assert s.limit == ast.Literal(10) and s.offset == ast.Literal(5)


def test_create_table():
    t = parse_one("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name VARCHAR(40) NOT NULL UNIQUE, score REAL DEFAULT 0)")
    assert t.if_not_exists
    assert [(c.name, c.type) for c in t.columns] == [("id", "INTEGER"), ("name", "TEXT"), ("score", "REAL")]
    assert t.columns[0].primary_key and t.columns[1].not_null and t.columns[1].unique
    assert t.columns[2].default == ast.Literal(0)


def test_script_with_several_statements():
    stmts = parse("BEGIN; INSERT INTO t VALUES (1, 'a'), (2, NULL); COMMIT;")
    assert [type(s).__name__ for s in stmts] == ["Begin", "Insert", "Commit"]
    assert len(stmts[1].rows) == 2


@pytest.mark.parametrize("sql, message", [
    ("SELECT FROM t", "expected an expression"),
    ("SELECT * FROM", "expected a table name"),
    ("CREATE TABLE t (a BLOBBY)", "expected a column type"),
    ("SELECT 'oops", "unterminated string"),
    ("SELECT 1 SELECT 2", "';' between statements"),
    ("SELECT * FROM t WHERE a = ", "found end of input"),
    ("CREATE INDEX i ON t (a, b)", "multi-column indexes"),
])
def test_errors(sql, message):
    with pytest.raises(SQLSyntaxError, match=message):
        parse(sql)


def test_error_position():
    # the 5 is where a table name should be: line 2, column 6
    with pytest.raises(SQLSyntaxError, match=r"expected a table name, found '5' \(line 2, column 6\)"):
        parse("SELECT *\nFROM 5")

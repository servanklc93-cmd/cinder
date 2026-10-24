"""Differential testing against SQLite.

The same schema and the same random data go into cinder and into SQLite
(through the sqlite3 module, with STRICT tables so the typing rules match).
Then thousands of randomly generated queries run on both, and the results
must be identical, value for value and type for type.
"""

import math
import random
import sqlite3

import pytest

import cinder

SCHEMA = [
    "CREATE TABLE t1 (id INTEGER PRIMARY KEY, a INTEGER, b TEXT, c REAL)",
    "CREATE TABLE t2 (id INTEGER PRIMARY KEY, t1_id INTEGER, v INTEGER, s TEXT)",
    "CREATE INDEX t1_a ON t1 (a)",
    "CREATE INDEX t1_b ON t1 (b)",
    "CREATE INDEX t2_t1 ON t2 (t1_id)",
]

TEXTS = ["", "a", "ab", "abc", "B", "b", "ba", "é", "Zed", "a%b", "x_y"]


def normalize(rows):
    def norm(v):
        if isinstance(v, float):
            if math.isinf(v):
                return ("real", v)
            return ("real", float(f"{v:.10g}"))
        if isinstance(v, int):
            return ("integer", v)
        return (type(v).__name__, v)
    return [tuple(norm(v) for v in row) for row in rows]


def as_multiset(rows):
    return sorted(rows, key=repr)


@pytest.fixture(scope="module")
def dbs(tmp_path_factory):
    rng = random.Random(2024)
    lite = sqlite3.connect(":memory:")
    ours = cinder.connect(str(tmp_path_factory.mktemp("diff") / "diff.db"))
    for sql in SCHEMA:
        lite.execute(sql.replace(")", ") STRICT", 1) if sql.startswith("CREATE TABLE") else sql)
        ours.execute(sql)

    def maybe(value, p=0.15):
        return None if rng.random() < p else value

    t1 = [(i, maybe(rng.randint(-6, 6)), maybe(rng.choice(TEXTS)), maybe(rng.randint(-20, 20) / 4))
          for i in range(1, 201)]
    t2 = [(i, maybe(rng.randint(1, 230), 0.1), maybe(rng.randint(-50, 50)), maybe(rng.choice(TEXTS)))
          for i in range(1, 401)]
    for row in t1:
        lite.execute("INSERT INTO t1 VALUES (?, ?, ?, ?)", row)
        ours.execute("INSERT INTO t1 VALUES (?, ?, ?, ?)", row)
    for row in t2:
        lite.execute("INSERT INTO t2 VALUES (?, ?, ?, ?)", row)
        ours.execute("INSERT INTO t2 VALUES (?, ?, ?, ?)", row)
    yield lite, ours
    ours.close()


class Gen:
    """Random SQL that both engines must agree on."""

    def __init__(self, rng, tables):
        self.rng = rng
        self.tables = tables   # [(alias, table)]

    def pick(self, *options):
        return self.rng.choice(options)

    def col(self, kind):
        alias, table = self.rng.choice(self.tables)
        cols = {"t1": {"int": ["id", "a"], "text": ["b"], "real": ["c"]},
                "t2": {"int": ["id", "t1_id", "v"], "text": ["s"], "real": []}}[table]
        options = cols[kind] or cols["int"]
        return f"{alias}.{self.rng.choice(options)}"

    def int_expr(self, depth=0):
        r = self.rng.random()
        if depth > 1 or r < 0.45:
            return self.col("int")
        if r < 0.6:
            return str(self.rng.randint(-5, 5))
        if r < 0.85:
            op = self.pick("+", "-", "*", "/", "%")
            return f"({self.int_expr(depth + 1)} {op} {self.int_expr(depth + 1)})"
        return self.pick(f"abs({self.int_expr(depth + 1)})", f"coalesce({self.col('int')}, {self.rng.randint(-3, 3)})",
                         f"length({self.col('text')})", f"-{self.col('int')}")

    def real_expr(self):
        return self.pick(self.col("real"), f"({self.col('real')} * 2)", f"({self.col('real')} + {self.col('int')})",
                         f"({self.col('real')} / 2)", f"round({self.col('real')})")

    def text_expr(self):
        return self.pick(self.col("text"), f"upper({self.col('text')})", f"lower({self.col('text')})",
                         f"({self.col('text')} || 'x')", f"substr({self.col('text')}, 2)",
                         f"coalesce({self.col('text')}, 'none')", f"replace({self.col('text')}, 'a', 'Q')")

    def predicate(self, depth=0):
        r = self.rng.random()
        if depth < 2 and r < 0.25:
            op = self.pick("AND", "OR")
            return f"({self.predicate(depth + 1)} {op} {self.predicate(depth + 1)})"
        if depth < 2 and r < 0.32:
            return f"NOT ({self.predicate(depth + 1)})"
        kind = self.rng.random()
        cmp = self.pick("=", "!=", "<", "<=", ">", ">=")
        if kind < 0.35:
            return f"{self.int_expr()} {cmp} {self.int_expr()}"
        if kind < 0.45:
            return f"{self.real_expr()} {cmp} {self.rng.randint(-10, 10) / 2}"
        if kind < 0.6:
            return f"{self.text_expr()} {cmp} '{self.rng.choice(TEXTS)}'"
        if kind < 0.68:
            lo = self.rng.randint(-6, 3)
            return f"{self.col('int')} {self.pick('', 'NOT ')}BETWEEN {lo} AND {lo + self.rng.randint(0, 6)}"
        if kind < 0.76:
            items = ", ".join(self.pick(str(self.rng.randint(-6, 6)), "NULL") for _ in range(self.rng.randint(1, 4)))
            return f"{self.col('int')} {self.pick('', 'NOT ')}IN ({items})"
        if kind < 0.84:
            return f"{self.pick(self.col('int'), self.col('text'), self.col('real'))} IS {self.pick('', 'NOT ')}NULL"
        if kind < 0.92:
            pattern = self.pick("a%", "%b", "_", "A%", "%", "é%", "%a%", "__")
            return f"{self.col('text')} {self.pick('', 'NOT ')}LIKE '{pattern}'"
        return f"CASE WHEN {self.col('int')} > 0 THEN 1 ELSE {self.col('int')} END {cmp} {self.rng.randint(-3, 3)}"

    def scalar(self):
        return self.pick(self.int_expr, self.text_expr, self.real_expr)()


def from_clause(rng):
    shape = rng.random()
    if shape < 0.55:
        return "FROM t1", [("t1", "t1")]
    if shape < 0.8:
        return "FROM t1 JOIN t2 ON t2.t1_id = t1.id", [("t1", "t1"), ("t2", "t2")]
    if shape < 0.95:
        return "FROM t1 LEFT JOIN t2 ON t2.t1_id = t1.id", [("t1", "t1"), ("t2", "t2")]
    return "FROM t1, t2", [("t1", "t1"), ("t2", "t2")]


def random_query(rng):
    frm, tables = from_clause(rng)
    g = Gen(rng, tables)
    cross = frm == "FROM t1, t2"
    where = f" WHERE {g.predicate()}" + (" AND t1.id < 15 AND t2.id < 15" if cross else "")
    style = rng.random()

    if style < 0.5:
        cols = ", ".join(g.scalar() for _ in range(rng.randint(1, 3)))
        return f"SELECT {cols} {frm}{where}", False

    if style < 0.65:
        cols = ", ".join(g.scalar() for _ in range(rng.randint(1, 2)))
        return f"SELECT DISTINCT {cols} {frm}{where}", False

    if style < 0.85:
        key = g.pick(g.col("int"), g.col("text"), f"({g.col('int')} % 3)")
        aggs = ", ".join(g.pick("count(*)", f"count({g.col('int')})", f"sum({g.col('int')})",
                                f"min({g.col('text')})", f"max({g.col('real')})", f"total({g.col('int')})",
                                f"count(DISTINCT {g.col('int')})", f"max({g.col('int')})")
                         for _ in range(rng.randint(1, 3)))
        having = g.pick("", " HAVING count(*) > 1", f" HAVING sum({g.col('int')}) > 0")
        return f"SELECT {key}, {aggs} {frm}{where} GROUP BY {key}{having}", False

    # ordered output: break ties on the unique ids so the order is total
    ids = "t1.id" + (", t2.id" if len(tables) > 1 else "")
    order = f"{g.pick(g.col('int'), g.col('text'), g.col('real'))} {g.pick('ASC', 'DESC')}, {ids}"
    limit = g.pick("", f" LIMIT {rng.randint(0, 20)}", f" LIMIT {rng.randint(1, 10)} OFFSET {rng.randint(0, 10)}")
    return f"SELECT {ids}, {g.scalar()} {frm}{where} ORDER BY {order}{limit}", True


def test_aggregates_without_group_by(dbs):
    lite, ours = dbs
    for sql in [
        "SELECT count(*), sum(a), avg(a), min(b), max(c), total(c) FROM t1",
        "SELECT count(*), sum(a), min(b) FROM t1 WHERE 0",
        "SELECT count(*) FROM t1 JOIN t2 ON t2.t1_id = t1.id WHERE t2.v > 10",
    ]:
        assert normalize(ours.execute(sql).rows) == normalize(lite.execute(sql).fetchall()), sql


@pytest.mark.parametrize("seed", range(6))
def test_random_queries_match_sqlite(dbs, seed):
    lite, ours = dbs
    rng = random.Random(seed)
    for _ in range(500):
        sql, ordered = random_query(rng)
        expected = normalize(lite.execute(sql).fetchall())
        try:
            got = normalize(ours.execute(sql).rows)
        except Exception as e:
            raise AssertionError(f"cinder failed on:\n  {sql}\n{type(e).__name__}: {e}") from e
        if not ordered:
            expected, got = as_multiset(expected), as_multiset(got)
        assert got == expected, f"\n  {sql}\n  sqlite: {expected[:8]}\n  cinder: {got[:8]}"


@pytest.mark.parametrize("in_transaction", [False, True])
@pytest.mark.parametrize("seed", range(3))
def test_random_writes_match_sqlite(tmp_path, seed, in_transaction):
    # inside one big transaction, a statement that fails (a UNIQUE violation,
    # say) must be undone on its own while everything before it stays
    rng = random.Random(100 + seed)
    lite = sqlite3.connect(":memory:", isolation_level=None)
    ours = cinder.connect(str(tmp_path / "w.db"))
    for sql in SCHEMA[:1] + SCHEMA[2:4]:
        lite.execute(sql.replace(")", ") STRICT", 1) if sql.startswith("CREATE TABLE") else sql)
        ours.execute(sql)

    g = Gen(rng, [("t1", "t1")])
    if in_transaction:
        lite.execute("BEGIN")
        ours.execute("BEGIN")
    errors = 0
    for step in range(400):
        r = rng.random()
        existing = [i for (i,) in lite.execute("SELECT id FROM t1").fetchall()]
        if r < 0.4:
            row = (rng.choice([None, rng.randint(1, 120)]), rng.randint(-6, 6), rng.choice(TEXTS), rng.randint(-8, 8) / 4)
            sql = "INSERT INTO t1 (id, a, b, c) VALUES (?, ?, ?, ?)"
            args = row
        elif r < 0.55 and existing:
            # two good rows, then one that collides: the whole statement has to
            # be undone, including the two rows (and index entries) it already wrote
            dup = rng.choice(existing)
            sql = "INSERT INTO t1 (id, a, b) VALUES (NULL, ?, ?), (NULL, ?, ?), (?, 0, 'dup')"
            args = (rng.randint(-6, 6), rng.choice(TEXTS), rng.randint(-6, 6), rng.choice(TEXTS), dup)
        elif r < 0.8:
            sql = f"UPDATE t1 SET a = {g.int_expr()}, b = {g.text_expr()} WHERE {g.predicate()}"
            args = ()
        else:
            sql = f"DELETE FROM t1 WHERE {g.predicate()}"
            args = ()
        sql_unqualified = sql.replace("t1.", "")
        outcome = []
        for engine in (lite, ours):
            try:
                res = engine.execute(sql_unqualified, args)
                outcome.append(("ok", res.rowcount))
            except Exception as e:
                outcome.append(("error", type(e).__name__ in ("IntegrityError", "SQLError")))
        assert outcome[0] == outcome[1], f"step {step}: {sql_unqualified} {args}\n  sqlite {outcome[0]} cinder {outcome[1]}"
        errors += outcome[0][0] == "error"

    assert errors > 10  # the failure paths really were exercised
    if in_transaction:
        lite.execute("COMMIT")
        ours.execute("COMMIT")
    final = "SELECT * FROM t1 ORDER BY id"
    assert normalize(ours.execute(final).rows) == normalize(lite.execute(final).fetchall())
    # and the indexes agree with the table
    for col in ("a", "b"):
        q = f"SELECT id FROM t1 WHERE {col} IS NOT NULL ORDER BY {col}, id"
        assert normalize(ours.execute(q).rows) == normalize(lite.execute(q).fetchall())
    ours.close()

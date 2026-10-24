import os
import subprocess
import sys
import textwrap

import pytest

import cinder
from cinder import SQLError, SQLSyntaxError


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "app.db")


@pytest.fixture
def db(path):
    d = cinder.connect(path)
    d.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT, age INTEGER DEFAULT 18);
        CREATE INDEX users_age ON users (age);
        INSERT INTO users (email, name, age) VALUES
            ('ada@x.io', 'Ada', 36), ('linus@x.io', 'Linus', 54), ('grace@x.io', 'Grace', 85);
    """)
    yield d
    d.close()


def rows(db, sql, *params):
    return db.execute(sql, params).rows


def plan(db, sql):
    return "\n".join(r[0] for r in db.execute("EXPLAIN " + sql))


# --- basics -------------------------------------------------------------------------


def test_insert_returns_rowid_and_defaults_apply(db):
    r = db.execute("INSERT INTO users (email) VALUES ('new@x.io')")
    assert r.lastrowid == 4 and r.rowcount == 1
    assert rows(db, "SELECT name, age FROM users WHERE id = 4") == [(None, 18)]


def test_explicit_rowids_and_autoincrement_after_them(db):
    db.execute("INSERT INTO users (id, email) VALUES (100, 'a@b')")
    db.execute("INSERT INTO users (id, email) VALUES (5, 'c@d'), (NULL, 'e@f')")
    assert rows(db, "SELECT id FROM users WHERE email = 'e@f'") == [(101,)]


def test_parameters_are_values_not_sql(db):
    evil = "'; DROP TABLE users; --"
    db.execute("INSERT INTO users (email) VALUES (?)", [evil])
    assert rows(db, "SELECT email FROM users WHERE email = ?", evil) == [(evil,)]
    assert "users" in db.tables()
    with pytest.raises(SQLError, match="missing value for parameter 2"):
        db.execute("SELECT ? + ?", [1])


def test_update_sees_old_values(db):
    db.execute("UPDATE users SET age = age + 1, name = name || '-' || age WHERE id = 1")
    assert rows(db, "SELECT name, age FROM users WHERE id = 1") == [("Ada-36", 37)]


def test_update_and_delete_counts(db):
    assert db.execute("UPDATE users SET age = 1 WHERE age > 50").rowcount == 2
    assert db.execute("DELETE FROM users WHERE age = 1").rowcount == 2
    assert rows(db, "SELECT count(*) FROM users") == [(1,)]


def test_changing_the_rowid(db):
    db.execute("UPDATE users SET id = 10 WHERE id = 1")
    assert rows(db, "SELECT email FROM users WHERE id = 10") == [("ada@x.io",)]
    with pytest.raises(SQLError, match="UNIQUE constraint failed: users.id"):
        db.execute("UPDATE users SET id = 2 WHERE id = 10")


# --- constraints --------------------------------------------------------------------


def test_constraints(db):
    with pytest.raises(SQLError, match="UNIQUE constraint failed: users.email"):
        db.execute("INSERT INTO users (email) VALUES ('ada@x.io')")
    with pytest.raises(SQLError, match="NOT NULL constraint failed: users.email"):
        db.execute("INSERT INTO users (name) VALUES ('nobody')")
    with pytest.raises(SQLError, match="cannot store TEXT value in INTEGER column users.age"):
        db.execute("INSERT INTO users (email, age) VALUES ('t@x', 'old')")
    with pytest.raises(SQLError, match="UNIQUE constraint failed: users.email"):
        db.execute("UPDATE users SET email = 'ada@x.io' WHERE id = 2")


def test_unique_allows_many_nulls(path):
    with cinder.connect(path) as d:
        d.execute("CREATE TABLE t (x TEXT UNIQUE)")
        d.execute("INSERT INTO t VALUES (NULL), (NULL), ('a')")
        assert d.execute("SELECT count(*) FROM t").scalar() == 3


def test_text_primary_key_keeps_its_rules_after_reopen(path):
    with cinder.connect(path) as d:
        d.execute("CREATE TABLE tags (name TEXT PRIMARY KEY)")
        d.execute("INSERT INTO tags VALUES ('a')")
    with cinder.connect(path) as d:
        with pytest.raises(SQLError, match="NOT NULL"):
            d.execute("INSERT INTO tags VALUES (NULL)")
        with pytest.raises(SQLError, match="UNIQUE"):
            d.execute("INSERT INTO tags VALUES ('a')")


def test_failed_multirow_insert_inserts_nothing(db):
    with pytest.raises(SQLError):
        db.execute("INSERT INTO users (email) VALUES ('ok1@x'), ('ok2@x'), ('ada@x.io')")
    assert rows(db, "SELECT count(*) FROM users") == [(3,)]


def test_unique_index_creation_checks_existing_rows(db):
    db.execute("INSERT INTO users (email, age) VALUES ('dup@x', 36)")
    with pytest.raises(SQLError, match="UNIQUE constraint failed: users.age"):
        db.execute("CREATE UNIQUE INDEX u_age ON users (age)")
    assert "u_age" not in plan(db, "SELECT * FROM users WHERE age = 1")


# --- transactions -------------------------------------------------------------------


def test_rollback(db):
    db.execute("BEGIN")
    db.execute("INSERT INTO users (email) VALUES ('tmp@x')")
    db.execute("CREATE TABLE scratch (x INTEGER)")
    assert rows(db, "SELECT count(*) FROM users") == [(4,)]
    db.execute("ROLLBACK")
    assert rows(db, "SELECT count(*) FROM users") == [(3,)]
    assert db.tables() == ["users"]


def test_failed_statement_inside_a_transaction_only_undoes_itself(db):
    db.execute("BEGIN")
    db.execute("INSERT INTO users (email) VALUES ('kept@x')")
    with pytest.raises(SQLError):
        db.execute("INSERT INTO users (email) VALUES ('also@x'), ('ada@x.io')")
    db.execute("COMMIT")
    assert rows(db, "SELECT email FROM users WHERE id > 3") == [("kept@x",)]


def test_uncommitted_transaction_is_gone_after_reopen(path, db):
    db.execute("BEGIN")
    db.execute("DELETE FROM users")
    db.close()
    with cinder.connect(path) as d:
        assert d.execute("SELECT count(*) FROM users").scalar() == 3


def test_transaction_errors(db):
    with pytest.raises(SQLError, match="no transaction is active"):
        db.execute("COMMIT")
    db.execute("BEGIN")
    with pytest.raises(SQLError, match="within a transaction"):
        db.execute("BEGIN")


CRASH_SCRIPT = textwrap.dedent("""
    import sys, cinder
    db = cinder.connect(sys.argv[1])
    db.execute("BEGIN")
    db.execute("UPDATE accounts SET balance = balance - 100 WHERE id = 1")
    for i in range(300):
        db.execute("INSERT INTO log (note) VALUES (?)", ["x" * 200])
    db.execute("UPDATE accounts SET balance = balance + 100 WHERE id = 2")
    db.execute("COMMIT")   # the process is killed in the middle of writing this
""")


@pytest.mark.parametrize("crash_after", [1, 4, 12])
def test_a_crash_during_commit_never_leaves_half_a_transfer(path, crash_after):
    with cinder.connect(path) as d:
        d.executescript("""
            CREATE TABLE accounts (id INTEGER PRIMARY KEY, balance INTEGER NOT NULL);
            CREATE TABLE log (id INTEGER PRIMARY KEY, note TEXT);
            INSERT INTO accounts VALUES (1, 500), (2, 500);
        """)
    env = dict(os.environ, CINDER_TEST_CRASH_AFTER_PAGES=str(crash_after))
    assert subprocess.run([sys.executable, "-c", CRASH_SCRIPT, path], env=env).returncode == 17

    with cinder.connect(path) as d:
        assert d.execute("SELECT balance FROM accounts ORDER BY id").rows == [(500,), (500,)]
        assert d.execute("SELECT count(*) FROM log").scalar() == 0


# --- schema -------------------------------------------------------------------------


def test_schema_survives_reopen(path, db):
    db.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT)")
    db.execute("CREATE INDEX posts_user ON posts (user_id)")
    db.execute("INSERT INTO posts (user_id, title) VALUES (1, 'hello')")
    db.close()
    with cinder.connect(path) as d:
        assert d.tables() == ["posts", "users"]
        assert "posts_user" in plan(d, "SELECT * FROM posts WHERE user_id = 1")
        assert d.execute("SELECT title FROM posts WHERE user_id = 1").rows == [("hello",)]


def test_drop_table_and_index(db):
    db.execute("DROP INDEX users_age")
    assert "SCAN users" in plan(db, "SELECT * FROM users WHERE age = 36")
    with pytest.raises(SQLError, match="cannot be dropped"):
        db.execute("DROP INDEX sqlite_autoindex_users_1")
    db.execute("DROP TABLE users")
    assert db.tables() == []
    db.execute("DROP TABLE IF EXISTS users")
    with pytest.raises(SQLError, match="no such table"):
        db.execute("SELECT * FROM users")


def test_dropped_pages_are_reused(path):
    with cinder.connect(path) as d:
        d.execute("CREATE TABLE big (x TEXT)")
        for _ in range(20):
            d.execute("INSERT INTO big VALUES (?), (?), (?), (?), (?)", ["y" * 500] * 5)
        size = os.path.getsize(path)
        d.execute("DROP TABLE big")
        d.execute("CREATE TABLE big2 (x TEXT)")
        for _ in range(20):
            d.execute("INSERT INTO big2 VALUES (?), (?), (?), (?), (?)", ["y" * 500] * 5)
        assert os.path.getsize(path) <= size + 4096


def test_vacuum_shrinks_the_file(path):
    with cinder.connect(path) as d:
        d.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, x TEXT UNIQUE)")
        d.execute("CREATE INDEX t_x ON t (x)")
        for i in range(400):
            d.execute("INSERT INTO t (x) VALUES (?)", [f"{i:05d}" + "z" * 300])
        d.execute("DELETE FROM t WHERE id > 20")
        before = os.path.getsize(path)
        d.execute("VACUUM")
        assert os.path.getsize(path) < before / 3
        assert d.execute("SELECT count(*), min(id), max(id) FROM t").rows == [(20, 1, 20)]
        # both indexes rebuilt; the unique one wins for an equality lookup
        assert [ix.name for ix in d.catalog.get("t").indexes] == ["sqlite_autoindex_t_1", "t_x"]
        assert "USING INDEX sqlite_autoindex_t_1" in plan(d, "SELECT * FROM t WHERE x = '00003'")
        assert d.execute("SELECT id FROM t WHERE x = ?", ["00003" + "z" * 300]).rows == [(4,)]
        with pytest.raises(SQLError, match="UNIQUE"):
            d.execute("INSERT INTO t (x) VALUES (?)", ["00001" + "z" * 300])


# --- planning -----------------------------------------------------------------------


def test_access_paths(db):
    assert "USING ROWID (id=2)" in plan(db, "SELECT * FROM users WHERE id = 2")
    assert "USING ROWID RANGE" in plan(db, "SELECT * FROM users WHERE id BETWEEN 1 AND 2")
    assert "USING INDEX sqlite_autoindex_users_1" in plan(db, "SELECT * FROM users WHERE email = 'x'")
    assert "USING INDEX users_age (age>40)" in plan(db, "SELECT * FROM users WHERE age > 40")
    assert "SCAN users" in plan(db, "SELECT * FROM users WHERE name = 'Ada'")


def test_order_by_index_skips_the_sort_and_stops_early(db):
    p = plan(db, "SELECT name FROM users ORDER BY age LIMIT 1")
    assert "SCAN users USING INDEX users_age" in p and "SORT" not in p
    assert rows(db, "SELECT name FROM users ORDER BY age LIMIT 1") == [("Ada",)]
    assert "SORT" in plan(db, "SELECT name FROM users ORDER BY age DESC")


def test_join_uses_index_on_the_inner_table(db):
    db.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT)")
    db.execute("CREATE INDEX posts_user ON posts (user_id)")
    p = plan(db, "SELECT * FROM users u JOIN posts p ON p.user_id = u.id")
    assert "SEARCH posts USING INDEX posts_user (user_id=u.id)" in p


# --- errors and edge cases ----------------------------------------------------------


@pytest.mark.parametrize("sql, message", [
    ("SELECT nope FROM users", "no such column: nope"),
    ("SELECT * FROM missing", "no such table: missing"),
    ("SELECT id FROM users a JOIN users b ON a.id = b.id", "ambiguous column name: id"),
    ("SELECT count(*) FROM users WHERE count(*) > 1", "not allowed in WHERE"),
    ("SELECT nosuch(1)", "no such function: NOSUCH"),
    ("SELECT upper(1, 2)", "wrong number of arguments"),
    ("SELECT sum(sum(age)) FROM users", "misuse of aggregate"),
    ("CREATE TABLE users (x INTEGER)", "already exists"),
    ("CREATE TABLE t (a INTEGER, a TEXT)", "duplicate column name"),
    ("SELECT * FROM users ORDER BY 9", "ORDER BY term out of range"),
])
def test_error_messages(db, sql, message):
    with pytest.raises(SQLError, match=message):
        db.execute(sql)


def test_syntax_errors_point_at_the_problem(db):
    with pytest.raises(SQLSyntaxError, match=r"expected a table name, found '7' \(line 1, column 15\)"):
        db.execute("SELECT * FROM 7")


def test_row_too_large(db):
    with pytest.raises(SQLError, match="row too large"):
        db.execute("INSERT INTO users (email) VALUES (?)", ["x" * 5000])


def test_closed_database(path):
    d = cinder.connect(path)
    d.close()
    with pytest.raises(SQLError, match="closed"):
        d.execute("SELECT 1")


def test_select_without_from(db):
    assert rows(db, "SELECT 1 + 2, 'a' || 'b', 7 / 2, NULL IS NULL") == [(3, "ab", 3, 1)]
    assert rows(db, "SELECT 1 WHERE 0") == []

import io

import cinder
from cinder.cli import Shell, main, render


def shell(tmp_path):
    out = io.StringIO()
    return Shell(cinder.connect(str(tmp_path / "s.db")), out), out


def test_script_and_query(tmp_path, capsys):
    path = str(tmp_path / "c.db")
    assert main([path, "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT); INSERT INTO t (name) VALUES ('a'), (NULL);"]) == 0
    assert main([path, "SELECT * FROM t ORDER BY id;"]) == 0
    out = capsys.readouterr().out
    assert "2 rows affected" in out
    assert "id  name\n--  ----\n1   a\n2   NULL" in out


def test_errors_set_the_exit_code(tmp_path, capsys):
    assert main([str(tmp_path / "e.db"), "SELECT * FROM nope;"]) == 1
    assert "Error: no such table: nope" in capsys.readouterr().out


def test_dot_commands(tmp_path):
    sh, out = shell(tmp_path)
    sh.run_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE); CREATE INDEX u_email ON users (email);")
    for cmd in (".tables", ".schema", ".indexes", ".nope"):
        sh.dot(cmd)
    text = out.getvalue()
    assert "users" in text
    assert "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE);" in text
    assert "CREATE INDEX u_email ON users (email);" in text
    assert "sqlite_autoindex_users_1  (users.email, unique)" in text
    assert "unknown command .nope" in text
    assert sh.dot(".quit") is False


def test_explain_prints_the_plan(tmp_path):
    sh, out = shell(tmp_path)
    sh.run_sql("CREATE TABLE t (id INTEGER PRIMARY KEY); EXPLAIN SELECT * FROM t WHERE id = 3;")
    assert "SEARCH t USING ROWID (id=3)" in out.getvalue()


def test_render_truncates_long_results():
    text = render(["n"], [(i,) for i in range(250)], limit=200)
    assert text.endswith("... 50 more rows")

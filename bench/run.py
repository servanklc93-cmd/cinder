"""cinder vs SQLite on the same machine, same workloads, both on disk.

    python bench/run.py

Both engines fsync on commit (Python's sqlite3 defaults to a rollback
journal with synchronous=FULL, the same scheme cinder uses).
"""

import os
import random
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cinder  # noqa: E402

N = 20_000


def timed(fn):
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


class Lite:
    def __init__(self, path):
        self.c = sqlite3.connect(path, isolation_level=None)

    def execute(self, sql, params=()):
        return self.c.execute(sql, params).fetchall()


class Ours:
    def __init__(self, path):
        self.db = cinder.connect(path)

    def execute(self, sql, params=()):
        return self.db.execute(sql, params).rows


def workloads(db):
    rng = random.Random(1)
    cities = [f"city{i}" for i in range(50)]
    rows = [(f"user{i}@x.io", rng.randint(18, 90), rng.choice(cities)) for i in range(N)]
    ids = [rng.randint(1, N) for _ in range(5000)]
    emails = [f"user{rng.randrange(N)}@x.io" for _ in range(2000)]

    def setup():
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE, age INTEGER, city TEXT)")
        db.execute("CREATE INDEX users_age ON users (age)")
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, total REAL)")
        db.execute("CREATE INDEX orders_user ON orders (user_id)")

    def bulk_insert():
        db.execute("BEGIN")
        for r in rows:
            db.execute("INSERT INTO users (email, age, city) VALUES (?, ?, ?)", r)
        for i in range(N):
            db.execute("INSERT INTO orders (user_id, total) VALUES (?, ?)", (rng.randint(1, N), rng.randint(1, 500) / 4))
        db.execute("COMMIT")

    def autocommit_inserts():
        for i in range(100):
            db.execute("INSERT INTO orders (user_id, total) VALUES (?, ?)", (i, 1.0))

    def pk_lookups():
        for i in ids:
            db.execute("SELECT email FROM users WHERE id = ?", (i,))

    def index_lookups():
        for e in emails:
            db.execute("SELECT id FROM users WHERE email = ?", (e,))

    def range_scan():
        for _ in range(20):
            db.execute("SELECT count(*) FROM users WHERE age BETWEEN 30 AND 35")

    def full_scan_group_by():
        db.execute("SELECT city, count(*), avg(age) FROM users GROUP BY city")

    def join():
        db.execute("""SELECT u.city, sum(o.total) FROM users u JOIN orders o ON o.user_id = u.id
                      WHERE u.age < 25 GROUP BY u.city""")

    def top_n():
        for _ in range(200):
            db.execute("SELECT email FROM users ORDER BY age LIMIT 10")

    return [
        ("setup (create tables)", setup, None),
        (f"insert {2 * N:,} rows in one transaction", bulk_insert, 2 * N),
        ("100 inserts, each its own commit (fsync)", autocommit_inserts, 100),
        ("5,000 lookups by primary key", pk_lookups, 5000),
        ("2,000 lookups by unique index", index_lookups, 2000),
        ("20 index range scans (~1,700 rows each)", range_scan, 20),
        (f"GROUP BY over all {N:,} rows", full_scan_group_by, None),
        ("join + GROUP BY (index nested loop)", join, None),
        ("200 x ORDER BY indexed column LIMIT 10", top_n, 200),
    ]


def main():
    results = {}
    for name, factory in [("sqlite", Lite), ("cinder", Ours)]:
        path = os.path.join(tempfile.mkdtemp(), "bench.db")
        db = factory(path)
        results[name] = [(label, timed(fn)) for label, fn, _ in workloads(db)]
        results[name + "_size"] = os.path.getsize(path)
        db.execute("VACUUM")
        results[name + "_vacuumed"] = os.path.getsize(path)

    print(f"python {sys.version.split()[0]}, sqlite {sqlite3.sqlite_version}\n")
    print("| workload | SQLite | cinder | ratio |")
    print("|---|---:|---:|---:|")
    for (label, t_lite), (_, t_ours) in zip(results["sqlite"][1:], results["cinder"][1:]):
        print(f"| {label} | {t_lite * 1000:,.1f} ms | {t_ours * 1000:,.1f} ms | {t_ours / t_lite:,.0f}x |")
    print(f"\nfile size: sqlite {results['sqlite_size'] / 1024:,.0f} KB, cinder {results['cinder_size'] / 1024:,.0f} KB")
    print(f"after VACUUM: sqlite {results['sqlite_vacuumed'] / 1024:,.0f} KB, cinder {results['cinder_vacuumed'] / 1024:,.0f} KB")


if __name__ == "__main__":
    main()

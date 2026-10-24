import os
import subprocess
import sys
import textwrap

import pytest

from cinder.pager import PAGE_SIZE, CorruptDatabase, Pager


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "test.db")


def fill(pager, pgno, byte):
    pager.write(pgno)[:] = bytes([byte]) * PAGE_SIZE


def test_new_file_has_a_header(path):
    Pager(path).close()
    assert os.path.getsize(path) == PAGE_SIZE
    with open(path, "rb") as f:
        assert f.read(16) == b"cinder format 1\x00"


def test_commit_persists(path):
    p = Pager(path)
    a, b = p.allocate(), p.allocate()
    fill(p, a, 1)
    fill(p, b, 2)
    p.schema_root = a
    p.commit()
    p.close()

    p = Pager(path)
    assert p.page_count == 3
    assert p.schema_root == a
    assert p.read(a)[0] == 1 and p.read(b)[-1] == 2


def test_uncommitted_changes_are_lost(path):
    p = Pager(path)
    pg = p.allocate()
    fill(p, pg, 9)
    p.commit()
    fill(p, pg, 5)
    p.allocate()
    p.close()  # no commit

    p = Pager(path)
    assert p.page_count == 2
    assert p.read(pg)[0] == 9


def test_rollback(path):
    p = Pager(path)
    pg = p.allocate()
    fill(p, pg, 1)
    p.commit()
    fill(p, pg, 2)
    p.allocate()
    p.rollback()
    assert p.page_count == 2
    assert p.read(pg)[0] == 1


def test_savepoint_undoes_only_later_changes(path):
    p = Pager(path)
    a, b = p.allocate(), p.allocate()
    p.commit()
    fill(p, a, 1)
    sp = p.savepoint()
    fill(p, a, 2)
    fill(p, b, 3)
    c = p.allocate()
    p.rollback_to(sp)
    assert p.read(a)[0] == 1     # changed before the savepoint: kept
    assert p.read(b)[0] == 0     # changed after: back to committed
    assert p.page_count == c     # allocation undone


def test_free_pages_are_reused(path):
    p = Pager(path)
    a, b = p.allocate(), p.allocate()
    p.commit()
    p.free(a)
    assert p.allocate() == a
    assert p.allocate() == b + 1


def test_rejects_other_files(path):
    with open(path, "wb") as f:
        f.write(b"definitely not a database".ljust(PAGE_SIZE, b"\0"))
    with pytest.raises(CorruptDatabase):
        Pager(path)


CRASH_SCRIPT = textwrap.dedent("""
    import sys
    from cinder.pager import Pager, PAGE_SIZE
    p = Pager(sys.argv[1])
    for pgno in range(1, p.page_count):
        p.write(pgno)[:] = b"\\x02" * PAGE_SIZE   # overwrite every page...
    for _ in range(5):
        p.write(p.allocate())[:] = b"\\x02" * PAGE_SIZE   # ...and grow the file
    p.commit()   # dies partway through via the test hook
""")


@pytest.mark.parametrize("crash_after", [1, 5, 9])
def test_recovers_from_a_crash_in_the_middle_of_commit(path, crash_after):
    p = Pager(path)
    for _ in range(10):
        p.write(p.allocate())[:] = b"\x01" * PAGE_SIZE
    p.commit()
    p.close()

    env = dict(os.environ, CINDER_TEST_CRASH_AFTER_PAGES=str(crash_after))
    result = subprocess.run([sys.executable, "-c", CRASH_SCRIPT, path], env=env)
    assert result.returncode == 17  # really died inside commit
    assert os.path.exists(path + "-journal")

    # the file on disk is now a mix of old and new pages; opening it must
    # roll back to the old version completely
    p = Pager(path)
    assert not os.path.exists(path + "-journal")
    assert p.page_count == 11
    assert os.path.getsize(path) == 11 * PAGE_SIZE
    assert all(p.read(pgno) == b"\x01" * PAGE_SIZE for pgno in range(1, 11))


def test_a_torn_journal_is_ignored(path):
    # journal written halfway, db never touched: just discard it
    p = Pager(path)
    pg = p.allocate()
    fill(p, pg, 7)
    p.commit()
    p.close()
    with open(path + "-journal", "wb") as j:
        j.write(b"cinder jour")
    p = Pager(path)
    assert p.read(pg)[0] == 7
    assert not os.path.exists(path + "-journal")


LOCK_SCRIPT = textwrap.dedent("""
    import sys
    from cinder.pager import Pager, DatabaseLocked
    try:
        Pager(sys.argv[1])
    except DatabaseLocked as e:
        print("locked:", e)
        sys.exit(3)
""")


def test_a_second_process_cannot_open_it(path):
    p = Pager(path)
    result = subprocess.run([sys.executable, "-c", LOCK_SCRIPT, path], capture_output=True, text=True)
    assert result.returncode == 3, result.stderr
    assert "is open in another process" in result.stdout
    p.close()
    # released on close
    assert subprocess.run([sys.executable, "-c", LOCK_SCRIPT, path]).returncode == 0

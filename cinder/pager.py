"""Fixed-size pages on disk, a page cache and atomic commit.

Page 0 is the file header. Everything else is B+tree nodes or free pages.

Changes stay in memory until commit. Commit is the classic rollback-journal
dance SQLite uses:

    1. copy the original content of every page we're about to overwrite
       into <db>-journal, and fsync it
    2. write the new pages into the database file, and fsync it
    3. delete the journal

If the process dies during step 2, the next open finds the journal and
copies the originals back, so the file is either fully the old version or
fully the new one, never a mix.
"""

import os
import struct
import zlib
from collections import OrderedDict

PAGE_SIZE = 4096
MAGIC = b"cinder format 1\x00"
HEADER = struct.Struct(">16sHIII")  # magic, page size, page count, freelist head, schema root
JOURNAL_MAGIC = b"cinder journal 1"
JOURNAL_HEADER = struct.Struct(">16sI")  # magic, original page count
JOURNAL_RECORD = struct.Struct(">II")  # page number, crc32 of the page
CACHE_PAGES = 2048

# test hook: die (for real, os._exit) after writing this many pages during
# commit, to prove recovery works. never set outside the crash tests.
_CRASH_AFTER_PAGES = int(os.environ.get("CINDER_TEST_CRASH_AFTER_PAGES", "-1"))


class CorruptDatabase(Exception):
    pass


class Pager:
    def __init__(self, path):
        self.path = path
        self.journal_path = path + "-journal"
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        self.file = open(path, "r+b" if exists else "w+b")

        if os.path.exists(self.journal_path):
            self._recover()

        self.cache = OrderedDict()   # pgno -> bytearray, clean pages, LRU
        self.dirty = {}              # pgno -> bytearray, changed since commit
        self.originals = {}          # pgno -> bytes, content before this transaction

        if exists:
            self._read_header()
        else:
            self.page_count = 1
            self.freelist = 0
            self.schema_root = 0
            self._write_header_to_disk()
        self._committed_header = (self.page_count, self.freelist, self.schema_root)

    # --- header ---------------------------------------------------------------

    def _read_header(self):
        self.file.seek(0)
        raw = self.file.read(HEADER.size)
        if len(raw) < HEADER.size:
            raise CorruptDatabase("file is too short to be a cinder database")
        magic, page_size, self.page_count, self.freelist, self.schema_root = HEADER.unpack(raw)
        if magic != MAGIC:
            raise CorruptDatabase("not a cinder database")
        if page_size != PAGE_SIZE:
            raise CorruptDatabase(f"unsupported page size {page_size}")

    def _header_page(self):
        page = bytearray(PAGE_SIZE)
        HEADER.pack_into(page, 0, MAGIC, PAGE_SIZE, self.page_count, self.freelist, self.schema_root)
        return page

    def _write_header_to_disk(self):
        self.file.seek(0)
        self.file.write(self._header_page())
        self.file.flush()
        os.fsync(self.file.fileno())

    # --- pages ----------------------------------------------------------------

    def read(self, pgno):
        """A page for reading. Don't modify the returned buffer; use write()."""
        page = self.dirty.get(pgno)
        if page is not None:
            return page
        page = self.cache.get(pgno)
        if page is not None:
            self.cache.move_to_end(pgno)
            return page
        if not 0 < pgno < self.page_count:
            raise CorruptDatabase(f"page {pgno} out of range")
        self.file.seek(pgno * PAGE_SIZE)
        page = bytearray(self.file.read(PAGE_SIZE))
        if len(page) < PAGE_SIZE:
            page.extend(bytes(PAGE_SIZE - len(page)))
        self.cache[pgno] = page
        if len(self.cache) > CACHE_PAGES:
            self.cache.popitem(last=False)
        return page

    def write(self, pgno):
        """A page for modification. The original is kept for the journal."""
        page = self.dirty.get(pgno)
        if page is not None:
            return page
        page = self.read(pgno)
        if pgno < self._committed_header[0]:
            self.originals.setdefault(pgno, bytes(page))
        self.cache.pop(pgno, None)
        self.dirty[pgno] = page
        return page

    def allocate(self):
        if self.freelist:
            pgno = self.freelist
            self.freelist = struct.unpack_from(">I", self.read(pgno), 0)[0]
        else:
            pgno = self.page_count
            self.page_count += 1
            self.dirty[pgno] = bytearray(PAGE_SIZE)
            return pgno
        page = self.write(pgno)
        page[:] = bytes(PAGE_SIZE)
        return pgno

    def free(self, pgno):
        page = self.write(pgno)
        page[:] = bytes(PAGE_SIZE)
        struct.pack_into(">I", page, 0, self.freelist)
        self.freelist = pgno

    # --- transactions ---------------------------------------------------------

    @property
    def has_changes(self):
        return bool(self.dirty) or self._committed_header != (self.page_count, self.freelist, self.schema_root)

    def commit(self):
        if not self.has_changes:
            return
        original_count = self._committed_header[0]

        # 1. journal the originals of every existing page we're about to
        # overwrite (page 0 always changes, it holds the page count)
        self.originals.setdefault(0, self._read_raw(0))
        with open(self.journal_path, "wb") as j:
            j.write(JOURNAL_HEADER.pack(JOURNAL_MAGIC, original_count))
            for pgno, data in sorted(self.originals.items()):
                j.write(JOURNAL_RECORD.pack(pgno, zlib.crc32(data)))
                j.write(data)
            j.flush()
            os.fsync(j.fileno())
        _fsync_dir(self.journal_path)

        # 2. write the new pages, header last
        written = 0
        for pgno in sorted(self.dirty):
            self.file.seek(pgno * PAGE_SIZE)
            self.file.write(self.dirty[pgno])
            written += 1
            if written == _CRASH_AFTER_PAGES:
                self.file.flush()
                os._exit(17)
        self.file.seek(0)
        self.file.write(self._header_page())
        self.file.truncate(self.page_count * PAGE_SIZE)
        self.file.flush()
        os.fsync(self.file.fileno())

        # 3. the commit point: once the journal is gone, the new version is it
        os.remove(self.journal_path)
        _fsync_dir(self.journal_path)

        for pgno, page in self.dirty.items():
            self.cache[pgno] = page
        while len(self.cache) > CACHE_PAGES:
            self.cache.popitem(last=False)
        self.dirty.clear()
        self.originals.clear()
        self._committed_header = (self.page_count, self.freelist, self.schema_root)

    def rollback(self):
        self.dirty.clear()
        self.originals.clear()
        self.page_count, self.freelist, self.schema_root = self._committed_header

    def savepoint(self):
        """Snapshot of the uncommitted state, so one failed statement can be
        undone without losing the rest of an open transaction."""
        return (
            {pgno: bytes(page) for pgno, page in self.dirty.items()},
            (self.page_count, self.freelist, self.schema_root),
        )

    def rollback_to(self, savepoint):
        pages, (self.page_count, self.freelist, self.schema_root) = savepoint
        for pgno in list(self.dirty):
            if pgno not in pages:
                del self.dirty[pgno]
                self.originals.pop(pgno, None)
        for pgno, data in pages.items():
            self.dirty[pgno] = bytearray(data)

    # --- recovery -------------------------------------------------------------

    def _read_raw(self, pgno):
        self.file.seek(pgno * PAGE_SIZE)
        data = self.file.read(PAGE_SIZE)
        return data + bytes(PAGE_SIZE - len(data))

    def _recover(self):
        with open(self.journal_path, "rb") as j:
            raw = j.read()
        if len(raw) >= JOURNAL_HEADER.size:
            magic, original_count = JOURNAL_HEADER.unpack_from(raw, 0)
            if magic == JOURNAL_MAGIC:
                pos = JOURNAL_HEADER.size
                step = JOURNAL_RECORD.size + PAGE_SIZE
                while pos + step <= len(raw):
                    pgno, crc = JOURNAL_RECORD.unpack_from(raw, pos)
                    data = raw[pos + JOURNAL_RECORD.size:pos + step]
                    # a torn record means the journal itself wasn't finished,
                    # so the database file was never touched past this point
                    if zlib.crc32(data) != crc:
                        break
                    self.file.seek(pgno * PAGE_SIZE)
                    self.file.write(data)
                    pos += step
                self.file.truncate(original_count * PAGE_SIZE)
                self.file.flush()
                os.fsync(self.file.fileno())
        os.remove(self.journal_path)
        _fsync_dir(self.journal_path)

    def close(self):
        self.rollback()
        self.file.close()


def _fsync_dir(path):
    # make the creation / removal of the journal itself durable
    try:
        fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)

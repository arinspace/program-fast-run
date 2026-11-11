"""Read-only database browsing: SQLite files and CSV (loaded into a temp SQLite file).
No HTTP in here; server.py exposes it. Nothing here can modify the user's data:
  - SQLite is opened with mode=ro (fallback immutable=1) and PRAGMA query_only
  - an authorizer blocks ATTACH/DETACH and every PRAGMA except harmless introspection ones
  - one statement per query, a time limit, and a row limit
"""
import codecs
import csv
import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

SQLITE_EXT = {".db", ".sqlite", ".sqlite3", ".db3"}
FIND_EXT = SQLITE_EXT | {".csv"}
MAX_CELL, QUERY_TIMEOUT, MAX_QUERY_ROWS, MAX_TABLES = 2000, 8.0, 1000, 300
_SAFE_PRAGMAS = {"table_info", "table_xinfo", "table_list", "foreign_key_list", "index_list",
                 "index_info", "index_xinfo", "database_list", "page_count", "user_version"}
_SKIP_DIRS = {"venv", ".venv", "env", "node_modules", "site-packages", "__pycache__", ".git"}
_INT_RE = re.compile(r"^-?(0|[1-9]\d*)$")
_FLOAT_RE = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


def _q(name):
    return '"' + str(name).replace('"', '""') + '"'


def _authorizer(action, a1, a2, db, src):
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and (a1 or "").lower() not in _SAFE_PRAGMAS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _cell(v):
    if v is None:
        return None
    if isinstance(v, bytes):
        return f"<BLOB {len(v)} bytes>"
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return str(v)
    if isinstance(v, int) and abs(v) > 2**53:  # JavaScript would round it
        return str(v)
    if isinstance(v, str) and len(v) > MAX_CELL:
        return v[:MAX_CELL] + "…"
    return v


def _err(e):
    msg = str(e)
    if "interrupted" in msg:
        return ValueError(f"คำสั่งใช้เวลานานเกิน {QUERY_TIMEOUT:.0f} วินาที จึงถูกยกเลิก")
    if "readonly" in msg or "read-only" in msg:
        return ValueError("เปิดแบบอ่านอย่างเดียว ไม่อนุญาตให้แก้ไขข้อมูล")
    return ValueError(msg)


def _is_sqlite(p: Path):
    try:
        with open(p, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _detect_encoding(p: Path):
    with open(p, "rb") as f:
        sample = f.read(1 << 20)
    for enc in ("utf-8-sig", "cp874"):  # cp874 = Thai Windows (Excel "CSV" often saves this)
        try:
            codecs.getincrementaldecoder(enc)().decode(sample, final=False)
            return enc
        except UnicodeDecodeError:
            pass
    return "latin-1"


def _csv_to_sqlite(src: Path, dest: Path):
    enc = _detect_encoding(src)

    def reader():
        f = open(src, newline="", encoding=enc, errors="replace")
        sample = f.read(65536)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        return f, csv.reader(f, dialect)

    f, rd = reader()
    with f:
        header = next(rd, None)
    if not header:
        raise ValueError("ไฟล์ CSV ว่าง")
    cols, seen = [], set()
    for i, h in enumerate(header):
        name = h.strip() or f"col{i + 1}"
        base, n = name, 2
        while name.lower() in seen:
            name, n = f"{base}_{n}", n + 1
        seen.add(name.lower())
        cols.append(name)
    kinds = ["INTEGER"] * len(cols)
    f, rd = reader()
    with f:  # pass 1: infer column types
        next(rd, None)
        for row in rd:
            for i in range(len(cols)):
                v = row[i].strip() if i < len(row) else ""
                if not v or kinds[i] == "TEXT":
                    continue
                if kinds[i] == "INTEGER" and not _INT_RE.match(v):
                    kinds[i] = "REAL" if _FLOAT_RE.match(v) and not re.match(r"^-?0\d", v) else "TEXT"
                elif kinds[i] == "REAL" and not _FLOAT_RE.match(v):
                    kinds[i] = "TEXT"

    def conv(i, v):
        if kinds[i] == "TEXT":
            return v
        v = v.strip()
        return None if not v else (int(v) if kinds[i] == "INTEGER" else float(v))

    con = sqlite3.connect(dest)
    try:
        tname = re.sub(r"\W+", "_", src.stem) or "data"
        con.execute(f"CREATE TABLE {_q(tname)} ({', '.join(f'{_q(c)} {k}' for c, k in zip(cols, kinds))})")
        ins = f"INSERT INTO {_q(tname)} VALUES ({','.join('?' * len(cols))})"
        f, rd = reader()
        with f:  # pass 2: insert
            next(rd, None)
            batch = []
            for row in rd:
                row = (row + [""] * len(cols))[:len(cols)]
                batch.append([conv(i, v) for i, v in enumerate(row)])
                if len(batch) >= 5000:
                    con.executemany(ins, batch)
                    batch.clear()
            if batch:
                con.executemany(ins, batch)
        con.commit()
    finally:
        con.close()


def find_files(base: Path, depth=3, limit=50):
    """Database-looking files under base (for the 'found in your project' list)."""
    base, out = Path(base), []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        if Path(root).relative_to(base).parts.__len__() >= depth:
            dirs[:] = []
        for fn in sorted(files):
            if Path(fn).suffix.lower() in FIND_EXT:
                p = Path(root) / fn
                out.append({"path": str(p), "name": str(p.relative_to(base)), "size": p.stat().st_size})
                if len(out) >= limit:
                    return out
    return out


class DbView:
    def __init__(self):
        self.path = self.name = self.kind = None
        self.tmp = None

    # ---- open/close ----
    def open(self, path):
        p = Path(path)
        if not p.is_file():
            raise ValueError("ไม่พบไฟล์: " + str(p))
        self.close()
        try:
            if _is_sqlite(p):
                dbp, kind = p, "sqlite"
            elif p.suffix.lower() == ".csv":
                self.tmp = tempfile.mkdtemp(prefix="lsr_db_")
                dbp, kind = Path(self.tmp) / "csv.sqlite", "csv"
                _csv_to_sqlite(p, dbp)
            else:
                raise ValueError("ไม่รองรับไฟล์นี้ (รองรับไฟล์ SQLite เช่น .db .sqlite .sqlite3 และ .csv)")
            self.path, self.name, self.kind = dbp, p.name, kind
            return self.summary()
        except Exception:
            self.close()
            raise

    def close(self):
        self.path = self.name = self.kind = None
        tmp, self.tmp = self.tmp, None
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---- connections ----
    def _connect(self):
        if self.path is None:
            raise ValueError("ยังไม่ได้เปิดฐานข้อมูล")
        uri, last = Path(self.path).resolve().as_uri(), None
        for suffix in ("?mode=ro", "?immutable=1"):  # immutable: DB in WAL mode with no -shm file
            try:
                c = sqlite3.connect(uri + suffix, uri=True, timeout=2)
                c.execute("SELECT count(*) FROM sqlite_master").fetchone()
                break
            except sqlite3.Error as e:
                last = e
        else:
            raise _err(last)
        c.text_factory = lambda b: b.decode("utf-8", "replace")
        c.execute("PRAGMA query_only=ON")
        c.set_authorizer(_authorizer)
        end = time.monotonic() + QUERY_TIMEOUT
        c.set_progress_handler(lambda: 1 if time.monotonic() > end else 0, 10000)
        return c

    @contextmanager
    def _conn(self):
        c = self._connect()
        try:
            yield c
        except sqlite3.Error as e:
            raise _err(e)
        finally:
            c.close()

    @staticmethod
    def _names(c):
        return c.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
                         "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY type, name").fetchall()[:MAX_TABLES]

    # ---- API ----
    def summary(self):
        with self._conn() as c:
            tables = []
            for name, typ in self._names(c):
                try:
                    n = c.execute(f"SELECT COUNT(*) FROM {_q(name)}").fetchone()[0]
                except sqlite3.Error:
                    n = None
                tables.append({"name": name, "type": typ, "rows": n})
        return {"name": self.name, "kind": self.kind, "tables": tables}

    def table(self, name, page=0, size=50, sort=None, direction="asc", q=""):
        size, page = max(1, min(int(size), 500)), max(0, int(page))
        with self._conn() as c:
            if name not in {n for n, _ in self._names(c)}:
                raise ValueError("ไม่พบตาราง: " + str(name))
            info = c.execute(f"PRAGMA table_info({_q(name)})").fetchall()
            cols = [r[1] for r in info]
            where, params = "", []
            if q:
                esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                where = "WHERE " + " OR ".join(f"CAST({_q(col)} AS TEXT) LIKE ? ESCAPE '\\'" for col in cols[:60])
                params = [f"%{esc}%"] * len(cols[:60])
            order = f"ORDER BY {_q(sort)} {'DESC' if direction == 'desc' else 'ASC'}" if sort in cols else ""
            total = c.execute(f"SELECT COUNT(*) FROM {_q(name)} {where}", params).fetchone()[0]
            rows = c.execute(f"SELECT * FROM {_q(name)} {where} {order} LIMIT ? OFFSET ?",
                             params + [size, page * size]).fetchall()
        return {"columns": cols, "rows": [[_cell(v) for v in r] for r in rows], "total": total,
                "page": page, "size": size,
                "schema": [{"name": r[1], "type": r[2], "notnull": bool(r[3]), "pk": bool(r[5])} for r in info]}

    def query(self, sql):
        sql = (sql or "").strip()
        if not sql:
            raise ValueError("ยังไม่ได้พิมพ์ SQL")
        t0 = time.monotonic()
        with self._conn() as c:
            try:
                cur = c.execute(sql)
            except sqlite3.Warning as e:  # "You can only execute one statement at a time"
                raise ValueError(str(e))
            except sqlite3.DatabaseError as e:
                if "not authorized" in str(e):
                    raise ValueError("คำสั่งนี้ไม่อนุญาต (อ่านข้อมูลอย่างเดียว)")
                raise
            if cur.description is None:
                raise ValueError("คำสั่งนี้ไม่คืนผลลัพธ์ (อนุญาตเฉพาะ SELECT)")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(MAX_QUERY_ROWS + 1)
        return {"columns": cols, "rows": [[_cell(v) for v in r] for r in rows[:MAX_QUERY_ROWS]],
                "truncated": len(rows) > MAX_QUERY_ROWS, "ms": round((time.monotonic() - t0) * 1000)}
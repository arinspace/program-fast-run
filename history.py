"""Run history for grading student work: one row per run with the program's whole output,
an editable title, a score and a short note. Stored in a small SQLite file (stdlib only).
No HTTP in here; server.py exposes it."""
import csv
import io
import math
import os
import shutil
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

MAX_OUTPUT = 200_000            # characters of output kept per run
MAX_TITLE, MAX_NOTE = 200, 500
STATE_TH = {"ok": "สำเร็จ", "error": "ผิดพลาด", "stopped": "หยุดโดยผู้ใช้",
            "running": "กำลังรัน", "interrupted": "ถูกขัดจังหวะ"}

SCHEMA = """CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started REAL NOT NULL, finished REAL,
  title TEXT NOT NULL, target TEXT, source TEXT,
  state TEXT NOT NULL, exit_code INTEGER,
  output TEXT NOT NULL DEFAULT '', truncated INTEGER NOT NULL DEFAULT 0,
  score REAL, max_score REAL NOT NULL DEFAULT 10, note TEXT NOT NULL DEFAULT '')"""
LIST_COLS = ("id, started, finished, title, target, source, state, exit_code, truncated, "
             "score, max_score, note, length(output) AS out_len")


def _app_dir():
    """Folder of app.py, or of app.exe when packaged with PyInstaller."""
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def default_path():
    env = os.environ.get("LSR_HISTORY")  # tests / portable installs can redirect the file
    if env:
        return Path(env)
    new, old = _app_dir() / "history" / "history.db", Path.home() / ".local_runner" / "history.db"
    if not new.exists() and old.is_file():  # one-time move from the old location (the old file is kept)
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, new)
        except OSError:
            pass
    return new


def _id(v):
    if isinstance(v, bool):
        raise ValueError("id ไม่ถูกต้อง")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError("id ไม่ถูกต้อง")


def _num(v, what):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise ValueError(f"{what}ต้องเป็นตัวเลข")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what}ต้องเป็นตัวเลข")
    if not math.isfinite(f) or f < 0 or f > 1_000_000:
        raise ValueError(f"{what}ต้องเป็นตัวเลขตั้งแต่ 0 ถึง 1,000,000")
    return f


def _csv_text(v):
    """Titles/notes come from students' file names: stop Excel from running them as formulas."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _clock(ts):
    return "" if ts is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


class History:
    def __init__(self, path=None):
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as c:
            c.execute(SCHEMA)
            # the app was closed while something was running
            c.execute("UPDATE runs SET state='interrupted', finished=COALESCE(finished, started) WHERE state='running'")

    @contextmanager
    def _db(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---- recording a run ----
    def start(self, title, target, source):
        with self._db() as c:
            cur = c.execute("INSERT INTO runs (started, title, target, source, state) VALUES (?,?,?,?, 'running')",
                            (time.time(), (title or "ไม่มีชื่อ")[:MAX_TITLE], target, source))
            return cur.lastrowid

    def finish(self, rid, state, exit_code, output, truncated=False):
        with self._db() as c:
            c.execute("UPDATE runs SET finished=?, state=?, exit_code=?, output=?, truncated=? WHERE id=?",
                      (time.time(), state, exit_code, output, int(bool(truncated)), rid))

    # ---- reading ----
    def get(self, rid, output=True):
        cols = LIST_COLS + (", output" if output else "")
        with self._db() as c:
            r = c.execute(f"SELECT {cols} FROM runs WHERE id=?", (_id(rid),)).fetchone()
        if r is None:
            raise ValueError("ไม่พบรายการ")
        return dict(r)

    def search(self, q="", filter="all", order="desc", limit=100, offset=0):
        where, params = [], []
        if q:
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(title LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\' OR target LIKE ? ESCAPE '\\')")
            params += [f"%{esc}%"] * 3
        if filter == "graded":
            where.append("score IS NOT NULL")
        elif filter == "ungraded":
            where.append("score IS NULL")
        w = ("WHERE " + " AND ".join(where)) if where else ""
        limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
        with self._db() as c:
            total = c.execute(f"SELECT COUNT(*) FROM runs {w}", params).fetchone()[0]
            rows = c.execute(f"SELECT {LIST_COLS} FROM runs {w} ORDER BY id {'ASC' if order == 'asc' else 'DESC'} "
                             "LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
            n, graded, avg = c.execute("SELECT COUNT(*), COUNT(score), AVG(CASE WHEN score IS NOT NULL AND max_score > 0 "
                                       "THEN score * 100.0 / max_score END) FROM runs").fetchone()
        return {"items": [dict(r) for r in rows], "total": total,
                "summary": {"count": n, "graded": graded, "avg_pct": None if avg is None else round(avg, 1)}}

    # ---- grading ----
    def update(self, rid, fields):
        rid, sets, vals = _id(rid), [], []
        if "title" in fields:
            t = str(fields["title"]).strip()
            if not t:
                raise ValueError("ชื่อต้องไม่ว่าง")
            if len(t) > MAX_TITLE:
                raise ValueError(f"ชื่อยาวเกิน {MAX_TITLE} ตัวอักษร")
            sets.append("title=?"); vals.append(t)
        if "score" in fields:
            sets.append("score=?"); vals.append(_num(fields["score"], "คะแนน"))
        if "max_score" in fields:
            m = _num(fields["max_score"], "คะแนนเต็ม")
            if not m:
                raise ValueError("คะแนนเต็มต้องมากกว่า 0")
            sets.append("max_score=?"); vals.append(m)
        if "note" in fields:
            n = str(fields["note"] or "").strip()
            if len(n) > MAX_NOTE:
                raise ValueError(f"ข้อความยาวเกิน {MAX_NOTE} ตัวอักษร")
            sets.append("note=?"); vals.append(n)
        if sets:
            with self._db() as c:
                if c.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", vals + [rid]).rowcount == 0:
                    raise ValueError("ไม่พบรายการ")
        return self.get(rid, output=False)

    def delete(self, rid):
        with self._db() as c:
            if c.execute("DELETE FROM runs WHERE id=? AND state!='running'", (_id(rid),)).rowcount == 0:
                raise ValueError("ไม่พบรายการ (หรือกำลังรันอยู่)")

    def clear(self):
        with self._db() as c:
            return c.execute("DELETE FROM runs WHERE state!='running'").rowcount

    # ---- export for a spreadsheet ----
    def export_csv(self):
        with self._db() as c:
            rows = c.execute(f"SELECT {LIST_COLS} FROM runs ORDER BY id").fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ลำดับ", "เวลาเริ่ม", "ชื่อ", "ไฟล์/โฟลเดอร์", "สถานะ", "exit code", "ระยะเวลา (วินาที)",
                    "คะแนน", "คะแนนเต็ม", "เปอร์เซ็นต์", "ข้อความ"])
        for r in rows:
            dur = "" if r["finished"] is None else round(r["finished"] - r["started"], 1)
            pct = "" if r["score"] is None or not r["max_score"] else round(r["score"] * 100 / r["max_score"], 1)
            w.writerow([r["id"], _clock(r["started"]), _csv_text(r["title"]), _csv_text(r["target"]),
                        STATE_TH.get(r["state"], r["state"]), "" if r["exit_code"] is None else r["exit_code"], dur,
                        "" if r["score"] is None else r["score"], r["max_score"], pct, _csv_text(r["note"])])
        return buf.getvalue()
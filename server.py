"""Web layer: serves web/ and a small JSON API on 127.0.0.1. All run logic lives in runner.py.

Security model (the page can start programs, so other websites must not be able to talk to it):
  - listens on 127.0.0.1 only
  - Host header must be 127.0.0.1:<port> or localhost:<port>   (blocks DNS rebinding)
  - every API call needs the random per-launch token in X-Token (a custom header makes cross-site
    requests fail the CORS preflight, and other sites cannot read the token from the page)
"""
import json
import secrets
import socket
import shutil
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dbview import DbView, find_files
from history import MAX_OUTPUT, History
from runner import BASE, KINDS, Runner, find_urls

WEB = BASE / "web"
STATIC = {"/style.css": "text/css; charset=utf-8", "/app.js": "text/javascript; charset=utf-8"}
MAX_LOG = 5000      # lines kept in memory
POLL_CHUNK = 500    # lines returned per poll
IDLE_EXIT = 120     # seconds without any browser request (and nothing running) before exiting


def _open_when_up(url, timeout=15):
    """Open the browser for a web app the program started, once its port accepts connections."""
    port, end = int(url.rsplit(":", 1)[1].split("/")[0]), time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            webbrowser.open(url)
            return
        except OSError:
            time.sleep(0.2)


class Controller:
    """Holds UI-facing state: log buffer, status, chosen target. Thread-safe via self.lock."""

    def __init__(self):
        self.runner = Runner()
        self.lock = threading.Lock()
        self.log, self.base = [], 0          # log[0] has absolute index self.base
        self.state, self.text = "ready", "พร้อมใช้งาน"
        self.stopped = False
        self.target = None
        self.upload_root = None
        self.urls, self.auto_open = [], False   # web addresses the running program printed
        self.db, self._db_up = DbView(), None
        self.history, self.run_id, self.target_src = History(), None, None   # grading history
        self._out, self._out_len, self._out_trunc, self._out_cr, self._recording = [], 0, False, False, False
        self.last_seen = time.time()
        threading.Thread(target=self._drain, daemon=True).start()

    # ---- runner queue -> log/status ----
    def _hist(self, fn, *args):
        """History is a convenience: a failure there must never break running programs."""
        try:
            return fn(*args)
        except Exception as e:
            if not getattr(self, "_hist_warned", False):
                self._hist_warned = True
                self._add(f"[ประวัติ] บันทึกไม่สำเร็จ: {type(e).__name__}: {e}", "err")
            return None

    def _out_add(self, tag, text):
        """Keep the run's whole output for the history; progress rows collapse like on the page."""
        if tag in ("cr", "crend") and self._out_cr and self._out:
            if text != "":
                self._out[-1] = text
            self._out_cr = tag == "cr"
            return
        if self._out_len >= MAX_OUTPUT:
            self._out_trunc = True
            return
        self._out.append(text)
        self._out_len += len(text) + 1
        self._out_cr = tag == "cr"

    def _finish_run(self, state, code=None):
        if not self._recording:
            return
        self._recording = False
        text = "\n".join(self._out) + ("\n[... ตัดส่วนที่เกินออก ...]" if self._out_trunc else "")
        self._hist(self.history.finish, self.run_id, state, code, text, self._out_trunc)

    def _add(self, text, tag=None):
        if self._recording:
            self._out_add(tag, text)
        self.log.append((text, tag))
        if len(self.log) > MAX_LOG:
            drop = len(self.log) - MAX_LOG
            del self.log[:drop]
            self.base += drop

    def _drain(self):
        while True:
            kind, val = self.runner.q.get()
            with self.lock:
                if kind == "line":
                    self._add(val)
                    for u in find_urls(val):
                        if u not in self.urls:
                            self.urls.append(u)
                            if self.auto_open and not val.startswith("Serving "):  # runner opens its own HTML server
                                threading.Thread(target=_open_when_up, args=(u,), daemon=True).start()
                elif kind in ("cr", "crend"):  # progress-bar style output: the page updates one row
                    self._add(val, kind)
                elif kind == "error":
                    self._add(f"[error] {val}", "err")
                    self.state, self.text = "error", "เริ่มโปรแกรมไม่สำเร็จ"
                elif self.stopped:
                    self._add(f"[หยุดโดยผู้ใช้, exit code {val}]", "info")
                    self.state, self.text = "ready", "หยุดแล้ว"
                elif val == 0:
                    self._add("[เสร็จสิ้น, exit code 0]", "ok")
                    self.state, self.text = "ok", "เสร็จสิ้น"
                else:
                    self._add(f"[exit code {val}]", "err")
                    self.state, self.text = "error", f"จบด้วย exit code {val}"
                if kind in ("done", "error"):  # the run is over: store it in the history
                    self._finish_run({"ready": "stopped"}.get(self.state, self.state), val if kind == "done" else None)

    def poll(self, since):
        with self.lock:
            since = max(since, self.base)
            i = since - self.base
            chunk = self.log[i:i + POLL_CHUNK]
            nxt = since + len(chunk)
            return {"lines": chunk, "next": nxt, "more": nxt < self.base + len(self.log),
                    "state": self.state, "text": self.text, "urls": list(self.urls), "run_id": self.run_id}

    # ---- choosing what to run ----
    def set_target(self, data):
        if "upload" in data:
            root = self._root()
            p = (root / str(data["upload"])).resolve()
            if not p.is_relative_to(root) or p == root or not p.exists():
                raise ValueError("ไม่พบไฟล์ที่อัปโหลด")
            self.target, display, self.target_src = p, "อัปโหลดแล้ว", "upload"
        else:
            p = Path(str(data.get("path", "")).strip().strip('"'))
            if not p.exists():
                raise ValueError("ไม่พบ: " + str(p))
            self.target, display, self.target_src = p, str(p), "path"
        return {"name": self.target.name or str(self.target), "display": display}

    def _root(self):
        if self.upload_root is None:
            raise ValueError("ยังไม่ได้เริ่มอัปโหลด")
        return self.upload_root.resolve()

    def new_upload(self):
        if self.state == "running":
            raise RuntimeError("มีโปรแกรมกำลังรันอยู่")
        self._drop_upload()
        self.upload_root = Path(tempfile.mkdtemp(prefix="lsr_up_"))

    def save_upload(self, rel, rfile, length):
        root = self._root()
        dest = (root / rel).resolve()  # an absolute or ../ rel escapes root and is rejected below
        if not rel or not dest.is_relative_to(root) or dest == root:
            raise ValueError("path ไม่ถูกต้อง")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            while length > 0:
                chunk = rfile.read(min(65536, length))
                if not chunk:
                    break
                f.write(chunk)
                length -= len(chunk)

    def _drop_upload(self):
        if self.upload_root:
            shutil.rmtree(self.upload_root, ignore_errors=True)
            self.upload_root = None

    # ---- run control ----
    def run(self, auto_open=False):
        if not self.target:
            raise ValueError("เลือกไฟล์หรือโฟลเดอร์ก่อน")
        with self.lock:
            if self.state == "running":
                raise RuntimeError("มีโปรแกรมกำลังรันอยู่")
            # set BEFORE starting so a fast "error"/"done" message can't be overwritten afterwards
            prev = (self.state, self.text)
            self.stopped, self.state = False, "running"
            self.urls, self.auto_open = [], bool(auto_open)
            prev_run, title = self.run_id, self.target.name or str(self.target)
            self.run_id = self._hist(self.history.start, title,
                                     title if self.target_src == "upload" else str(self.target), self.target_src or "path")
            self._out, self._out_len, self._out_trunc, self._out_cr = [], 0, False, False
            self._recording = self.run_id is not None
            self.text = "กำลังรัน: " + (self.target.name or str(self.target))
        try:
            self.runner.start_async(self.target, interactive=True)
        except RuntimeError:
            with self.lock:
                self.state, self.text = prev
                if self._recording:  # nothing started, so there is nothing to remember
                    self._recording = False
                    self._hist(self.history.delete, self.run_id)
                    self.run_id = prev_run
            raise

    def send_input(self, data):
        eof = bool(data.get("eof"))
        text = str(data.get("text", ""))
        with self.lock:
            if self.state != "running":
                raise RuntimeError("ไม่มีโปรแกรมที่กำลังรันอยู่")
            self._add("[ส่ง EOF]" if eof else "> " + text, "info" if eof else "input")  # echo: pipes don't echo
        if eof:
            self.runner.close_input()
        else:
            self.runner.send_input(text)

    def stop(self):
        with self.lock:
            if self.state != "running":  # nothing to stop: don't mislabel the next result as a user stop
                return
            self.stopped = True
        self.runner.stop()

    # ---- database viewer ----
    def db_open(self, data):
        return self.db.open(Path(str(data.get("path", "")).strip().strip('"')))

    def db_upload(self, name, rfile, length):
        name = Path(name or "").name
        if name in ("", ".", ".."):
            raise ValueError("ชื่อไฟล์ไม่ถูกต้อง")
        self._drop_db_upload()
        self._db_up = Path(tempfile.mkdtemp(prefix="lsr_dbup_"))
        dest = self._db_up / name
        with open(dest, "wb") as f:
            while length > 0:
                chunk = rfile.read(min(65536, length))
                if not chunk:
                    break
                f.write(chunk)
                length -= len(chunk)
        return self.db.open(dest)

    def db_find(self):
        t = self.target
        if not t or not t.exists() or t.suffix.lower() == ".zip":
            return []
        return find_files(t, depth=3) if t.is_dir() else find_files(t.parent, depth=1)

    def _drop_db_upload(self):
        if self._db_up:
            if self.db.path and Path(self.db.path).is_relative_to(self._db_up):
                self.db.close()
            shutil.rmtree(self._db_up, ignore_errors=True)
            self._db_up = None

    def shutdown(self):
        self.runner.shutdown()
        with self.lock:
            self._finish_run("interrupted")
        self._drop_upload()
        self.db.close()
        self._drop_db_upload()


class Handler(BaseHTTPRequestHandler):
    ctl = None
    token = ""

    def log_message(self, *args):  # silence per-request logging (also safe with --noconsole)
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _host_ok(self):
        port = self.server.server_port
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _guard(self):
        """Host + token check for API calls; also counts as a 'browser is still open' heartbeat."""
        if not self._host_ok() or not secrets.compare_digest(self.headers.get("X-Token", ""), self.token):
            self._json({"error": "forbidden"}, 403)
            return False
        self.ctl.last_seen = time.time()
        return True

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        if u.path == "/":
            if not secrets.compare_digest(parse_qs(u.query).get("t", [""])[0], self.token):
                return self._send(403, "Forbidden: open the link printed/opened by app.py".encode(), "text/plain; charset=utf-8")
            html = (WEB / "index.html").read_text("utf-8")
            html = (html.replace("__TOKEN__", self.token).replace("__KINDS__", " ".join(KINDS))
                        .replace("__ACCEPT__", ",".join([".zip", *KINDS])))
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if u.path in STATIC:
            return self._send(200, (WEB / u.path[1:]).read_bytes(), STATIC[u.path])
        if u.path.startswith("/api/db/"):
            if not self._guard():
                return
            try:
                q = {k: x[0] for k, x in parse_qs(u.query).items()}
                if u.path == "/api/db/table":
                    return self._json(self.ctl.db.table(q.get("name", ""), int(q.get("page", 0)), int(q.get("size", 50)),
                                                        q.get("sort"), q.get("dir", "asc"), q.get("q", "")))
                if u.path == "/api/db/find":
                    return self._json({"files": self.ctl.db_find()})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        if u.path == "/api/history" or u.path.startswith("/api/history/"):
            if not self._guard():
                return
            try:
                q, h = {k: x[0] for k, x in parse_qs(u.query).items()}, self.ctl.history
                if u.path == "/api/history":
                    return self._json(h.search(q.get("q", ""), q.get("filter", "all"), q.get("order", "desc"),
                                               int(q.get("limit", 100)), int(q.get("offset", 0))))
                if u.path == "/api/history/get":
                    return self._json(h.get(q.get("id")))
                if u.path == "/api/history/export":
                    return self._send(200, h.export_csv().encode("utf-8-sig"), "text/csv; charset=utf-8")
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        if u.path == "/api/poll":
            if not self._guard():
                return
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            return self._json(self.ctl.poll(since))
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._guard():
            return
        u, ctl = urlparse(self.path), self.ctl
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if u.path == "/api/upload":
                ctl.save_upload(parse_qs(u.query).get("rel", [""])[0], self.rfile, n)
                return self._json({"ok": True})
            if u.path == "/api/db/upload":
                return self._json(ctl.db_upload(parse_qs(u.query).get("name", [""])[0], self.rfile, n))
            data = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/api/new_upload":
                ctl.new_upload()
            elif u.path == "/api/target":
                return self._json(ctl.set_target(data))
            elif u.path == "/api/db/open":
                return self._json(ctl.db_open(data))
            elif u.path == "/api/db/query":
                return self._json(ctl.db.query(data.get("sql")))
            elif u.path == "/api/history/update":
                return self._json(ctl.history.update(data.get("id"), data))
            elif u.path == "/api/history/delete":
                ctl.history.delete(data.get("id"))
            elif u.path == "/api/history/clear":
                return self._json({"deleted": ctl.history.clear()})
            elif u.path == "/api/run":
                ctl.run(data.get("auto_open"))
            elif u.path == "/api/stdin":
                ctl.send_input(data)
            elif u.path == "/api/stop":
                ctl.stop()
            elif u.path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            else:
                return self._json({"error": "not found"}, 404)
            self._json({"ok": True})
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except RuntimeError as e:
            self._json({"error": str(e)}, 409)
        except Exception as e:  # keep the server alive, tell the UI
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def make_server():
    """Returns (httpd, controller, token). Not started yet."""
    ctl, token = Controller(), secrets.token_urlsafe(16)
    handler = type("BoundHandler", (Handler,), {"ctl": ctl, "token": token})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    return httpd, ctl, token


def _watchdog(ctl, httpd):
    """Exit after the browser tab is gone for IDLE_EXIT seconds, unless a program is still running."""
    while True:
        time.sleep(5)
        if time.time() - ctl.last_seen > IDLE_EXIT and ctl.state != "running":
            httpd.shutdown()
            return


def main():
    httpd, ctl, token = make_server()
    url = f"http://127.0.0.1:{httpd.server_port}/?t={token}"
    print("Open:", url)  # harmless when frozen with --noconsole (stdout is None)
    threading.Thread(target=_watchdog, args=(ctl, httpd), daemon=True).start()
    webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctl.shutdown()
        httpd.server_close()
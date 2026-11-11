"""Logic layer: (zip | file | folder) -> find entry point -> run with portable runtime -> stream output -> cleanup.

Messages arrive on Runner.q as ("line", text), ("error", text) and finally ("done", exit_code).
NOTE: this is NOT a security sandbox. Child processes run with the current user's permissions.
"""
import ast
import codecs
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))  # _MEIPASS = PyInstaller bundle dir


def _runtime(name, exe, dev_fallback):
    """Portable runtime if bundled; when run from source (not frozen) fall back to the system one."""
    p = BASE / "runtime" / name / exe
    return p if p.is_file() or getattr(sys, "frozen", False) else Path(dev_fallback or p)


KINDS = {".py": "python", ".js": "node", ".bat": "bat", ".cmd": "bat",
         ".exe": "exe", ".html": "html", ".htm": "html",
         ".ps1": "ps1", ".sh": "sh", ".jar": "jar", ".rb": "ruby", ".php": "php",
         ".pl": "perl", ".go": "go", ".lua": "lua"}
# kinds that use an interpreter installed on the machine: kind -> (candidate executables, leading args)
SYSTEM_CMDS = {"ps1": (["powershell", "pwsh"], ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]),
               "sh": (["bash", "sh"], []), "jar": (["java"], ["-jar"]), "ruby": (["ruby"], []),
               "perl": (["perl"], []), "go": (["go"], ["run"]), "lua": (["lua"], [])}
ENTRY_NAMES = ["main.py", "app.py", "run.py", "__main__.py", "index.py", "index.js", "app.js",
               "server.js", "run.bat", "start.bat", "run.sh", "main.ps1", "index.php", "main.php", "index.html"]  # priority order

# Env vars whose names look like secrets are not handed to the child process.
_SECRET_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.I)

# Embeddable Python (has python3xx._pth) does not put the script's folder on sys.path,
# so `import sibling_module` fails. This launcher adds it, then runs the script as __main__.
_PY_LAUNCHER = ("import sys,runpy;d,f=sys.argv[1:3];sys.path[0:0]=[d]+sys.argv[3:];"
                "sys.argv=[f];runpy.run_path(f,run_name='__main__')")

DEPS_CACHE = Path.home() / ".local_runner" / "deps"   # pip --target folders, reused between runs
# import name -> pip package name, only where they differ
PIP_NAMES = {"cv2": "opencv-python", "PIL": "Pillow", "yaml": "PyYAML", "sklearn": "scikit-learn",
             "bs4": "beautifulsoup4", "dotenv": "python-dotenv", "dateutil": "python-dateutil",
             "serial": "pyserial", "Crypto": "pycryptodome", "jwt": "PyJWT", "docx": "python-docx",
             "pptx": "python-pptx", "fitz": "PyMuPDF", "skimage": "scikit-image", "attr": "attrs",
             "win32api": "pywin32", "win32com": "pywin32", "magic": "python-magic", "git": "GitPython",
             "OpenSSL": "pyOpenSSL", "telegram": "python-telegram-bot", "discord": "discord.py",
             "psycopg2": "psycopg2-binary", "MySQLdb": "mysqlclient", "usb": "pyusb", "wx": "wxPython",
             "Xlib": "python-xlib", "zmq": "pyzmq", "lxml": "lxml", "ruamel": "ruamel.yaml"}
_SKIP_DIRS = {"venv", ".venv", "env", "node_modules", "site-packages", "__pycache__", ".git"}


def _pip_flags():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _run_quiet(cmd, **kw):
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONUTF8="1")
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env, stdin=subprocess.DEVNULL, **_pip_flags(), **kw)


def _tail(r, n=8):
    return "\n".join((r.stdout + r.stderr).strip().splitlines()[-n:])


def _enable_site(exe):
    """Embeddable Python ships with `#import site` commented out, which hides site-packages and pip."""
    for pth in Path(exe).parent.glob("python*._pth"):
        txt = pth.read_text("utf-8")
        if re.search(r"^#\s*import site", txt, re.M):
            pth.write_text(re.sub(r"^#\s*import site", "import site", txt, flags=re.M), "utf-8")


def ensure_pip(exe):
    """Make sure `exe -m pip` works; bootstrap it (ensurepip, or get-pip.py next to the runtime)."""
    exe = str(exe)
    ok = lambda: _run_quiet([exe, "-m", "pip", "--version"]).returncode == 0
    if ok():
        return
    if _is_embeddable(exe):
        _enable_site(exe)
        if ok():
            return
    getpip = Path(exe).parent / "get-pip.py"
    if getpip.is_file():
        _run_quiet([exe, str(getpip), "--no-warn-script-location"], timeout=300)
    else:
        _run_quiet([exe, "-m", "ensurepip", "--upgrade"], timeout=300)
    if not ok():
        raise RuntimeError("Python runtime ไม่มี pip: วาง get-pip.py ไว้ข้าง python.exe "
                           "(https://bootstrap.pypa.io/get-pip.py) หรือใช้ Python แบบเต็มแทน Embeddable")


def _pyproject_deps(proj: Path):
    p = proj / "pyproject.toml"
    if not p.is_file():
        return []
    try:
        import tomllib  # Python 3.11+
        return list(tomllib.loads(p.read_text("utf-8")).get("project", {}).get("dependencies", []))
    except Exception:
        return []


def _third_party_imports(folder: Path):
    """Top-level module names imported by the project's .py files that are neither stdlib nor local."""
    names, local = set(), set()
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        local.update(dirs)
        for f in files:
            if not f.endswith(".py"):
                continue
            local.add(f[:-3])
            try:
                tree = ast.parse((Path(root) / f).read_text("utf-8", errors="replace"))
            except (SyntaxError, ValueError, OSError):
                continue
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    names.update(a.name.split(".")[0] for a in n.names)
                elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                    names.add(n.module.split(".")[0])
    return sorted(names - local - set(getattr(sys, "stdlib_module_names", ())))


def _missing_in_runtime(exe, deps_dir, names):
    """Which import names the runtime cannot find (looking in deps_dir as well)."""
    if not names:
        return []
    code = ("import sys,json,importlib.util as u;sys.path.insert(0,sys.argv[1]);"
            "print(json.dumps([n for n in sys.argv[2:] if u.find_spec(n) is None]))")
    r = _run_quiet([str(exe), "-c", code, str(deps_dir), *names])
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return list(names)


def _is_embeddable(exe):
    return any(Path(exe).parent.glob("python*._pth"))


def _is_full_python(exe):
    """Full CPython (has pip bootstrap, venv, tkinter-capable) rather than the embeddable zip build."""
    try:
        r = subprocess.run([str(exe), "-c", "import ensurepip, venv"], capture_output=True, timeout=20,
                           stdin=subprocess.DEVNULL,
                           **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _python_runtime():
    """Best Python: bundled full > the Python running this app (from source) > a full Python on PATH
    > bundled Embeddable (last resort: no tkinter/venv, pip must be bootstrapped)."""
    bundled = BASE / "runtime" / "python" / "python.exe"
    if bundled.is_file() and not _is_embeddable(bundled):
        return bundled
    if not getattr(sys, "frozen", False):
        return Path(sys.executable)
    for name in ("python", "python3"):
        c = shutil.which(name)
        if c and "WindowsApps" not in c and _is_full_python(c):  # WindowsApps = Microsoft Store stub
            return Path(c)
    return bundled


RUNTIMES = {"python": _python_runtime(),
            "node": _runtime("node", "node.exe", shutil.which("node"))}


# PHP: a script that looks like a web page/app runs under the built-in web server (php -S) and the browser is
# opened on it; a plain script runs as a CLI program with its output in the console.
_PHP_WEB_RE = re.compile(r"\$_(GET|POST|REQUEST|SERVER|SESSION|COOKIE|FILES)\b|<(!doctype|html)\b|\bheader\s*\(", re.I)
_PHP_EXTS = ("mbstring", "openssl", "curl", "fileinfo", "gd", "pdo_sqlite", "sqlite3", "pdo_mysql", "mysqli")


def _find_php():
    """Bundled runtime/php first, then php on PATH."""
    for p in (BASE / "runtime" / "php" / "php.exe", BASE / "runtime" / "php" / "php"):
        if p.is_file():
            return p
    w = shutil.which("php")
    return Path(w) if w else None


def _php_is_web(entry: Path):
    if entry.name.lower() == "index.php":
        return True
    try:
        return bool(_PHP_WEB_RE.search(entry.read_text(encoding="utf-8", errors="ignore")[:200_000]))
    except OSError:
        return False


def _php_flags(exe: Path):
    """php.exe from the zip on php.net has no php.ini, so every extension is off (PDO/SQLite/MySQL included).
    Switch on the common ones that sit in its ext folder."""
    ext = exe.parent / "ext"
    if (exe.parent / "php.ini").is_file() or not ext.is_dir():
        return []
    flags = ["-d", "extension_dir=" + str(ext)]
    for e in _PHP_EXTS:
        if (ext / f"php_{e}.dll").is_file():
            flags += ["-d", "extension=" + e]
    return flags


def _child_env():
    env = {k: v for k, v in os.environ.items() if not _SECRET_RE.search(k)}
    env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return env


def _visible(d: Path):
    return [p for p in d.iterdir() if not p.name.startswith((".", "__MACOSX"))]


def find_entry(folder: Path):
    """Unwrap single wrapper folders, then: standard names first, else the only runnable file."""
    d = folder
    while True:
        kids = _visible(d)
        if len(kids) == 1 and kids[0].is_dir():
            d = kids[0]
        else:
            break
    for name in ENTRY_NAMES:
        if (d / name).is_file():
            return d / name, KINDS[Path(name).suffix]
    runnable = [p for p in kids if p.is_file() and p.suffix.lower() in KINDS]
    if len(runnable) == 1:
        return runnable[0], KINDS[runnable[0].suffix.lower()]
    found = ", ".join(sorted(p.name for p in kids)[:10]) or "(ว่าง)"
    raise FileNotFoundError(
        f"ไม่พบ Entry Point ({', '.join(ENTRY_NAMES)}) หรือไฟล์ที่รันได้เพียงไฟล์เดียว. พบ: {found}")


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_URL_RE = re.compile(r"(https?)://(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1?\]):(\d{2,5})((?:/[^\s'\"<>)\]]*)?)", re.I)
_PORT_RE = re.compile(r"\b(?:listening|running|started|serving)\b[^\n]{0,40}?\bport\b\D{0,5}(\d{2,5})", re.I)


def find_urls(text):
    """Local web-server addresses mentioned in a line of program output (Flask, Django, Vite, Express...)."""
    text, out = _ANSI_RE.sub("", text), []
    for scheme, host, port, path in _URL_RE.findall(text):
        if 0 < int(port) <= 65535:
            host = "127.0.0.1" if host in ("0.0.0.0", "[::]") else host
            out.append(f"{scheme.lower()}://{host}:{port}{path.rstrip('.,;:')}")
    if not out:
        m = _PORT_RE.search(text)
        if m and 0 < int(m.group(1)) <= 65535:
            out.append(f"http://127.0.0.1:{m.group(1)}")
    return list(dict.fromkeys(out))


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Runner:
    def __init__(self, runtimes=RUNTIMES):
        self.runtimes = runtimes
        self.q = queue.Queue()
        self.proc = None
        self.tmp = None
        self._starting = False

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start_async(self, path, interactive=False):
        """Like start(), but extraction/launch happen off the UI thread; failures arrive as ("error", msg)."""
        if self.running or self._starting:
            raise RuntimeError("มีโปรแกรมกำลังรันอยู่")
        self._starting = True

        def work():
            try:
                self.start(path, interactive)
            except Exception as e:
                self.q.put(("error", str(e)))
            finally:
                self._starting = False

        threading.Thread(target=work, daemon=True).start()

    def start(self, path, interactive=False):
        """interactive=True keeps stdin open for send_input(); otherwise input() gets EOF immediately."""
        if self.running:
            raise RuntimeError("มีโปรแกรมกำลังรันอยู่")
        path = Path(path)
        try:
            if path.suffix.lower() == ".zip":
                self.tmp = tempfile.mkdtemp(prefix="lsr_")
                with zipfile.ZipFile(path) as z:
                    z.extractall(self.tmp)
                entry, kind = find_entry(Path(self.tmp))
            elif path.is_dir():
                entry, kind = find_entry(path)
            elif path.is_file() and path.suffix.lower() in KINDS:
                entry, kind = path, KINDS[path.suffix.lower()]
            else:
                raise ValueError("ไม่รองรับ: " + str(path) + " (รองรับ .zip, โฟลเดอร์, " + ", ".join(KINDS) + ")")
            deps = self._prepare_deps(entry, kind)
            cmd, url = self._command(entry, kind, deps)
            # own process group on POSIX so stop() can kill children too
            extra = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
                     else {"start_new_session": True})
            self.proc = subprocess.Popen(
                cmd, cwd=entry.parent,
                stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, env=_child_env(), **extra,  # raw pipe: read() returns as soon as bytes arrive
            )
        except Exception:
            self._cleanup()
            raise
        if url:
            self.q.put(("line", "Serving " + url))
            threading.Thread(target=self._open_when_ready, args=(url, self.proc), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _say(self, text):
        self.q.put(("line", "[setup] " + text))

    def _prepare_deps(self, entry, kind):
        """Install what the program needs. Returns a deps folder for Python, else None.
        Install failures are reported in the log but do not stop the run (the program shows its own error)."""
        try:
            if kind == "python":
                return self._python_deps(entry)
            if kind == "node":
                self._node_deps(entry.parent)
        except Exception as e:
            self._say(f"ติดตั้ง library ไม่สำเร็จ: {e}")
        return None

    def _python_deps(self, entry):
        exe = Path(self.runtimes["python"])
        if not exe.is_file():
            return None  # _command reports the missing runtime
        proj = entry.parent
        req = next((p / "requirements.txt" for p in (proj, proj.parent)
                    if (p / "requirements.txt").is_file()), None)
        if req:
            text = req.read_text("utf-8", errors="replace")
            specs = [l.split(" #")[0].strip() for l in text.splitlines()
                     if l.strip() and not l.strip().startswith(("#", "-"))]
            args, label = ["-r", str(req)], "requirements.txt"
        elif _pyproject_deps(proj):
            specs = _pyproject_deps(proj)
            args, label = specs, "pyproject.toml"
        else:
            names = _missing_in_runtime(exe, DEPS_CACHE / "_probe", _third_party_imports(proj))
            if not names:
                return None
            specs = sorted({PIP_NAMES.get(n, n) for n in names})
            args, label = specs, ", ".join(specs)
        dest = DEPS_CACHE / hashlib.sha1((str(exe) + "\n" + "\n".join(specs)).encode()).hexdigest()[:16]
        if not (dest / ".ok").is_file():
            self._say("กำลังติดตั้ง: " + label)
            ensure_pip(exe)
            base = [str(exe), "-m", "pip", "install", "--target", str(dest), "--no-warn-script-location"]
            r = _run_quiet(base + args, timeout=1800)
            failed = []
            if r.returncode != 0:  # one bad package must not block the rest: retry one by one
                for sp in specs:
                    if _run_quiet(base + [sp], timeout=1800).returncode != 0:
                        failed.append(sp)
                if failed:
                    self._say("ติดตั้งไม่ได้: " + ", ".join(failed) + "\n" + _tail(r))
            if not failed:
                dest.mkdir(parents=True, exist_ok=True)
                (dest / ".ok").write_text("ok")
                self._say("ติดตั้งเสร็จ")
        return dest if dest.is_dir() else None

    def _node_deps(self, d):
        pj = d / "package.json"
        if not pj.is_file() or (d / "node_modules").exists():
            return
        if not json.loads(pj.read_text("utf-8")).get("dependencies"):
            return
        npm = shutil.which("npm")
        if not npm:
            self._say("โปรเจกต์นี้ต้องใช้ npm install แต่ไม่พบ npm (node.exe แบบ standalone ไม่มี npm) "
                      "ให้รัน npm install ในโฟลเดอร์ก่อน แล้วใช้วิธี 'วาง path'")
            return
        self._say("กำลังรัน npm install")
        r = _run_quiet([npm, "install", "--no-audit", "--no-fund"], cwd=d, timeout=900)
        self._say("npm install เสร็จ" if r.returncode == 0 else "npm install ล้มเหลว:\n" + _tail(r))

    def _command(self, entry, kind, deps=None):
        """Returns (argv, url_to_open_or_None)."""
        if kind == "bat":
            return ["cmd", "/c", entry.name], None
        if kind == "exe":
            return [str(entry)], None
        if kind == "php":
            exe = self.runtimes.get("php") or _find_php()
            if not exe or not Path(exe).is_file():
                raise FileNotFoundError("ไม่พบ PHP: วาง php.exe ไว้ที่ runtime/php/php.exe หรือติดตั้ง PHP แล้วเพิ่มใน PATH")
            exe = Path(exe)
            flags = _php_flags(exe)
            if _php_is_web(entry):
                port = _free_port()
                page = "" if entry.name.lower() == "index.php" else entry.name
                return ([str(exe), *flags, "-S", f"127.0.0.1:{port}", "-t", str(entry.parent)],
                        f"http://127.0.0.1:{port}/{page}")
            return [str(exe), *flags, entry.name], None
        if kind in SYSTEM_CMDS:
            names, lead = SYSTEM_CMDS[kind]
            found = next((w for w in map(shutil.which, names) if w), None)
            if not found:
                raise FileNotFoundError(f"ไม่พบ {names[0]} ในเครื่อง ต้องติดตั้งก่อนจึงจะรันไฟล์ {entry.suffix} ได้")
            return [found, *lead, entry.name], None
        exe = Path(self.runtimes["python" if kind == "html" else kind])
        if not exe.is_file():
            raise FileNotFoundError(f"ไม่พบ Portable Runtime: {exe}")
        if kind == "html":
            port = _free_port()
            return ([str(exe), "-u", "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                    f"http://127.0.0.1:{port}/{entry.name}")
        if kind == "python":
            if deps or _is_embeddable(exe):
                return [str(exe), "-u", "-c", _PY_LAUNCHER, str(entry.parent), entry.name,
                        *([str(deps)] if deps else [])], None
            return [str(exe), "-u", entry.name], None
        return [str(exe), entry.name], None

    def _open_when_ready(self, url, proc, timeout=10):
        """Open the browser only once the local server accepts connections."""
        port, end = urlparse(url).port, time.time() + timeout
        while proc.poll() is None and time.time() < end:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        if proc.poll() is None:
            webbrowser.open(url)

    def _pump(self, proc):
        """Forward output as lines. Text without a trailing newline (an input() prompt) is
        flushed after a short pause so the user can see it before answering."""
        chunks = queue.Queue()

        def reader():
            try:
                while True:
                    b = proc.stdout.read(4096)
                    if not b:
                        break
                    chunks.put(b)
            finally:
                chunks.put(None)

        threading.Thread(target=reader, daemon=True).start()
        dec, buf = codecs.getincrementaldecoder("utf-8")(errors="replace"), ""
        while True:
            try:
                b = chunks.get(timeout=0.15)
            except queue.Empty:
                if buf.rstrip("\r"):
                    self.q.put(("line", buf.rstrip("\r")))
                buf = ""
                continue
            if b is None:
                break
            buf += dec.decode(b)
            hold = "\r" if buf.endswith("\r") else ""  # a "\r" may be the first half of "\r\n"
            buf = buf[:-1] if hold else buf
            *lines, buf = buf.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            for line in lines:
                self.q.put(("line", line))
            buf += hold
        buf += dec.decode(b"", final=True)
        if buf.rstrip("\r"):
            self.q.put(("line", buf.rstrip("\r")))
        code = proc.wait()
        self.close_input()
        self._cleanup()
        self.q.put(("done", code))

    def send_input(self, text):
        """Type a line into the running program (needs start(..., interactive=True))."""
        p = self.proc
        if not self.running or p.stdin is None:
            raise RuntimeError("ไม่มีโปรแกรมที่รับ input อยู่")
        try:
            p.stdin.write((text + "\n").encode("utf-8"))
            p.stdin.flush()
        except (OSError, ValueError):
            raise RuntimeError("โปรแกรมไม่รับ input แล้ว")

    def close_input(self):
        """Send EOF (like Ctrl+D / Ctrl+Z)."""
        p = self.proc
        if p is not None and p.stdin is not None:
            try:
                p.stdin.close()
            except OSError:
                pass

    def stop(self):
        if not self.running:
            return
        if os.name == "nt":  # kill the whole tree (cmd /c, child processes)
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)  # whole process group
            except ProcessLookupError:
                pass

    def shutdown(self):
        """For app exit: stop the child, wait briefly, and make sure the temp folder is gone."""
        self.stop()
        if self.proc is not None:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self._cleanup()

    def _cleanup(self):
        tmp, self.tmp = self.tmp, None
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
"""Everything that differs between Linux/macOS and Windows lives here: the shell, process
groups and tree kills, liveness checks, file locks, and the python interpreter.

Callers never test os.name themselves; they ask this module.
"""
import contextlib
import functools
import os
import shutil
import signal
import subprocess
import sys
import time

IS_WIN = os.name == "nt"

# Windows creation flags (defined here so the module imports on POSIX too)
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


# ---------------- shell ----------------
def _win_bash():
    """Git Bash, never System32\\bash.exe (that is the WSL launcher: a different machine)."""
    cands = []
    git = shutil.which("git")
    if git:
        root = os.path.dirname(os.path.dirname(os.path.realpath(git)))   # <Git>\cmd\git.exe -> <Git>
        cands += [os.path.join(root, "bin", "bash.exe"), os.path.join(root, "usr", "bin", "bash.exe")]
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        if os.environ.get(var):
            cands.append(os.path.join(os.environ[var], "Git", "bin", "bash.exe"))
    if os.environ.get("LOCALAPPDATA"):
        cands.append(os.path.join(os.environ["LOCALAPPDATA"], "Programs", "Git", "bin", "bash.exe"))
    found = shutil.which("bash")
    if found and not any(s in found.lower() for s in ("\\system32\\", "\\windowsapps\\")):
        cands.append(found)
    return next((c for c in cands if os.path.isfile(c)), None)


@functools.lru_cache(maxsize=None)
def shell_path():
    """The shell run_bash uses. FINCH_SHELL overrides. Windows: Git Bash > PowerShell > cmd."""
    forced = os.environ.get("FINCH_SHELL")
    if forced:
        return shutil.which(forced) or forced
    if not IS_WIN:
        if os.path.exists("/bin/bash"):
            return "/bin/bash"
        return shutil.which("bash") or "/bin/sh"
    return (_win_bash() or shutil.which("pwsh") or shutil.which("powershell")
            or os.environ.get("COMSPEC") or "cmd.exe")


def shell_name(path=None):
    """'bash', 'sh', 'zsh', 'pwsh', 'powershell' or 'cmd'."""
    base = (path or shell_path()).replace("\\", "/").rsplit("/", 1)[-1]
    return os.path.splitext(base)[0].lower()


def shell_command(cmd, path=None):
    """(args, use_shell) for subprocess. cmd.exe needs shell=True: list2cmdline quoting breaks `cmd /c`."""
    path = path or shell_path()
    name = shell_name(path)
    # both default to the OEM code page on a pipe (✓ arrives as √); run_bash children have
    # their own hidden console (hidden_group), so switching it to UTF-8 touches nothing else
    if name in ("pwsh", "powershell"):
        utf8 = "[Console]::OutputEncoding = $OutputEncoding = [Text.UTF8Encoding]::new($false); "
        return [path, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", utf8 + cmd], False
    if name == "cmd":
        return "chcp 65001 >nul & " + cmd, True
    return [path, "-c", cmd], False


def shell_hint():
    """One line for the system prompt when run_bash is not a POSIX shell, else ''."""
    name = shell_name()
    if name in ("pwsh", "powershell"):
        return ("run_bash runs PowerShell, not bash: use PowerShell syntax (Get-ChildItem, Get-Content, "
                "Remove-Item, `;` between commands).")
    if name == "cmd":
        return "run_bash runs Windows cmd.exe, not bash: use cmd syntax (dir, type, del, `&&` between commands)."
    return ""


def split_cmd(cmd):
    """argv for a user-configured command ($EDITOR, $PAGER). A whole string that names an
    executable wins, so unquoted `C:\\Program Files\\...\\code.exe` works on Windows."""
    import shlex
    cmd = (cmd or "").strip()
    if not cmd:
        return []
    if os.path.isfile(cmd) or (" " in cmd and shutil.which(cmd)):
        return [cmd]
    if IS_WIN:
        return [a.strip('"') for a in shlex.split(cmd, posix=False)]
    return shlex.split(cmd)


EDITORS = ("notepad",) if IS_WIN else ("nano", "vim", "vi")


# ---------------- python ----------------
PY = "python" if IS_WIN else "python3"      # the name to suggest to the model in hints


def find_python():
    """The interpreter run_python and the test guard use. On Windows, skip the Microsoft Store
    stubs in WindowsApps: they open the Store instead of running anything."""
    names = ("python", "py", "python3") if IS_WIN else ("python3", "python")
    for n in names:
        p = shutil.which(n)
        if p and "\\windowsapps\\" not in p.lower():
            return p
    return sys.executable or PY


# ---------------- processes ----------------
def new_group():
    """Popen kwargs: the child leads its own process group, so the whole tree can be killed
    and the REPL's ctrl+c doesn't reach it."""
    if IS_WIN:
        return {"creationflags": CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def detached():
    """Popen kwargs for background runners (daemon, job supervisor): they survive the window
    that started them closing."""
    if IS_WIN:
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def hidden_group():
    """Popen kwargs: own group, and on Windows its own hidden console, so nothing pops up and
    nothing the child does to its console (code page, ctrl+c) reaches Finch's."""
    if IS_WIN:
        return {"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}
    return {"start_new_session": True}


def kill_tree(pid, force=True):
    """Kill a process and everything it started. The pid must lead its own group (see new_group).
    On Windows there is no graceful console signal across groups, so it is always forced."""
    if not pid:
        return
    if IS_WIN:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)


def kill_proc_tree(proc):
    """kill_tree for a Popen, falling back to killing just the process."""
    kill_tree(proc.pid)
    with contextlib.suppress(OSError):
        if proc.poll() is None:
            proc.kill()


def terminate(pid):
    """Ask a single process to stop (SIGTERM); on Windows that is TerminateProcess."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGTERM)


def _win_alive(pid):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, int(pid))         # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ctypes.get_last_error() == 5             # access denied: it exists
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return True
        return code.value == 259                        # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def pid_alive(pid):
    """Never os.kill(pid, 0) on Windows: there it terminates the process."""
    if not pid:
        return False
    if IS_WIN:
        try:
            return _win_alive(pid)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
            return f.read().split(")")[-1].split()[0] != "Z"     # zombies are dead
    except (OSError, IndexError):
        return True     # no /proc (macOS): kill(0) succeeding is the best we have


# ---------------- files ----------------
def lock(fh):
    """Exclusive, blocking lock on an open file (released by unlock or on close)."""
    if IS_WIN:
        import msvcrt
        while True:
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)     # retries for ~10 s, then raises
                return
            except OSError:
                time.sleep(0.05)
    import fcntl
    fcntl.flock(fh, fcntl.LOCK_EX)


def unlock(fh):
    if IS_WIN:
        import msvcrt
        fh.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(fh, fcntl.LOCK_UN)


def replace(src, dst, tries=40):
    """os.replace, retried: on Windows it fails while another process has dst open for reading."""
    for i in range(tries):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if not IS_WIN or i == tries - 1:
                raise
            time.sleep(0.05)


def decode(raw):
    """Child process output: UTF-8, else the Windows console code page, else latin-1."""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if IS_WIN:
        with contextlib.suppress(LookupError, UnicodeDecodeError):
            return raw.decode("oem")
    return raw.decode("latin-1", "replace")


def utf8_stdio():
    """Windows encodes redirected stdout/stderr (`finch -p ... > out.txt`, pipes) as cp1252,
    which has no ✓ or ⏺: switch them to UTF-8. Consoles already get Unicode via the wide API."""
    if not IS_WIN:
        return
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError, ValueError):
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")


def child_env(env=None, **extra):
    """Environment for a child Python/Finch: UTF-8 everywhere, so redirected output (logs,
    pipes) doesn't fall back to cp1252 on Windows and crash on a ✓."""
    e = dict(os.environ if env is None else env)
    e.setdefault("PYTHONUTF8", "1")
    e.setdefault("PYTHONIOENCODING", "utf-8")
    e.update(extra)
    return e

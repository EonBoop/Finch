"""Tool definitions and execution.

Schemas are deliberately minimal (no per-parameter descriptions): every byte of the
static prefill is paid on every turn by a small local model.
"""
import base64
import copy
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from . import config, docs, osutil
from .llm import Cancelled
from .safety import safe_path


def _f(name, desc, props, req=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props,
                       "required": req if req is not None else list(props)}}}


S, B, N = {"type": "string"}, {"type": "boolean"}, {"type": "number"}
ARR = {"type": "array", "items": {"type": "string"}}

ENABLE_TOOLS_DESC = "Enable advanced tools on demand: "
ENABLE_LISTED = ("run_python", "append_file", "delete_file", "search_files", "web_search", "web_fetch",
                 "memory_store", "memory_recall", "mcp_call", "spawn_agent")

CORE_TOOLS = [
    _f("create_file", "Create or overwrite a file. Overwriting an existing larger file requires replace=true",
       {"path": S, "content": S, "replace": B}, ["path", "content"]),
    _f("read_file", "Read a file's content (optional start_line/end_line to read a range)",
       {"path": S, "start_line": N, "end_line": N}, ["path"]),
    _f("edit_file", "Find-and-replace text in a file", {"path": S, "old": S, "new": S}),
    _f("list_dir", "List files in a directory", {"path": S}),
    _f("run_bash", "Run a shell command (cwd=workspace)", {"command": S}),
    _f("todo", "Manage the task plan. action: create(items), update(index is 1-based, or all=true), list",
       {"action": {"type": "string", "enum": ["create", "update", "list"]}, "items": ARR,
        "index": N, "done": B, "all": B}, ["action"]),
    _f("skills", "List available skills, or load one by name to follow its instructions",
       {"action": {"type": "string", "enum": ["list", "load"]}, "name": S}, ["action"]),
    _f("enable_tools", ENABLE_TOOLS_DESC + ", ".join(ENABLE_LISTED), {"tools": ARR}),
    _f("finish", "Declare the task complete with a short summary", {"summary": S}),
]
ADVANCED_TOOLS = [
    _f("run_python", "Run Python code (cwd=workspace). Prefer it over shell one-liners for data/text processing",
       {"code": S}),
    _f("append_file", "Append text to the end of a file", {"path": S, "content": S}),
    _f("delete_file", "Delete a file (moved to trash, recoverable)", {"path": S}),
    _f("search_files", "Regex content search in a directory", {"path": S, "pattern": S}),
    _f("web_search", "Search the web, return titles+links+snippets", {"query": S}),
    _f("web_fetch", "Fetch a URL: full text saved to disk, returns summary+path", {"url": S}),
    _f("web_search_multi", "Search several queries in parallel", {"queries": ARR, "max_results": N}, ["queries"]),
    _f("batch_tools", "Run several tool calls in one go: calls=[{tool,args},...]",
       {"calls": {"type": "array", "items": {"type": "object"}}}),
    _f("memory_store", "Save an important fact to long-term memory", {"text": S}),
    _f("memory_recall", "Retrieve facts from memory relevant to a query", {"query": S, "limit": N}, ["query"]),
    _f("mcp_call", "Call a tool exposed by an MCP server", {"server": S, "tool": S, "args": {"type": "object"}},
       ["server", "tool"]),
    _f("spawn_agent", "Hand one self-contained subtask to a subagent with a fresh context; it works with tools and "
       "returns only its summary. It can't see this conversation: put every detail it needs in task",
       {"task": S}),
]
ALL_BUILTIN = {t["function"]["name"]: t for t in CORE_TOOLS + ADVANCED_TOOLS}
READ_FILE_OFFICE = copy.deepcopy(ALL_BUILTIN["read_file"])     # advertised only while office_extract is on
READ_FILE_OFFICE["function"]["description"] = (
    "Read a file's content; also extracts the text of .xlsx/.docx/.pptx/.pdf (spreadsheets as numbered rows, "
    "no libraries needed). Optional start_line/end_line to read a range")
ONLINE_TOOLS = {"web_search", "web_fetch", "web_search_multi"}
MUTATING = ("create_file", "edit_file", "append_file", "delete_file", "run_bash", "run_python", "spawn_agent")
PRODUCTIVE = ("create_file", "edit_file", "append_file", "run_bash", "run_python", "spawn_agent", "finish")

REQ_ARGS = {"create_file": ["path", "content"], "read_file": ["path"], "edit_file": ["path", "old", "new"],
            "list_dir": ["path"], "run_bash": ["command"], "run_python": ["code"],
            "append_file": ["path", "content"], "delete_file": ["path"], "todo": ["action"],
            "skills": ["action"], "finish": ["summary"], "search_files": ["path", "pattern"],
            "web_search": ["query"], "web_fetch": ["url"], "spawn_agent": ["task"]}



def off_msg(name):
    return (f"[{name} is turned off by the user in /settings and cannot be used. Do the work with the tools "
            f"you have, or tell the user what you would need.]")


def enable_tools_def(settings):
    """enable_tools advertising only the advanced tools that are switched on."""
    allowed = [n for n in ENABLE_LISTED if settings.tool_on(n)]
    if len(allowed) == len(ENABLE_LISTED):
        return ALL_BUILTIN["enable_tools"]
    d = copy.deepcopy(ALL_BUILTIN["enable_tools"])
    d["function"]["description"] = ENABLE_TOOLS_DESC + (", ".join(allowed) or "(none: all turned off)")
    return d


# Small models emit industry-standard names; map instead of refusing ("tool disabled"
# sends a small model into a retry loop).
ALIASES = {
    "write_file": "create_file", "save_file": "create_file", "write": "create_file",
    "read_text_file": "read_file", "cat": "read_file", "read": "read_file", "open_file": "read_file",
    "list_directory": "list_dir", "ls": "list_dir", "list_files": "list_dir",
    "append_to_file": "append_file", "edit": "edit_file", "replace_in_file": "edit_file",
    "str_replace": "edit_file", "search_code": "search_files", "grep": "search_files",
    "delete": "delete_file", "remove_file": "delete_file", "bash": "run_bash", "shell": "run_bash",
    "execute_command": "run_bash", "run_command": "run_bash", "exec": "run_bash",
    "python": "run_python", "execute_python": "run_python", "done": "finish", "complete": "finish",
    "task_complete": "finish", "attempt_completion": "finish",
    "subagent": "spawn_agent", "spawn_subagent": "spawn_agent", "delegate": "spawn_agent",
    "delegate_task": "spawn_agent", "run_agent": "spawn_agent",
}
ARG_ALIASES = {
    "path": ("file_path", "filepath", "filename", "file", "dir", "directory", "target_file"),
    "content": ("text", "contents", "data", "body", "file_text"),
    "old": ("old_text", "old_str", "old_string", "search", "find"),
    "new": ("new_text", "new_str", "new_string", "replace", "replacement"),
    "command": ("cmd", "script", "bash", "shell"),
    "code": ("script", "source", "python"),
    "summary": ("result", "message", "answer", "text"),
    "task": ("prompt", "description", "instructions", "goal", "subtask"),
}

# ---- crawl guard v2 (byte budget; never refuses) ----
CRAWL_MULT, CRAWL_FLOOR, CRAWL_CAP = 2, 64 * 1024, 512 * 1024
CRAWL_THROTTLE = 2000
CRAWL_NUDGE_AT = (4, 10)
READ_CAP = 6000
CLOBBER_MIN_OLD, CLOBBER_RATIO = 300, 0.5
IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
IMG_MAX, IMG_PER_TURN = 20 * 1024 * 1024, 4
SKIP_DIRS = {".git", ".finch", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
             ".pytest_cache", ".tox", "dist", "build", ".idea", ".vscode"}


def read_text(path):
    for enc in ("utf-8", "latin-1"):
        try:
            with open(path, encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def decode_console(raw):
    return osutil.decode(raw)


def auto_repair(path):
    """Small models often double-escape newlines/quotes so a .py file arrives as one line
    of literal backslash sequences. Only touch .py files that fail to compile (and keep the
    repair only if it then compiles); text files need >=3 literal escapes."""
    try:
        if path.endswith((".md", ".txt", ".csv")):
            s = read_text(path)
            if "\\" in s and "\n" not in s.strip():
                lit = len(re.findall(r"\\+n", s)) + len(re.findall(r"\\+t", s))
                if lit >= 3:
                    fixed = re.sub(r"\\+t", "\t", re.sub(r"\\+n", "\n", s))
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(fixed)
                    return True
            return False
        if not path.endswith(".py"):
            return False
        def compiles():
            try:
                compile(read_text(path), path, "exec")
                return True
            except Exception:
                return False
        if compiles():
            return False
        s = read_text(path)
        if "\\" not in s:
            return False
        fixed = re.sub(r"\\+n", "\n", s)
        fixed = re.sub(r"\\+t", "\t", fixed)
        fixed = re.sub(r'\\+"', '"', fixed)
        fixed = re.sub(r"\\+'", "'", fixed).rstrip("\\")
        if fixed == s:
            return False
        with open(path, "w", encoding="utf-8") as f:
            f.write(fixed)
        if compiles():
            return True
        with open(path, "w", encoding="utf-8") as f:
            f.write(s)
        return False
    except Exception:
        return False


_OFFICE_IMPORT = re.compile(r"No module named '(openpyxl|pandas|xlrd|docx|pptx|PyPDF2|pypdf|fitz|pdfplumber)\b")


def format_bash_out(rc, out, err, small=False, hints=True):
    res = f"[exit {rc}]\nSTDOUT:\n{out[-5000:]}\nSTDERR:\n{err[-2000:]}"
    if not hints:
        pass
    elif _OFFICE_IMPORT.search(err):
        res += ("\n[hint: that library isn't installed and you don't need it: read_file(path) already returns the "
                "text of .xlsx/.docx/.pptx/.pdf files (spreadsheets as numbered rows). Use it, or process the "
                "text it gives you.]")
    elif (rc == 127 and ("not found" in err or not (out.strip() or err.strip()))) or "is not recognized as" in err:
        res += ("\n[hint: exit 127 = command not found on this machine. Python tools that aren't on PATH "
                f"usually work as modules: {osutil.PY} -m pytest / {osutil.PY} -m pip. Use {osutil.PY}.]")
    if hints and "load_setuptools_entrypoints" in err + out:
        res += ("\n[hint: pytest crashed while auto-loading a broken third-party plugin: an environment "
                f"problem, not your code. Re-run with PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 set: {osutil.PY} -m pytest -q]")
    if small and (len(out) > 2500 or len(err) > 1200):
        res = (f"[exit {rc}]\nSTDOUT (tail):\n{out[-2500:]}\nSTDERR (tail):\n{err[-1200:]}"
               "\n[output trimmed to the tail for a small model. For the full output: "
               "run `cmd > out.txt 2>&1` then read_file it in ranges.]")
    return res


def _run_proc(cmd, cwd, timeout, shell=True, env=None):
    """shell=True runs cmd (a string) in the platform shell (osutil.shell_path); else cmd is argv."""
    env = osutil.child_env(env, PAGER="cat", GIT_PAGER="cat")
    args, use_shell = osutil.shell_command(cmd) if shell else (cmd, False)
    proc = subprocess.Popen(args, shell=use_shell, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **osutil.hidden_group())
    try:
        ob, eb = proc.communicate(timeout=timeout)
        return proc.returncode, decode_console(ob), decode_console(eb), False
    except subprocess.TimeoutExpired:
        # killing only the shell leaves grandchildren holding the pipe: kill the tree
        osutil.kill_proc_tree(proc)
        try:
            ob, eb = proc.communicate(timeout=10)
        except Exception:
            ob, eb = b"", b""
        return proc.returncode or 1, decode_console(ob), decode_console(eb), True
    except KeyboardInterrupt:
        osutil.kill_proc_tree(proc)
        raise


class Rollback:
    """Ring 4: every write is preceded by a backup; /undo walks the journal back."""

    def __init__(self, workdir, enabled=lambda: True):
        self.workdir = workdir
        self.journal = []
        self.enabled = enabled

    @property
    def root(self):
        return os.path.join(self.workdir, ".finch")

    def rel(self, real):
        try:
            return os.path.relpath(real, self.workdir)
        except ValueError:
            return os.path.basename(real)

    def backup(self, real):
        """Snapshot before a write. Returns the model-facing backup path (or None if new)."""
        if not self.enabled():
            return None
        entry = {"path": real, "backup": None, "ts": time.time()}
        shown = None
        if os.path.isfile(real):
            rel = self.rel(real)
            if rel.startswith(".."):
                rel = os.path.basename(real)
            latest = os.path.join(self.root, "backups", rel + ".bak")
            hist = os.path.join(self.root, "backups", ".history",
                                f"{int(time.time()*1000)}_{len(self.journal)}_" + rel.replace(os.sep, "__"))
            os.makedirs(os.path.dirname(latest), exist_ok=True)
            os.makedirs(os.path.dirname(hist), exist_ok=True)
            shutil.copy2(real, latest)
            shutil.copy2(real, hist)
            entry["backup"] = hist
            shown = os.path.relpath(latest, self.workdir)
        self.journal.append(entry)
        return shown

    def trash(self, real):
        if not self.enabled():
            os.remove(real)
            return None
        d = os.path.join(self.root, "trash")
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, f"{os.path.basename(real)}.{time.strftime('%H%M%S')}")
        shutil.move(real, dst)
        self.journal.append({"path": real, "backup": dst, "ts": time.time(), "trashed": True})
        return os.path.relpath(dst, self.workdir)

    def undo(self):
        if not self.journal:
            return None
        e = self.journal.pop()
        p = e["path"]
        if e.get("backup") and os.path.exists(e["backup"]):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            shutil.copy2(e["backup"], p)
            return f"restored {self.rel(p)}"
        if os.path.exists(p):
            self.trash(p)
            self.journal.pop()
            return f"removed newly created {self.rel(p)} (moved to .finch/trash)"
        return f"nothing to restore for {self.rel(p)}"


def bash_snapshot(cmd, workdir, rollback):
    """sed -i / > redirects / cp / mv rewrite files in place, bypassing the write tools'
    backups. Snapshot every existing workdir file the command names."""
    low = f" {cmd} "
    if not any(s in low for s in ("sed -i", "sed --in-place", ">", " tee ", "cp ", "mv ",
                                  "truncate ", "perl -pi", "dd ")):
        return []
    out = []
    wd = os.path.realpath(workdir)
    for tok in re.findall(r"[A-Za-z0-9_\-./\\:]+\.[A-Za-z0-9]{1,8}", cmd):
        base = os.path.basename(tok)
        if base.endswith(".bak") or base.startswith("."):
            continue
        rp = os.path.realpath(os.path.join(workdir, tok))
        inside = os.path.normcase(rp).startswith(os.path.normcase(wd) + os.sep)
        if not inside or not os.path.isfile(rp) or f"{os.sep}.finch{os.sep}" in rp:
            continue
        try:
            rollback.backup(rp)
            if base not in out:
                out.append(base)
        except Exception:
            continue
        if len(out) >= 20:
            break
    return out


def walk_files(root, limit=20000):
    n = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in fns:
            yield os.path.join(dp, fn)
            n += 1
            if n >= limit:
                return


# ---- todo ----
def format_todo(t):
    if not t:
        return "(todo list empty)"
    return "\n".join(f"{'[x]' if x.get('done') else '[ ]'} {i}. {x.get('item', '')}"
                     for i, x in enumerate(t, 1))


def todo_progress(t):
    t = t or []
    return sum(1 for x in t if x.get("done")), len(t)


# ---- memory ----
def _load_memory():
    try:
        with open(config.MEMORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def memory_store(text):
    mem = _load_memory()
    mem.append({"text": text, "ts": time.strftime("%Y-%m-%d %H:%M")})
    config.atomic_write_json(config.MEMORY_FILE, mem)
    return f"[stored to memory: {text[:60]}]"


def memory_recall(query, limit=3):
    mem = _load_memory()
    if not mem:
        return "(no memory)"
    q = set(re.findall(r"\w+", query.lower()))
    scored = sorted(((len(q & set(re.findall(r"\w+", m["text"].lower()))), m) for m in mem),
                    key=lambda x: -x[0])
    hits = [f"[{m['ts']}] {m['text']}" for s, m in scored if s > 0][:int(limit or 3)]
    return "\n".join(hits or [m["text"] for m in mem[-3:]])


# ---- skills (progressive disclosure: names + one line, full text on load) ----
PKG_SKILLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")


def _skill_bases(workdir):
    return [(os.path.join(workdir, ".finch", "skills"), "project"), (config.SKILLS_DIR_HOME, "user"),
            (PKG_SKILLS, "built-in")]


def _skill_desc(path):
    try:
        head = read_text(path)[:600]
    except Exception:
        return ""
    m = re.search(r"description:\s*(.+)", head)
    if m:
        return m.group(1).strip().strip("\"'")[:120]
    for ln in head.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith(("#", "-", "---")):
            return ln[:120]
    return ""


def skill_infos(workdir):
    """Every skill by name (project > user > built-in): [{name, desc, scope, path}]."""
    seen = {}
    for base, scope in _skill_bases(workdir):
        if os.path.isdir(base):
            for f in sorted(os.listdir(base)):
                n = f.rsplit(".", 1)[0]
                if f.endswith((".md", ".txt")) and n not in seen:
                    p = os.path.join(base, f)
                    seen[n] = {"name": n, "desc": _skill_desc(p), "scope": scope, "path": p}
    return [seen[n] for n in sorted(seen)]


def list_skills(workdir, off=()):
    lines = [f"- {i['name']}: {i['desc']}" if i["desc"] else f"- {i['name']}"
             for i in skill_infos(workdir) if i["name"] not in off]
    return "\n".join(lines) or "(none)"


SKILL_INDEX_MAX = 12


def skills_def(workdir, off=()):
    """skills with the enabled skills listed inline, so one call loads the right one (no list round-trip)."""
    infos = [i for i in skill_infos(workdir) if i["name"] not in off]
    rows = [f"{i['name']} ({i['desc'][:80]})" if i["desc"] else i["name"] for i in infos[:SKILL_INDEX_MAX]]
    more = len(infos) - SKILL_INDEX_MAX
    tail = f"; +{more} more (action=list)" if more > 0 else ""
    return _f("skills", "Load a skill's step-by-step instructions by name: " + "; ".join(rows) + tail,
              {"name": S, "action": {"type": "string", "enum": ["load", "list"]}}, ["name"])


def load_skill(name, workdir, off=()):
    info = next((i for i in skill_infos(workdir) if i["name"] == str(name)), None)
    if info and info["name"] not in off:
        return f"### SKILL: {name}\n{read_text(info['path'])}"
    why = "is turned off in /settings" if info else "not found"
    return f"[skill '{name}' {why}. Available:\n{list_skills(workdir, off)}]"


SKILL_TEMPLATE = """---
name: {name}
description: {desc}
---
# {title}
1. <First step: what to read, run or check.>
2. <Next step. Keep steps short and concrete: small models follow numbered lists best.>
3. Verify: <how to check the result before calling finish.>
"""


def skill_dir(workdir=None, project=False):
    return os.path.join(workdir, ".finch", "skills") if project and workdir else config.SKILLS_DIR_HOME


def skill_slug(name):
    return re.sub(r"[^\w.-]+", "_", str(name).strip()).strip("_.-")


def create_skill(name, desc="", workdir=None, project=False, body=None):
    """Write a new skill (template unless body is given). Returns (name, path); raises ValueError if it exists."""
    n = skill_slug(name)
    if not n:
        raise ValueError("a skill needs a name (letters, digits, _ . -)")
    base = skill_dir(workdir, project)
    path = os.path.join(base, n + ".md")
    if os.path.exists(path) or os.path.exists(os.path.join(base, n + ".txt")):
        raise ValueError(f"skill '{n}' already exists (/skills edit {n})")
    os.makedirs(base, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body if body is not None else
                SKILL_TEMPLATE.format(name=n, desc=desc or "my custom skill", title=n.replace("_", " ").capitalize()))
    return n, path


def import_skill(src, workdir=None, project=False, name=None):
    """Copy an existing .md/.txt file in as a skill. Returns (name, path)."""
    src = os.path.abspath(os.path.expanduser(str(src)))
    if not os.path.isfile(src):
        raise ValueError(f"no such file: {src}")
    n = name or os.path.splitext(os.path.basename(src))[0]
    m = re.search(r"^name:\s*(.+)$", read_text(src)[:600], re.M)
    if not name and m and os.path.basename(src).upper() == "SKILL.MD":   # Claude-style <dir>/SKILL.md
        n = m.group(1).strip().strip("\"'")
    return create_skill(n, workdir=workdir, project=project, body=read_text(src))


def editable_skill(name, workdir):
    """Path to edit for a skill: built-ins are copied to the user dir first (the copy overrides by name)."""
    info = next((i for i in skill_infos(workdir) if i["name"] == name), None)
    if not info:
        raise ValueError(f"no skill named '{name}'")
    if info["scope"] != "built-in":
        return info["path"]
    os.makedirs(config.SKILLS_DIR_HOME, exist_ok=True)
    dst = os.path.join(config.SKILLS_DIR_HOME, os.path.basename(info["path"]))
    if not os.path.exists(dst):
        shutil.copyfile(info["path"], dst)
    return dst


def delete_skill(name, workdir):
    """Delete a project or user skill (built-ins can only be switched off). Returns (path, scope now used or None)."""
    info = next((i for i in skill_infos(workdir) if i["name"] == name), None)
    if not info:
        raise ValueError(f"no skill named '{name}'")
    if info["scope"] == "built-in":
        raise ValueError(f"'{name}' is built-in: switch it off instead (/skills {name} off)")
    os.remove(info["path"])
    left = next((i for i in skill_infos(workdir) if i["name"] == name), None)
    return info["path"], left["scope"] if left else None


# ---- web ----
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def _cache_get(key, ttl=3600):
    p = os.path.join(config.CACHE_DIR, key + ".json")
    try:
        if time.time() - os.path.getmtime(p) < ttl:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _cache_set(key, data):
    try:
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        config.atomic_write_json(os.path.join(config.CACHE_DIR, key + ".json"), data)
    except Exception:
        pass


def _ckey(*parts):
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]


def web_search(query, max_results=5, backends=None):
    import requests
    from bs4 import BeautifulSoup
    key = _ckey("search", query, max_results)
    hit = _cache_get(key)
    if hit:
        return hit
    last = ""
    for be in (backends or ["duckduckgo", "bing"]):
        try:
            out = []
            if be == "duckduckgo":
                r = requests.post("https://html.duckduckgo.com/html/", data={"q": query},
                                  headers={"User-Agent": _UA}, timeout=20)
                soup = BeautifulSoup(r.text, "html.parser")
                for res in soup.select("div.result")[:max_results]:
                    a = res.select_one("a.result__a")
                    if not a:
                        continue
                    href = a.get("href", "")
                    m = re.search(r"uddg=([^&]+)", href)
                    if m:
                        import urllib.parse
                        href = urllib.parse.unquote(m.group(1))
                    sn = res.select_one(".result__snippet")
                    out.append(f"- {a.get_text(strip=True)}\n  {href}\n  {sn.get_text(strip=True)[:220] if sn else ''}")
            else:
                url = be if be.startswith("http") else "https://www.bing.com/search"
                r = requests.get(url, params={"q": query}, headers={"User-Agent": _UA}, timeout=20)
                soup = BeautifulSoup(r.text, "html.parser")
                for li in soup.select("li.b_algo")[:max_results]:
                    a = li.select_one("h2 a")
                    if not a:
                        continue
                    cap = li.select_one(".b_caption p") or li.select_one("p")
                    out.append(f"- {a.get_text(strip=True)}\n  {a.get('href', '')}\n  "
                               f"{cap.get_text(strip=True)[:220] if cap else ''}")
            if out:
                res = "\n".join(out)
                _cache_set(key, res)
                return res
        except Exception as e:
            last = str(e)[:100]
    return f"[web_search error: no results{(': ' + last) if last else ''}]"


def web_fetch(url, workdir):
    """Full text to disk, a summary into context: the model can read_file any part later."""
    key = _ckey("fetch", url)
    hit = _cache_get(key)
    if hit:
        return hit
    try:
        import requests
        from bs4 import BeautifulSoup
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=25)
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
            t.decompose()
        full = re.sub(r"\n\s*\n+", "\n\n", soup.get_text("\n", strip=True))
        if not full:
            return "[web_fetch error: page has no text]"
        d = os.path.join(workdir, ".finch", "sources")
        os.makedirs(d, exist_ok=True)
        rel = os.path.join(".finch", "sources", f"fetch_{key}.txt")
        with open(os.path.join(workdir, rel), "w", encoding="utf-8") as f:
            f.write(f"URL: {url}\n\n{full}")
        out = full[:1200] + f"\n[full text saved: {rel} ({len(full)} chars). read_file it for details.]"
        _cache_set(key, out)
        return out
    except Exception as e:
        return f"[web_fetch error: {e}]"


def normalize_args(name, args):
    """Accept common argument-name variants instead of failing on a missing key."""
    if not isinstance(args, dict):
        return {}
    req = REQ_ARGS.get(name, [])
    for canon, alts in ARG_ALIASES.items():
        if canon in req and canon not in args:
            for a in alts:
                if a in args:
                    args[canon] = args.pop(a)
                    break
    return args


class ToolBox:
    """Executes tools against one workdir. Holds per-task state the tools need."""

    def __init__(self, settings, gate, workdir, mcp=None):
        self.s = settings
        self.gate = gate
        self.mcp = mcp
        self.set_workdir(workdir)
        self.todo = []
        self.small_model = False
        self.pending_images = []
        self.spawner = None     # set by a top-level Agent; None in a subagent (no grandchildren)

    def set_workdir(self, workdir):
        self.workdir = os.path.abspath(workdir)
        self.rollback = Rollback(self.workdir, enabled=lambda: self.s.on("backups"))
        self.crawl = {}
        self.partial = {}     # real path -> [total lines, [(first, last) line ranges the model has seen]]

    def p(self, path):
        return os.path.realpath(os.path.join(self.workdir, os.path.expanduser(str(path))))

    def disp(self, real):
        r = os.path.relpath(real, self.workdir)
        return real if r.startswith("..") else r

    # ---- entry ----
    def run(self, name, args):
        args = dict(args or {})
        if self.s.on("aliases"):
            name = ALIASES.get(name, name)
            args = normalize_args(name, args)
        if not self.s.tool_on(name):
            return off_msg(name)
        try:
            if "." in name and self.mcp and self.mcp.is_tool(name):
                gate = self.gate.check(name, args, self.workdir)
                if gate:
                    return gate
                return self.mcp.call_flat(name, args, self.workdir)
            gate = self.gate.check(name, args, self.workdir)
            if gate:
                return gate
            fn = getattr(self, "t_" + name, None)
            if fn is None:
                return f"[unknown tool: {name}. Available: {', '.join(sorted(ALL_BUILTIN))}]"
            return fn(args)
        except KeyError as e:
            req = REQ_ARGS.get(name, [])
            got = list(args)
            missing = [k for k in req if k not in args] or req
            ex = ""
            if not self.s.on("env_hints"):
                return f"[tool error: {name} is missing argument {e}]"
            if name == "create_file":
                ex = ' Correct example: create_file(path="app.py", content="...file content...")'
            elif name == "edit_file":
                ex = ' Correct example: edit_file(path="app.py", old="exact old text", new="new text")'
            elif name == "append_file":
                ex = ' Correct example: append_file(path="notes.md", content="text to append")'
            return (f"[tool error: {name} is missing argument {e}. Required: {missing}. "
                    f"You passed: {got}.{ex} Retry with the missing argument added, content unchanged.]")
        except (KeyboardInterrupt, Cancelled):     # Cancelled can come out of a subagent
            raise
        except Exception as e:
            return f"[tool error: {type(e).__name__}: {e}]"

    # ---- files ----
    def t_create_file(self, a):
        path = str(a["path"])
        content = a["content"]
        if not isinstance(content, str):
            content = json.dumps(content, indent=2) if isinstance(content, (dict, list)) else str(content)
        real = self.p(path)
        if os.path.isdir(real):
            return f"[tool error: '{path}' is a directory]"
        if os.path.exists(real) and not a.get("replace") and self.s.on("clobber_guard"):
            old = os.path.getsize(real)
            if old > CLOBBER_MIN_OLD and len(content.encode()) < CLOBBER_RATIO * old:
                return (f"[clobber guard: '{path}' already has {old} bytes and your new content is only "
                        f"{len(content.encode())} bytes (looks like overwriting a big file with a fragment). "
                        f"To replace it entirely: retry with replace=true. To add content: append_file. "
                        f"To change part of it: edit_file.]")
        bak = self.rollback.backup(real)
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "w", encoding="utf-8") as f:
            f.write(content)
        fixed = self.s.on("auto_repair") and auto_repair(real)
        tag = "auto-repaired escaped newlines" if fixed else f"{len(content.encode())} bytes"
        return f"[created {path} ({tag}{'; previous version backed up: ' + bak if bak else ''})]"

    def t_read_file(self, a):
        path = str(a["path"])
        real = self.p(path)
        if not os.path.exists(real):
            return f"[not found: {path}]"
        if os.path.isdir(real):
            return f"[{path} is a directory; use list_dir]"
        if real.lower().endswith(IMG_EXTS):
            return self._read_image(path, real)
        ext = os.path.splitext(real)[1].lower()
        if ext in docs.LEGACY_EXTS and self.s.on("office_extract"):
            return (f"[{path} is an old binary {ext} file and can't be read as text. Convert it first: "
                    f"`libreoffice --headless --convert-to {ext + 'x'} {path}` (then read the {ext + 'x'}), "
                    f"or tell the user.]")
        st = self.crawl.setdefault(real, [0, 0, 0, False])   # reads, chars served, nudges, throttle-note
        st[0] += 1
        if ext in docs.DOC_EXTS and self.s.on("office_extract"):
            try:
                text = f"[{ext[1:]} file, text extracted by the harness]\n" + docs.extract(real)
            except docs.DocError as e:
                return f"[cannot read {path}: {e}]"
        else:
            text = read_text(real)
        lines = text.splitlines()
        sl, el = int(a.get("start_line") or 0), int(a.get("end_line") or 0)
        if sl > 0 or el > 0:
            el = el if el > 0 else len(lines)
            sl = max(1, sl)
            body = "\n".join(lines[sl - 1:el])[:READ_CAP]
            meta = (f"\n[file has {len(lines)} lines; showed {sl}-{min(el, len(lines))}. "
                    f"Other parts: read_file(start_line=.., end_line=..)]")
        else:
            body = text[:READ_CAP]
            meta = ""
            if len(text) > READ_CAP:
                shown = body.count("\n") + 1
                meta = (f"\n[truncated: file has {len(lines)} lines / {len(text)} chars; showed about the first "
                        f"{shown} lines. Read more with read_file(start_line={shown}, end_line={shown + 150}).]")
        st[1] += len(body)
        if sl > 0 or el > 0:
            first = sl
        else:
            first = 1
        # a cut-off last line was not really seen
        shown = len(body.splitlines()) - (1 if body and len(body) >= READ_CAP and not body.endswith("\n") else 0)
        self.partial.setdefault(real, [len(lines), []])[1].append((first, first + shown - 1))
        try:
            budget = min(max(CRAWL_MULT * os.path.getsize(real), CRAWL_FLOOR), CRAWL_CAP)
        except OSError:
            budget = CRAWL_FLOOR
        if not self.s.on("crawl_guard"):
            return body + meta
        if st[1] > budget:
            # budget spent: throttle, never refuse. The hint never replaces data.
            if not st[3]:
                st[3] = True
                note = (f"[crawl-guard: '{path}' has served {st[1]} chars, over this file's budget of {budget} "
                        f"({CRAWL_MULT}x file size). Reads still work but return at most {CRAWL_THROTTLE} chars "
                        f"each. To process this file efficiently, run one python script that does the "
                        f"counting/searching/aggregation, e.g.\n  {osutil.PY} -c \"import collections; "
                        f"ls=open('{path}').readlines(); print(len(ls))\"\n]")
            else:
                note = f"[crawl-guard: '{path}' throttled ({CRAWL_THROTTLE} chars/read). A script gets it all at once.]"
            return note + "\n" + body[:CRAWL_THROTTLE] + meta
        if st[0] in CRAWL_NUDGE_AT and st[2] < 2:
            st[2] += 1
            return (f"[note: read #{st[0]} of '{path}'. If you are counting/searching/aggregating, a python "
                    f"script via run_bash is faster; ignore this if you are just re-checking.]\n" + body + meta)
        return body + meta

    def _scripted(self, text):
        """A script that names a partly-read file is how big files get processed whole."""
        for real in [r for r in self.partial if os.path.basename(r) in text]:
            del self.partial[real]

    def unread_parts(self):
        """[(relative path, total lines, unseen lines, first unseen line)] for files read only in part."""
        out = []
        for real, (total, ranges) in self.partial.items():
            seen, nxt = 0, 1
            for a, b in sorted(ranges):
                a = max(a, nxt)
                if b >= a:
                    seen += b - a + 1
                    nxt = b + 1
            if total > seen and os.path.exists(real):
                # first unseen line = first gap in the merged ranges
                gap = 1
                for a, b in sorted(ranges):
                    if a <= gap:
                        gap = max(gap, b + 1)
                out.append((os.path.relpath(real, self.workdir), total, total - seen, gap))
        return out

    def _read_image(self, path, real):
        size = os.path.getsize(real)
        if size > IMG_MAX:
            return f"[image too large: {path} ({size // 1048576} MB, limit 20 MB)]"
        if len(self.pending_images) >= IMG_PER_TURN:
            return f"[image queue full ({IMG_PER_TURN}); analyse the attached images before reading more]"
        with open(real, "rb") as f:
            self.pending_images.append(base64.b64encode(f.read()).decode("ascii"))
        return (f"[image attached: {path} ({size // 1024} KB)]: it is loaded with this turn; "
                f"describe, read text from, or analyse it directly.")

    def t_edit_file(self, a):
        path = str(a["path"])
        real = self.p(path)
        if not os.path.isfile(real):
            return f"[edit failed: not found: {path}. Use create_file to create it.]"
        s = read_text(real)
        old, new = str(a["old"]), str(a["new"])
        if old not in s:
            # double-escaped newlines are the most common small-model mismatch
            o2 = old.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
            if o2 in s:
                old, new = o2, new.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
            else:
                o3 = "\n".join(ln.rstrip() for ln in old.splitlines())
                s3 = "\n".join(ln.rstrip() for ln in s.splitlines())
                if o3 and o3 in s3 and s3 != s:
                    s, old = s3, o3
                else:
                    preview = s[:200].replace("\n", "\\n")
                    return (f"[edit failed: old text not found in {path}. File starts: {preview}...] "
                            f"read_file it to copy the exact text, or rewrite it with create_file.")
        if old == "":
            return "[edit failed: old must not be empty. Use append_file or create_file.]"
        n = s.count(old)
        bak = self.rollback.backup(real)
        with open(real, "w", encoding="utf-8") as f:
            f.write(s.replace(old, new, 1))
        fixed = self.s.on("auto_repair") and auto_repair(real)
        more = f"note: old text occurs {n} times, replaced the first" if n > 1 else ""
        parts = ([f"backup: {bak}"] if bak else []) + (["auto-repaired escapes"] if fixed else []) \
            + ([more] if more else [])
        return f"[edited {path}" + (f" ({'; '.join(parts)})" if parts else "") + "]"

    def t_append_file(self, a):
        path = str(a["path"])
        real = self.p(path)
        self.rollback.backup(real)
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "a", encoding="utf-8") as f:
            f.write(str(a["content"]))
        if self.s.on("auto_repair"):
            auto_repair(real)
        return f"[appended {len(str(a['content']).encode())} bytes to {path} (now {os.path.getsize(real)} bytes)]"

    def t_delete_file(self, a):
        path = str(a["path"])
        real = self.p(path)
        if not os.path.exists(real):
            return f"[not found: {path}]"
        if os.path.isdir(real):
            return f"[delete_file only deletes files; '{path}' is a directory]"
        where = self.rollback.trash(real)
        if where is None:
            return f"[deleted {path} (permanently: backups are off)]"
        return f"[deleted {path} -> moved to {where} (recoverable)]"

    def t_list_dir(self, a):
        path = str(a.get("path") or ".")
        real = self.p(path)
        if not os.path.isdir(real):
            return f"[not a directory: {path}]"
        names = []
        for n in sorted(os.listdir(real)):
            if n == ".finch":
                continue
            names.append(n + ("/" if os.path.isdir(os.path.join(real, n)) else ""))
        if len(names) > 300:
            names = names[:300] + [f"... ({len(names) - 300} more)"]
        return "\n".join(names) or "(empty directory)"

    def t_search_files(self, a):
        real = self.p(a.get("path") or ".")
        try:
            pat = re.compile(str(a["pattern"]))
        except re.error:
            pat = re.compile(re.escape(str(a["pattern"])))
        out = []
        files = [real] if os.path.isfile(real) else walk_files(real)
        for fp in files:
            try:
                if os.path.getsize(fp) > 2_000_000:
                    continue
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if pat.search(line):
                            out.append(f"{os.path.relpath(fp, self.workdir)}:{i}: {line.strip()[:140]}")
                            if len(out) >= 40:
                                break
            except Exception:
                continue
            if len(out) >= 40:
                out.append("[40 matches shown; narrow the pattern or path]")
                break
        return "\n".join(out) or "(no matches)"

    # ---- execution ----
    def t_run_bash(self, a):
        cmd = str(a["command"])
        # Small models double-escape: only fold runs of >=2 backslashes, so legit `\n`
        # inside printf/sed survives.
        if "\n" not in cmd and re.search(r"\\\\+n", cmd):
            cmd = re.sub(r"\\\\+n", "\n", cmd)
        cmd = re.sub(r'\\\\+"', '"', cmd)
        snapped = bash_snapshot(cmd, self.workdir, self.rollback) if self.s.on("backups") else []
        self._scripted(cmd)
        rc, out, err, timed_out = _run_proc(cmd, self.workdir, self.s.bash_timeout)
        if timed_out:
            out = out[:4000] + f"\n[timed out after {self.s.bash_timeout}s; process tree killed]"
        res = format_bash_out(rc, out, err, self.small_model, self.s.on("env_hints"))
        if snapped:
            res += f"\n[auto-backup before in-place rewrite: {', '.join(snapped)} -> .finch/backups/]"
        if self.s.on("env_hints") and re.search(r"(^|\s)(python3?|node|npm|top|vim?|nano|less|more)\s*$", cmd.strip()):
            res += "\n[hint: interactive programs get no stdin here; pass a script/arguments instead.]"
        return res

    def t_run_python(self, a):
        code = str(a.get("code") or a.get("script") or "")
        self._scripted(code)
        fd, script = tempfile.mkstemp(suffix=".py", prefix="finch_run_", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            rc, out, err, timed_out = _run_proc([self.s.python, script], self.workdir,
                                                self.s.bash_timeout, shell=False)
            if timed_out:
                out = out[:4000] + f"\n[timed out after {self.s.bash_timeout}s; killed]"
            return format_bash_out(rc, out, err.replace(script, "<run_python>"), self.small_model,
                                   self.s.on("env_hints"))
        finally:
            try:
                os.remove(script)
            except OSError:
                pass

    # ---- plan / meta ----
    def t_todo(self, a):
        act = a.get("action", "list")
        if act == "create":
            items = a.get("items") or a.get("plan") or a.get("steps") or a.get("tasks") or []
            if isinstance(items, str):
                items = [ln.strip(" -*0123456789.)") for ln in items.splitlines() if ln.strip()]
            items = [str(i.get("item") or i.get("content") or i) if isinstance(i, dict) else str(i)
                     for i in items if str(i).strip()]
            if not items:
                return '[todo create: no items. Pass a list of strings: items=["step 1", "step 2"]]'
            self.todo = [{"item": i, "done": False} for i in items]
            return format_todo(self.todo)
        if act == "update":
            if not self.todo:
                return "[todo update: the plan is empty. Create one first: todo(action=create, items=[...])]"
            done = bool(a.get("done", True))
            if a.get("all"):
                for x in self.todo:
                    x["done"] = done
                return format_todo(self.todo)
            idx = a.get("index", a.get("indices", 0))
            idx = idx if isinstance(idx, (list, tuple)) else [idx]
            hit = 0
            for i in idx:
                try:
                    j = int(i)
                except (TypeError, ValueError):
                    continue
                if 1 <= j <= len(self.todo):
                    self.todo[j - 1]["done"] = done
                    hit += 1
            if not hit:
                return (f"[todo update: invalid index (plan has {len(self.todo)} items, valid: "
                        f"{', '.join(map(str, range(1, len(self.todo) + 1)))}). "
                        f"To tick everything: todo(action=update, all=true)]")
            return format_todo(self.todo)
        return format_todo(self.todo)

    def t_skills(self, a):
        if not a.get("name") or a.get("action") == "list":
            return list_skills(self.workdir, self.s.skills_off)
        return load_skill(a["name"], self.workdir, self.s.skills_off)

    def t_finish(self, a):
        return "[TASK_COMPLETE] " + str(a.get("summary", ""))

    def t_memory_store(self, a):
        return memory_store(str(a["text"]))

    def t_memory_recall(self, a):
        return memory_recall(str(a["query"]), a.get("limit", 3))

    def t_web_search(self, a):
        if self.s.offline:
            return "[offline mode: web tools are disabled]"
        return web_search(str(a["query"]), backends=self.s.search_backends or None)

    def t_web_fetch(self, a):
        if self.s.offline:
            return "[offline mode: web tools are disabled]"
        return web_fetch(str(a["url"]), self.workdir)

    def t_web_search_multi(self, a):
        if self.s.offline:
            return "[offline mode: web tools are disabled]"
        import concurrent.futures as cf
        qs = [str(q) for q in (a.get("queries") or [])][:6]
        if not qs:
            return "[web_search_multi: pass queries=[...]]"
        k = int(a.get("max_results") or 3)
        with cf.ThreadPoolExecutor(max_workers=len(qs)) as ex:
            res = list(ex.map(lambda q: web_search(q, k, self.s.search_backends or None), qs))
        return "\n\n".join(f"### {q}\n{r}" for q, r in zip(qs, res))

    def t_batch_tools(self, a):
        calls = a.get("calls") or []
        if not isinstance(calls, list) or not calls:
            return "[batch_tools: pass calls=[{tool, args}, ...]]"
        out = []
        for i, c in enumerate(calls[:8], 1):
            if not isinstance(c, dict):
                continue
            t = c.get("tool") or c.get("name") or ""
            if t == "batch_tools":
                out.append(f"### [{i}] {t}\n[nested batch_tools refused]")
                continue
            out.append(f"### [{i}] {t}\n{str(self.run(t, c.get('args') or c.get('arguments') or {}))[:2000]}")
        return "\n\n".join(out)

    def t_mcp_call(self, a):
        if not self.mcp:
            return "[mcp_call: no MCP servers configured (~/.finch/mcp.json)]"
        if not self.s.tool_on(f"{a['server']}.{a['tool']}"):
            return off_msg(f"MCP server {a['server']}")
        return self.mcp.call(str(a["server"]), str(a["tool"]), a.get("args") or {}, self.workdir)

    def t_spawn_agent(self, a):
        task = str(a["task"])
        if self.spawner is None:
            return "[tool error: spawn_agent: a subagent can't start subagents of its own. Do this part yourself.]"
        return self.spawner(task)

    def t_enable_tools(self, a):
        # handled by the agent (it owns the active tool surface); this is a fallback
        return "[enable_tools handled by agent]"


# ---- artifact claims (finish gates) ----
ARTIFACT_EXTS = (".md", ".txt", ".png", ".jpg", ".jpeg", ".csv", ".py", ".json", ".docx", ".xlsx",
                 ".pdf", ".html", ".yaml", ".yml", ".svg", ".tex", ".tsv", ".log", ".js", ".ts",
                 ".css", ".sh", ".toml", ".ini", ".xml", ".sql", ".go", ".rs", ".java", ".c", ".cpp", ".h")
FILE_TOKEN = re.compile(r"[A-Za-z0-9_\-./]*[A-Za-z0-9_\-]\.[A-Za-z0-9]{1,5}\b")


_TECH_NAMES = {"node.js", "next.js", "vue.js", "react.js", "three.js", "d3.js", "express.js", "nuxt.js",
               "chart.js", "p5.js", "deno.js", "angular.js", "ember.js", "socket.io", "asp.net"}


def missing_files_in_text(text, workdir):
    """File names mentioned in text that do not exist anywhere in the workdir."""
    toks = {m.group(0).strip("./") for m in FILE_TOKEN.finditer(text or "")
            if m.group(0).lower().endswith(ARTIFACT_EXTS)}
    toks = {t for t in toks if t and not re.match(r"^\d+(\.\d+)+$", t) and "://" not in t
            and os.path.basename(t).lower() not in _TECH_NAMES}
    if not toks:
        return []
    names = None
    missing = []
    for tok in sorted(toks):
        if os.path.isfile(os.path.join(workdir, tok)):
            continue
        if names is None:
            names = [os.path.basename(f).lower() for f in walk_files(workdir)]
        base = os.path.basename(tok).lower()
        if base in names or any(base.endswith(n) for n in names if len(n) > 3):
            continue
        missing.append(tok)
    return missing


def workdir_listing(workdir, limit=40):
    try:
        names = sorted(n for n in os.listdir(workdir) if n != ".finch")
        return "\n".join(names[:limit]) or "(empty)"
    except Exception:
        return "(empty)"


def find_tests(workdir):
    pats = ("test_*.py", "*_test.py", "tests/test_*.py", "tests/*_test.py", "test/test_*.py")
    for p in pats:
        if glob.glob(os.path.join(workdir, p)):
            return True
    return False

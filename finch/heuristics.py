"""Pure heuristics used by the agent loop (no I/O except the test guard)."""
import json
import os
import re
import shutil
import subprocess
from difflib import SequenceMatcher

from . import osutil

# ---------------- Q&A vs task layering ----------------
TASK_HINTS = ("write ", "create ", "build ", "implement", "fix ", "fix the", "debug", "analyze", "analyse",
              "clean ", "optimize", "optimise", "refactor", "generate", "design ", "audit ", "review ",
              "compute", "convert", "export", "import ", "plot ", "profile ", "produce ", "research ",
              "search for", "find all", "list all", "make ", "add ", "remove ", "delete ", "rename ",
              "update ", "change ", "edit ", "run ", "install", "setup", "set up", "scaffold", "test ",
              "save ", "download", "scrape", "translate", "summarize", "summarise", "organize", "organise",
              "deploy", "migrate", "port ", "rewrite", "draft ", "compare ", "benchmark", "step 1",
              "todo(action", "modify", "move ", "copy ", "count ", "calculate", "parse ", "extract ",
              "delegate", "spawn a", "spawn sub", "subagent to", "subagents to", "sub-agent to", "sub-agents to",
              "use a subagent", "use subagents", "a subagent for", "subagents for")
QA_HINTS = ("what is", "what are", "what's", "how do i", "how does", "how do you", "why is", "why does",
            "can you explain", "explain", "tell me about", "who is", "who are", "when was", "where is",
            "hi", "hello", "hey", "thanks", "thank you", "good morning", "who are you", "?")
HARNESS_PREFIXES = ("⚠️", "⏱", "Continue:", "[harness]", "[context summary]")


def hint_hit(hint, low):
    """ASCII hints match on word boundaries ("hi" must not hit "this", "fix" not "prefix")."""
    h = hint.strip()
    if not h.isascii() or not re.match(r"\w", h):
        return h in low
    return re.search(r"\b" + re.escape(h) + (r"\b" if re.match(r".*\w$", h) else ""), low) is not None


def is_harness_note(text):
    return str(text or "").startswith(HARNESS_PREFIXES)


def last_user_text(messages):
    for m in reversed(messages):
        if m.get("role") == "user" and not is_harness_note(m.get("content")):
            t = str(m.get("content") or "").strip()
            if t:
                return t
    return ""


def has_task_progress(messages):
    """Has this session already done productive work? Then a short follow-up such as
    "now try the other file" continues the task instead of being treated as chit-chat."""
    for m in messages:
        for tc in (m.get("tool_calls") or []) if m.get("role") == "assistant" else []:
            n = str(((tc or {}).get("function") or {}).get("name", ""))
            if n in ("create_file", "edit_file", "append_file", "delete_file", "run_bash", "run_python"):
                return True
    return False


def is_qa(text, has_progress=False):
    low = text.lower().strip()
    if not low:
        return False
    if any(hint_hit(h, low) for h in TASK_HINTS):
        return False
    if any(hint_hit(h, low) for h in QA_HINTS):
        return True
    if len(low) <= 40:
        return not has_progress
    return False


# ---------------- flat prefill: category routing ----------------
BASE_TOOLS = {"read_file", "list_dir", "todo", "skills", "enable_tools", "finish"}
CATEGORY_TOOLS = {
    "file": ["create_file", "edit_file", "append_file", "delete_file", "search_files"],
    "code": ["run_bash", "run_python"],
    "web": ["web_search", "web_fetch", "web_search_multi"],
    "memory": ["memory_store", "memory_recall"],
    "mcp": ["mcp_call"],
    "agent": ["spawn_agent"],
}
CAT_KEYWORDS = {
    "code": ("code", "program", "script", "run", "bash", "python", "compile", "function", "module", "fix",
             "refactor", "debug", "implement", "test", "execute", "algorithm", "calculate", "compute", "error",
             "bug", "build", "install", "npm", "pip", "cli", "app", "server", "api", "class", "git", "command",
             "csv", "data", "analy", "plot", "chart", "benchmark", "lint", "deploy"),
    "file": ("create", "write", "generate", "document", "file", "folder", "director", "organize", "organise",
             "rename", "delete", "edit", "save", "report", "markdown", "readme", ".md", ".txt", ".json",
             "note", "draft", "update", "change", "add", "remove"),
    "web": ("search", "look up", "lookup", "research", "web", "online", "internet", "news", "weather",
            "download", "url", "http", "website", "latest", "current", "docs for", "documentation"),
    "memory": ("remember", "recall", "memory", "last time", "previously", "earlier session"),
    "mcp": ("mcp", "external tool", "plugin", "database"),
    "agent": ("subagent", "sub-agent", "sub agent", "delegate", "spawn"),
}


def route_categories(text):
    """Task text -> tool categories. When unsure, load a broad default (more is safer)."""
    low = (text or "").lower()
    cats = {c for c, kws in CAT_KEYWORDS.items() if any(k in low for k in kws)}
    if "code" in cats:
        cats.add("file")
    if not cats - {"agent"}:          # "use a subagent to …" still needs the tools to do the work
        cats |= {"file", "code", "web"}
    return cats


# ---------------- output shape checks ----------------
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>|</?think>", re.I | re.S)


def semantically_empty(text):
    """Empty after stripping think blocks. An unclosed leading <think> (generation cut off
    mid-thought) also counts as empty, otherwise the empty-turn ladder never fires."""
    t = text or ""
    s = t.lstrip()
    if s.lower().startswith("<think>") and not re.search(r"</think>", s, re.I):
        return True
    return not _THINK_BLOCK_RE.sub("", t).strip()


def similar(a, b, thresh=0.75):
    a, b = "".join(a.split()), "".join(b.split())
    if not a or not b:
        return False
    return SequenceMatcher(None, a[:200], b[:200]).ratio() > thresh


class Looping(Exception):
    """Raised from a streaming callback when the model is stuck repeating a passage."""
    def __init__(self, where, snippet, content=""):
        super().__init__(f"{where} loop")
        self.where, self.snippet, self.content = where, snippet, content


def find_repeat(text, min_unit=16, window=1500):
    """The repeated passage if the tail of `text` is one unit repeated back to back, else ''.
    Short units need more repetitions; units of one or two characters (rules, tables) never count."""
    tail = text[-window:]
    n = len(tail)
    for u in range(min_unit, n // 3 + 1):
        reps = 3 if u >= 40 else 5
        if u * reps > n:
            continue
        unit = tail[-u:]
        if len(set(unit)) < 6:
            continue
        if all(tail[n - (k + 1) * u:n - k * u] == unit for k in range(1, reps)):
            return unit.strip()
    return ""


class RepeatWatch:
    """Feed it streamed text; raises Looping once the stream turns into a repeat of itself."""
    def __init__(self, where, every=80):
        self.where, self.every = where, every
        self.buf, self.since = "", 0

    def feed(self, chunk):
        self.buf = (self.buf + chunk)[-3000:]
        self.since += len(chunk)
        if self.since < self.every:
            return
        self.since = 0
        unit = find_repeat(self.buf)
        if unit:
            raise Looping(self.where, unit[:120], self.buf)


# ---------------- text tool-call salvage (format leaks) ----------------
_FUNC_CALL_RE = re.compile(r'^([a-zA-Z_][a-zA-Z0-9_.]*)\s*\((.*)\)$', re.S)
_FUNC_ARG_RE = re.compile(
    r'([a-zA-Z_][a-zA-Z0-9_]*)\s*[=:]\s*'
    r'(<\|"\|>.*?<\|"\|>|"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|-?\d+(?:\.\d+)?|true|false|True|False|null|None)',
    re.S)


def _parse_func_expr(c, known):
    """The WHOLE text is one call expression name(k="v", ...) with scalar args only."""
    m = _FUNC_CALL_RE.match(c.strip())
    if not m:
        return None
    name, argstr = m.group(1), m.group(2).strip()
    if name not in known and "." not in name:
        return None
    args, pos = {}, 0
    for am in _FUNC_ARG_RE.finditer(argstr):
        if am.start() != pos:
            return None
        k, v = am.group(1), am.group(2)
        if v.startswith('<|"|>'):
            v = v[5:-5]
        elif v[:1] in "\"'":
            try:
                v = json.loads(v) if v[0] == '"' else v[1:-1]
            except Exception:
                v = v[1:-1]
        elif v in ("true", "True", "false", "False"):
            v = v.lower() == "true"
        elif v in ("null", "None"):
            v = None
        else:
            v = float(v) if "." in v else int(v)
        args[k] = v
        pos = am.end()
        while pos < len(argstr) and argstr[pos] in ", \t\r\n":
            pos += 1
    if pos != len(argstr):
        return None
    return [(name, args)]


_PY_LITERAL_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\b(None|True|False)\b')
_LOOSE_NAME_KEYS = ("function", "action", "tool", "name")
_ARG_KEYS = ("arguments", "parameters", "args")


def _json_pyish(c):
    """JSON with Python literals (None/True/False outside strings), as lfm2.5 and hermes3 write it."""
    lit = {"None": "null", "True": "true", "False": "false"}
    try:
        return json.loads(_PY_LITERAL_RE.sub(lambda m: lit[m.group(1)] if m.group(1) else m.group(0), c))
    except Exception:
        return None


def _loose_call(it):
    """{"action": "list_dir", "path": "."} / {"function": "read_file", "arguments": {..}}: the name under
    another key, the arguments nested or flat beside it."""
    for k in _LOOSE_NAME_KEYS:
        if isinstance(it.get(k), str):
            nested = next((it[a] for a in _ARG_KEYS if isinstance(it.get(a), dict)), None)
            return it[k], nested if nested is not None else {x: v for x, v in it.items() if x != k}
    return None


def try_parse_tool_calls(content, known, loose=False):
    """Small models often write the tool call as text instead of a real tool_call.
    Accepts: {"name":..,"arguments":..} / {"tool":{..}} / lists of those / ```json fences /
    <tool_call> tags / gemma-style call:name{...} / placeholder-quote name(k:<|"|>v<|"|>) /
    a bare name(k="v") expression. loose also takes the name under "action"/"function"/"tool"
    with flat arguments, and Python literals. Unknown names are rejected."""
    c = (content or "").strip()
    if not c:
        return None
    if not c.startswith(("{", "[")):
        m = re.search(r"<tool_call>\s*(.*?)\s*</tool_call>", c, re.S)
        if m:
            c = m.group(1).strip()
        else:
            m = re.search(r"```(?:json|tool_code|tool|python)?\s*(.*?)\s*```", c, re.S)
            if m and (c.startswith("```") or len(c) - len(m.group(0)) < 80):
                c = m.group(1).strip()
    if c.startswith(("{", "[")):
        try:
            data = json.loads(c)
        except Exception:
            data = _json_pyish(c) if loose else None
        if data is not None:
            calls = []
            for it in (data if isinstance(data, list) else [data]):
                if not isinstance(it, dict):
                    continue
                if "name" in it and ("arguments" in it or "parameters" in it or "args" in it):
                    a = it.get("arguments", it.get("parameters", it.get("args"))) or {}
                    if isinstance(a, str):
                        try:
                            a = json.loads(a)
                        except Exception:
                            a = {}
                    calls.append((it["name"], a))
                elif "function" in it and isinstance(it["function"], dict):
                    fn = it["function"]
                    a = fn.get("arguments") or {}
                    if isinstance(a, str):
                        try:
                            a = json.loads(a)
                        except Exception:
                            a = {}
                    calls.append((fn.get("name", ""), a))
                elif len(it) == 1 and isinstance(next(iter(it.values())), dict):
                    n = next(iter(it))
                    calls.append((n, it[n]))
                elif loose and _loose_call(it):
                    calls.append(_loose_call(it))
            calls = [(n, a) for n, a in calls if n in known or "." in str(n)]
            if calls:
                return calls
    # gemma-style leak: call:name{key:<|"|>v<|"|>,...}  or  call:name{"k": "v"}
    calls = []
    for m in re.finditer(r"call:([a-zA-Z_][\w.]*)\s*(\{.*?\})(?=\s*(?:<|call:|$))", c, re.S):
        name, body = m.group(1), m.group(2)
        if name not in known and "." not in name:
            continue
        try:
            calls.append((name, json.loads(body)))
            continue
        except Exception:
            pass
        args = {}
        for am in re.finditer(r'([a-zA-Z_]\w*)\s*:\s*(<\|"\|>(.*?)<\|"\|>|"((?:[^"\\]|\\.)*)"|([-\w.]+))', body, re.S):
            v = am.group(3) if am.group(3) is not None else (am.group(4) if am.group(4) is not None else am.group(5))
            if v in ("true", "false"):
                v = v == "true"
            args[am.group(1)] = v
        calls.append((name, args))
    if calls:
        return calls
    # placeholder-quote form, round or curly brackets
    for m in re.finditer(r"\b([a-zA-Z_][\w.]*)[\({]\s*([a-zA-Z_]\w*)\s*:\s*<\|\"\|>(.*?)<\|\"\|>\s*[\)}]", c, re.S):
        if m.group(1) in known or "." in m.group(1):
            calls.append((m.group(1), {m.group(2): m.group(3).strip()}))
    if calls:
        return calls
    return _parse_func_expr(c, known)


# ---------------- test guard ----------------
PLUGIN_CRASH = "load_setuptools_entrypoints"
_TEST_ENV_ERR = ("no module named pytest", "command not found", "is not recognized as")


def pytest_hint(output):
    """Exact failure: test id, file:line, the E-line. Small models can't self-debug from a wall of text."""
    lines = output.splitlines()
    hint = []
    for ln in lines:
        if ln.startswith("FAILED ") or ln.startswith("ERROR "):
            hint.append(ln.split(" ", 1)[1].strip()[:160])
            break
    for ln in lines:
        m = re.search(r'File "([^"]+)", line (\d+)', ln) or re.match(r"^([\w./-]+\.py):(\d+):", ln)
        if m:
            hint.append(f"at {os.path.basename(m.group(1))}:{m.group(2)}")
            break
    for ln in lines:
        if ln.startswith("E "):
            hint.append(ln[2:].strip()[:200])
            break
    if hint:
        return " | ".join(hint)
    tail = [ln for ln in lines if ln.strip()][-3:]
    return " / ".join(tail)[:300]


def run_test_guard(workdir, python):
    """('pass'|'fail'|'skip', hint). pytest -> unittest fallback -> skip if no python."""
    def _run(args, env=None):
        return subprocess.run(args, cwd=workdir, capture_output=True, text=True, timeout=300, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL, env=osutil.child_env(env))

    def _broken(pr, out):
        return pr.returncode in (127, 9009) or any(m in out.lower() for m in _TEST_ENV_ERR)
    if not python or not (os.path.isabs(python) or shutil.which(python)):
        return "skip", ""
    try:
        pr = _run([python, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"])
    except subprocess.TimeoutExpired:
        return "fail", "pytest timed out (>300s): an infinite loop or a test waiting for input?"
    except OSError:
        return "skip", ""
    out = (pr.stdout or "") + "\n" + (pr.stderr or "")
    if PLUGIN_CRASH in out:
        # a broken third-party plugin in site-packages crashes pytest before collection:
        # an environment tax, not a test failure. Retry without plugin autoload.
        try:
            pr = _run([python, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
                      env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
        except subprocess.TimeoutExpired:
            return "fail", "pytest timed out (>300s)"
        out = (pr.stdout or "") + "\n" + (pr.stderr or "")
    if _broken(pr, out):
        try:
            pr = _run([python, "-m", "unittest", "discover", "-p", "*test*.py"])
        except subprocess.TimeoutExpired:
            return "fail", "unittest timed out (>300s)"
        out = (pr.stdout or "") + "\n" + (pr.stderr or "")
        if _broken(pr, out):
            return "skip", ""
    if pr.returncode == 5 and "no tests ran" in out:
        return "skip", ""
    return ("pass", "") if pr.returncode == 0 else ("fail", pytest_hint(out))


# ---------------- shotgun-edit convergence guard ----------------
class EditConvergenceGuard:
    """Same file edited successfully N times in a row while the goal does not converge:
    don't disable anything (the edits succeed), inject a strategy change. 2 hints/file max."""

    def __init__(self, threshold=4, max_hints=2):
        self.threshold, self.max_hints = threshold, max_hints
        self.streak, self.last, self.hints = 0, None, {}

    def observe(self, path):
        p = os.path.normpath(str(path or "").lstrip("./"))
        if not p:
            self.streak, self.last = 0, None
            return None
        self.streak = self.streak + 1 if p == self.last else 1
        self.last = p
        if self.streak < self.threshold or self.hints.get(p, 0) >= self.max_hints:
            return None
        self.hints[p] = self.hints.get(p, 0) + 1
        if self.hints[p] == 1:
            return (f"⚠️ You have edited {p} successfully {self.streak} times in a row and the goal is still not "
                    f"met. Stop guess-tweaking: read the full failure output to locate the root cause (add a "
                    f"temporary print of intermediate values if needed). If the approach is wrong, restore the "
                    f"backup from .finch/backups/ or rewrite that part with create_file.")
        return (f"⚠️ Still circling on {p} (edit #{self.streak}). Rule: before the next edit, state your root-cause "
                f"hypothesis and verify it with one run_bash. No edit_file before reading a new error. If two "
                f"hypotheses fail, restore the backup and change the implementation approach.")


# ---------------- time budget ----------------
BUDGET_MARKS = (
    (0.50, "⏱ Time budget: ~50% used, ~{m} min left. Check remaining work: finish the core deliverable "
           "first; if something keeps failing, note it and move on instead of grinding."),
    (0.75, "⏱ Time budget: ~75% used, ~{m} min left. Stop exploring: complete the required deliverables "
           "and leave time to verify (run tests / check files exist)."),
    (0.90, "⏱ Time budget: ~90% used (~{m} min left). Wrap up NOW: claim only files that really exist, "
           "then call finish. Do not start new subtasks."),
)


def budget_nudge(budget_sec, elapsed, fired):
    if not budget_sec or budget_sec <= 0:
        return None
    ratio = elapsed / budget_sec
    hit = None
    for frac, tmpl in BUDGET_MARKS:
        if ratio >= frac and frac not in fired:
            hit = tmpl
    if hit is None:
        return None
    for frac, _ in BUDGET_MARKS:
        if ratio >= frac:
            fired.add(frac)
    return hit.format(m=max(0, round((budget_sec - elapsed) / 60)))


_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
              "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
              "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "half an": 0.5, "an": 1, "a": 1}
_NUM = r"(\d+(?:\.\d+)?|" + "|".join(sorted(map(re.escape, _NUM_WORDS), key=len, reverse=True)) + ")"
_UNIT = r"(hours?|hrs?|h|minutes?|mins?|m)\b"
_TB_RE = re.compile(r"\b(?:time\s*box(?:ed)?|time\s*limit(?:ed)?|time\s*budget|within|in under|under|"
                    r"no more than|at most|max(?:imum)?|budget(?: of)?|you have)\s*(?:of|:|to|is)?\s*"
                    + _NUM + r"\s*-?\s*" + _UNIT, re.I)


def parse_duration(s):
    """'40' / '40m' / '1.5h' / '90 minutes' / 'half an hour' -> minutes (float) or None."""
    s = (s or "").strip().lower()
    if not s:
        return None
    if s == "half an hour":
        return 30.0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|m|min|mins|minutes?)?", s)
    if not m:
        return None
    v = float(m.group(1))
    return v * 60 if (m.group(2) or "m").startswith("h") else v


def parse_time_budget(text):
    """'timebox 40 minutes' / 'within 1.5 hours' / 'you have half an hour' -> minutes."""
    low = (text or "").lower()
    if re.search(r"\bhalf an hour\b", low) and re.search(r"\b(within|in|under|have|timebox|limit)\s+half an hour", low):
        return 30.0
    m = _TB_RE.search(low)
    if not m:
        return None
    n = m.group(1)
    v = _NUM_WORDS.get(n) if n in _NUM_WORDS else float(n)
    unit = m.group(2)
    mins = v * 60 if unit.startswith("h") else v
    return mins if 1 <= mins <= 24 * 60 else None


# ---------------- context estimate ----------------
def estimate_tokens(text):
    if not text:
        return 0
    s = str(text)
    cjk = sum(1 for ch in s if "一" <= ch <= "鿿")
    return int(cjk * 2 + (len(s) - cjk) / 3) + 1   # overestimate on purpose: early compaction is cheap


def estimate_messages(messages, tools=None):
    total = 0
    for m in messages or []:
        total += estimate_tokens(m.get("content", ""))
        total += sum(len(b) for b in (m.get("images") or [])) // 4
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            total += estimate_tokens(fn.get("name", "")) + estimate_tokens(json.dumps(fn.get("arguments", "")))
    for t in tools or []:
        total += estimate_tokens(json.dumps(t))
    return total

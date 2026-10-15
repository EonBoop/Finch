"""SOUL.md: switchable persona / voice files injected at the top of every system prompt
(task and chat mode).

A soul is a markdown file with optional frontmatter:

    ---
    description: one line shown in the picker
    ---
    You are ... (the instructions)

Lookup order (first match wins for a given name):
  1. <workdir>/.finch/souls/<name>.md      project souls
  2. ~/.finch/souls/<name>.md              your souls
  3. finch/builtin_souls/<name>.md         built-in souls
plus two special files that appear as souls when they exist:
  <workdir>/.finch/SOUL.md  -> "project"   and   ~/.finch/SOUL.md -> "personal"

The harness rules still follow the soul: a soul changes voice and priorities, not the
tool protocol the tricks depend on. Every character is sent on every request, so keep
souls short (the picker shows the token cost).
"""
import os
import re

from . import config, osutil

PKG_SOULS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "builtin_souls")
MAX_CHARS = 4000
TEMPLATE = """---
description: {desc}
---
You are Finch, a local AI agent. <Describe the voice and priorities you want here.>

- Tone: <e.g. direct, warm, formal, playful>
- Replies: <e.g. match length to the question; short reports of finished work>
- Priorities: <e.g. correctness over speed; ask before big changes>
"""


def _user_dir():
    return os.path.join(config.FINCH_HOME, "souls")


def _bases(workdir):
    return [("project", os.path.join(workdir, ".finch", "souls")),
            ("user", _user_dir()),
            ("built-in", PKG_SOULS)]


def _special(workdir):
    return [("project", "project", os.path.join(workdir, ".finch", "SOUL.md")),
            ("personal", "user", os.path.join(config.FINCH_HOME, "SOUL.md"))]


def parse(raw):
    """-> (description, body) with frontmatter stripped."""
    desc, body = "", raw
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", raw, re.S)
    if m:
        body = raw[m.end():]
        d = re.search(r"description:\s*(.+)", m.group(1))
        if d:
            desc = d.group(1).strip().strip("\"'")
    if not desc:
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        desc = first[:90]
    return desc, body.strip()


def list_souls(workdir):
    """[{name, path, scope, desc, chars, tokens}] (first match per name wins)."""
    seen = {}
    for name, scope, path in _special(workdir):
        if os.path.isfile(path):
            seen[name] = (scope, path)
    for scope, base in _bases(workdir):
        if os.path.isdir(base):
            for f in sorted(os.listdir(base)):
                if f.endswith(".md"):
                    seen.setdefault(f[:-3], (scope, os.path.join(base, f)))
    out = []
    for name, (scope, path) in seen.items():
        try:
            with open(path, encoding="utf-8") as fh:
                desc, body = parse(fh.read())
        except Exception:
            continue
        out.append({"name": name, "path": path, "scope": scope, "desc": desc,
                    "chars": len(body), "tokens": len(body) // 3 + 1})
    order = {"project": 0, "user": 1, "built-in": 2}
    return sorted(out, key=lambda x: (order[x["scope"]], x["name"]))


def find(name, workdir):
    return next((s for s in list_souls(workdir) if s["name"] == name), None)


def load(name, workdir):
    """Soul body for injection ('' if none / missing). Capped at MAX_CHARS."""
    if not name:
        return ""
    s = find(name, workdir)
    if not s:
        return ""
    with open(s["path"], encoding="utf-8") as fh:
        _, body = parse(fh.read())
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + "\n[soul truncated at 4000 characters]"
    return body


def create(name, workdir=None, project=False, desc="my custom soul"):
    """Create a soul from the template. Returns its path (existing files are left alone)."""
    name = re.sub(r"[^\w.-]+", "-", name.strip()).strip("-") or "custom"
    base = os.path.join(workdir, ".finch", "souls") if project and workdir else _user_dir()
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, name + ".md")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(TEMPLATE.format(desc=desc))
    return name, path


def editor_cmd():
    """argv of the first usable editor ($VISUAL, $EDITOR, then the platform default), or None."""
    import shutil
    for c in (os.environ.get("VISUAL"), os.environ.get("EDITOR"), *osutil.EDITORS):
        argv = osutil.split_cmd(c)
        if argv and (os.path.isfile(argv[0]) or shutil.which(argv[0])):
            return argv
    return None

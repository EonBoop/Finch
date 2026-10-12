"""Terminal UI in the style of Claude Code: streamed markdown, ● tool lines with ⎿ results,
live spinner with elapsed time and token count, permission prompts, a todo checklist,
slash commands, @file mentions, and a status toolbar."""
import difflib
import json
import os
import time

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from . import themes

THEME = themes.rich_theme(themes.DEFAULT)

TOOL_LABEL = {
    "create_file": "Write", "read_file": "Read", "edit_file": "Update", "append_file": "Append",
    "list_dir": "List", "run_bash": "Bash", "run_python": "Python", "search_files": "Search",
    "web_search": "WebSearch", "web_fetch": "Fetch", "web_search_multi": "WebSearch",
    "todo": "Update Todos", "finish": "Finish", "skills": "Skill", "enable_tools": "EnableTools",
    "memory_store": "Remember", "memory_recall": "Recall", "batch_tools": "Batch", "mcp_call": "MCP",
    "delete_file": "Delete",
}
SPIN_WORDS = ("Thinking", "Pondering", "Working", "Reasoning", "Composing", "Considering", "Brewing",
              "Mulling", "Crafting", "Noodling")


def short(s, n=80):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_arg_summary(name, a):
    if name in ("create_file", "read_file", "edit_file", "append_file", "delete_file"):
        p = a.get("path", "")
        if name == "read_file" and (a.get("start_line") or a.get("end_line")):
            p += f":{a.get('start_line') or 1}-{a.get('end_line') or ''}"
        return p
    if name == "list_dir":
        return a.get("path", ".")
    if name == "run_bash":
        return short(a.get("command", ""), 90)
    if name == "run_python":
        first = str(a.get("code", "")).strip().splitlines()
        return short(first[0] if first else "", 70) + (f" (+{len(first) - 1} lines)" if len(first) > 1 else "")
    if name == "search_files":
        return f'"{a.get("pattern", "")}" in {a.get("path", ".")}'
    if name in ("web_search",):
        return short(a.get("query", ""), 80)
    if name == "web_search_multi":
        return short("; ".join(map(str, a.get("queries") or [])), 80)
    if name == "web_fetch":
        return short(a.get("url", ""), 80)
    if name == "skills":
        return f"{a.get('action', 'list')} {a.get('name', '')}".strip()
    if name == "enable_tools":
        return ", ".join(map(str, a.get("tools") or []))
    if name == "todo":
        return ""
    if name == "finish":
        return ""
    return short(json.dumps(a, ensure_ascii=False), 80)


class TerminalUI:
    interactive = True

    def __init__(self, console=None, verbose=False, theme=themes.DEFAULT):
        self.c = console or Console(theme=THEME, highlight=False)
        self._theme_pushed = False
        self.set_theme(theme)
        self.verbose = verbose
        self.live = None
        self._reset_stream()
        self.ctx_used = 0
        self.ctx_budget = 0
        self.turn_t0 = time.time()
        self.last_streamed = ""
        self.confirm_fn = None

    def set_theme(self, name):
        if self._theme_pushed:
            self.c.pop_theme()
        self.c.push_theme(themes.rich_theme(name))
        self._theme_pushed = True
        self.theme = name if name in themes.THEMES else themes.DEFAULT

    def g(self, key):
        """Glyph / branding for the active theme (bullet, result, logo, faces, verbs…)."""
        return themes.glyph(getattr(self, "theme", themes.DEFAULT), key)

    # ---------------- helpers ----------------
    def _reset_stream(self):
        self.content, self.thinking = [], []
        self.t0 = time.time()
        self.think_t = None
        verbs = self.g("verbs")
        self.word = verbs[int(time.time()) % len(verbs)]
        self.word = self.word[:1].upper() + self.word[1:]

    def _stop_live(self):
        if self.live is not None:
            try:
                self.live.stop()
            except Exception:
                pass
            self.live = None

    def abort(self):
        self._stop_live()

    def bullet(self, renderable, color="bullet"):
        g = Table.grid(padding=(0, 1))
        g.add_column(width=1, no_wrap=True)
        g.add_column(ratio=1)
        g.add_row(Text(self.g("bullet"), style=color), renderable)
        self.c.print(g)

    def result(self, renderable):
        g = Table.grid(padding=(0, 1))
        g.add_column(width=4, no_wrap=True)
        g.add_column(ratio=1)
        g.add_row(Text(f"  {self.g('result')} ", style="dim"), renderable)
        self.c.print(g)

    # ---------------- streaming ----------------
    def _render_live(self):
        elapsed = time.time() - self.t0
        toks = (sum(map(len, self.content)) + sum(map(len, self.thinking))) // 4
        parts = []
        h = max(6, self.c.size.height - 6)
        if self.thinking and not self.content:
            tail = "".join(self.thinking)[-1200:].strip().splitlines()[-4:]
            parts.append(Text("\n".join(short(t, self.c.size.width - 6) for t in tail), style="think"))
        if self.content:
            lines = "".join(self.content).splitlines()[-(h - 2):]
            parts.append(Text("\n".join(lines)))
        faces = self.g("faces") if self.thinking else (self.g("waiting") or self.g("faces"))
        face = (faces[int(elapsed / 1.5) % len(faces)] + " ") if faces else ""
        status = Text.assemble((f"{face}{self.word}… ", "accent"),
                               (f"({elapsed:.0f}s · ↓ {toks} tokens · ctrl+c to interrupt)", "dim"))
        parts.append(Spinner("dots", text=status, style="accent"))
        return Group(*parts)

    def model_start(self, i):
        self._stop_live()
        self._reset_stream()
        self.live = Live(self._render_live(), console=self.c, refresh_per_second=10, transient=True,
                         get_renderable=self._render_live, vertical_overflow="crop")
        self.live.start()

    def token(self, t):
        if self.think_t is None and self.thinking:
            self.think_t = time.time() - self.t0
        self.content.append(t)

    def think(self, t):
        self.thinking.append(t)

    def model_end(self, msg, stats):
        self._stop_live()
        th = "".join(self.thinking).strip()
        if th:
            dur = self.think_t if self.think_t is not None else time.time() - self.t0
            if self.verbose:
                self.c.print(Text(f"{self.g('logo')} Thinking ({dur:.1f}s)", style="think"))
                self.c.print(Text(th, style="think"), soft_wrap=True)
            else:
                self.c.print(Text(f"{self.g('logo')} Thought for {dur:.1f}s", style="think"))
        if stats.get("error") or stats.get("interrupted"):
            txt = "".join(self.content).strip()
            if txt:
                self.bullet(Markdown(txt))
            return
        content = (msg.get("content") or "").strip()
        if content and not stats.get("salvaged"):
            self.bullet(Markdown(content))
            self.last_streamed = content
        elif not content and not stats.get("has_tools") and not th:
            self.c.print(Text("  (empty response)", style="dim"))
        if stats.get("done_reason") == "length":
            self.c.print(Text("  ⚠ output hit the token cap", style="warn"))

    # ---------------- tools ----------------
    def tool_start(self, name, args):
        self._stop_live()
        label = TOOL_LABEL.get(name, name)
        summ = tool_arg_summary(name, args)
        line = Text.assemble((label, "tool"), (f"({summ})" if summ else "", ""))
        self._tool_line = line
        self._tool_t0 = time.time()
        if name not in ("todo", "finish", "enable_tools", "skills"):
            self.live = Live(Spinner("dots", text=Text.assemble(line, ("  running…", "dim")), style="accent"),
                             console=self.c, refresh_per_second=10, transient=True)
            self.live.start()

    def tool_end(self, name, args, result, error):
        self._stop_live()
        res = str(result)
        blocked = res.startswith(("[safety", "[blocked", "[clobber", "[tool ", "[finish rejected")) and "disabled" not in res[:6]
        if name == "finish":
            if "rejected" in res:
                self.bullet(Text.assemble(("Finish", "tool"), ("  rejected by harness gate", "warn")), "warn")
            return
        if name == "todo":
            return
        color = "err" if error and not blocked else ("warn" if blocked else "ok")
        dur = time.time() - getattr(self, "_tool_t0", time.time())
        line = self._tool_line.copy()
        if dur > 3:
            line.append(f"  {dur:.0f}s", style="dim")
        self.bullet(line, color)
        self.result(self._result_view(name, args, res, error))

    def _result_view(self, name, args, res, error):
        lines = res.splitlines() or [""]
        if res.startswith(("[safety", "[blocked", "[clobber", "[tool ", "[unknown", "[edit failed",
                           "[not found", "[crawl", "[offline")) and name not in ("read_file",):
            return Text(short(res, 400), style="warn" if not error else "err")
        if name == "create_file":
            n = str(args.get("content", "")).count("\n") + 1
            t = Text.assemble(("Wrote ", "dim"), (f"{n} lines", "bold"), (f" to {args.get('path', '')}", "dim"))
            if "backed up" in res:
                t.append("  (previous version backed up)", style="dim")
            if "auto-repaired" in res:
                t.append("  (auto-repaired escaped newlines)", style="warn")
            if self.verbose:
                prev = str(args.get("content", "")).splitlines()[:12]
                t.append("\n" + "\n".join(prev), style="output")
            return t
        if name == "edit_file":
            if not res.startswith("[edited"):
                return Text(short(res, 300), style="err")
            return self._diff(str(args.get("old", "")), str(args.get("new", "")), res)
        if name == "read_file":
            if res.startswith("[image"):
                return Text(short(res, 200), style="dim")
            n = len([ln for ln in lines if not ln.startswith("[")])
            extra = ""
            if "[crawl-guard" in res:
                extra = "  · crawl-guard throttling"
            elif "truncated" in lines[-1]:
                extra = "  · truncated"
            return Text(f"Read {n} lines{extra}", style="dim")
        if name == "list_dir":
            return Text(f"{len(lines)} entries" + ("" if not self.verbose else "\n" + "\n".join(lines[:40])), style="dim")
        if name in ("run_bash", "run_python"):
            return self._bash_view(res, error)
        if name in ("web_search", "web_search_multi"):
            n = sum(1 for ln in lines if ln.startswith("- "))
            return Text(f"{n} results" if n else short(res, 200), style="dim")
        if name == "web_fetch":
            return Text(short(lines[-1] if lines else res, 200), style="dim")
        if name == "search_files":
            return Text(f"{len(lines)} matches" if res != "(no matches)" else "no matches", style="dim")
        limit = 400 if not self.verbose else 4000
        return Text(short(res, limit) if not self.verbose else res[:limit], style="err" if error else "dim")

    def _diff(self, old, new, res):
        t = Text()
        ol, nl = old.splitlines(), new.splitlines()
        added = sum(1 for _ in nl) - sum(1 for _ in ol)
        t.append(f"Updated with {len(nl)} line(s) replacing {len(ol)}", style="dim")
        if "occurs" in res:
            t.append(" (first of several matches)", style="warn")
        shown = 0
        for d in difflib.ndiff(ol, nl):
            if d.startswith(("- ", "+ ")) and shown < (40 if self.verbose else 10):
                style = "diff.del" if d[0] == "-" else "diff.add"
                t.append("\n" + d[0] + " " + short(d[2:], self.c.size.width - 12), style=style)
                shown += 1
        _ = added
        return t

    def _bash_view(self, res, error):
        lines = res.splitlines()
        head = lines[0] if lines else ""
        body = []
        sect = None
        for ln in lines[1:]:
            if ln in ("STDOUT:", "STDERR:", "STDOUT (tail):", "STDERR (tail):"):
                sect = ln
                continue
            if ln.strip():
                body.append((sect, ln))
        n = 3 if not self.verbose else 60
        t = Text()
        if not body:
            t.append(f"{head} (no output)", style="dim" if not error else "err")
            return t
        # success: the head is informative; failure: the error is at the tail
        shown = body[:n] if not error else body[-n:]
        if error and len(body) > n:
            t.append(f"… +{len(body) - n} lines\n", style="dim")
        for sect, ln in shown:
            t.append(short(ln, self.c.size.width - 10) + "\n",
                     style=("err" if (sect or "").startswith("STDERR") and error else "output"))
        if not error and len(body) > n:
            t.append(f"… +{len(body) - n} lines", style="dim")
        else:
            t.rstrip()
        if error:
            t.append(f"  {head}", style="err")
        return t

    def todo(self, items):
        self._stop_live()
        if not items:
            return
        self.bullet(Text("Update Todos", style="tool"), "ok")
        t = Text()
        first_open = next((i for i, x in enumerate(items) if not x.get("done")), None)
        for i, x in enumerate(items):
            if x.get("done"):
                t.append("☒ " + x.get("item", ""), style="done")
            elif i == first_open:
                t.append("☐ " + x.get("item", ""), style="bold")
            else:
                t.append("☐ " + x.get("item", ""))
            if i < len(items) - 1:
                t.append("\n")
        self.result(t)

    # ---------------- misc events ----------------
    def turn_start(self, mode, info):
        self.turn_t0 = time.time()
        bits = []
        if mode == "chat":
            bits.append("chat mode · answer once, read-only tools")
        else:
            bits.append("task mode · tools: " + ", ".join(info.get("categories", [])))
            if info.get("small_model"):
                bits.append("small-model protocol (edit_file off, slim outputs)")
            if info.get("timebox"):
                bits.append(f"timebox {info['timebox']:.0f} min")
        self.c.print(Text("  " + " · ".join(bits), style="dim"))

    def notice(self, text, level="warn"):
        self._stop_live()
        style = {"warn": "warn", "error": "err", "info": "dim", "ok": "ok"}.get(level, "dim")
        icon = {"warn": "⚑", "error": "✗", "info": "↳", "ok": "✓"}.get(level, "·")
        text = text.lstrip("⚠️⏱ \ufe0f").split("\n")[0]
        if len(text) > 160:
            text = text[:text.find(":", 0, 160) + 1 or 157].rstrip(":") + "…"
        self.c.print(Text(f"  {icon} {text}", style=style))

    def status(self, used, budget):
        self.ctx_used, self.ctx_budget = used, budget

    def final(self, text):
        self._stop_live()
        if text and text.strip() and text.strip() != self.last_streamed.strip():
            self.bullet(Markdown(text.strip()))
        dur = time.time() - self.turn_t0
        self.c.print(Text(f"  {self.g('logo')} done in {dur:.0f}s", style="dim"))

    # ---------------- permission prompt ----------------
    def confirm(self, tool, target, action):
        self._stop_live()
        body = Text.assemble((f"{TOOL_LABEL.get(tool, tool)}", "bold"), "  wants to ", (action, "warn"), "\n\n",
                             (str(target), ""), "\n\n",
                             ("Do you want to proceed?\n", ""),
                             ("  1. Yes\n", ""),
                             (f"  2. Yes, and allow {TOOL_LABEL.get(tool, tool)} for the rest of this turn\n", ""),
                             ("  3. No, tell Finch what to do differently", ""))
        self.c.print(Panel(body, border_style="warn", title="permission", title_align="left"))
        try:
            from prompt_toolkit import prompt
            ans = prompt("  choose [1/2/3] › ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "3"
        if ans in ("1", "y", "yes", ""):
            return "yes"
        if ans in ("2", "a", "all"):
            return "all"
        return "no"


class PlainUI(TerminalUI):
    """Headless (-p) output: same renderers, no live spinners, no prompts."""
    interactive = False

    def model_start(self, i):
        self._reset_stream()

    def tool_start(self, name, args):
        label = TOOL_LABEL.get(name, name)
        summ = tool_arg_summary(name, args)
        self._tool_line = Text.assemble((label, "tool"), (f"({summ})" if summ else "", ""))
        self._tool_t0 = time.time()

    def confirm(self, tool, target, action):
        return "no"


def banner(c, settings, model, workdir, version, backend_ver):
    title = Text.assemble((themes.glyph(settings.theme, "logo") + " ", "accent"), ("Welcome to ", "bold"), ("Finch", "bold accent"),
                          (f"  v{version}", "dim"))
    rows = Text.assemble(
        ("  /help for help, /model to switch models, ctrl+c to interrupt\n\n", "dim"),
        ("  cwd:     ", "dim"), (workdir.replace(os.path.expanduser("~"), "~"), ""), "\n",
        ("  model:   ", "dim"), (model or "(none — use /model)", "bold"), "\n",
        ("  engine:  ", "dim"), (f"{settings.backend} {backend_ver} @ {settings.host if settings.backend == 'ollama' else settings.openai_base_url}", ""), "\n",
        ("  soul:    ", "dim"), (settings.soul or "none", ""), "\n",
        ("  context: ", "dim"), (f"{settings.num_ctx // 1024}K · output cap {settings.num_predict} · think "
                                 f"{ {None: 'auto', True: 'on', False: 'off'}[settings.think] }", ""),
        *((("\n  ", ""), ("offline mode: web tools are not assembled into the prompt", "warn")) if settings.offline else ()),
    )
    c.print(Panel(Group(title, Text(""), rows), border_style="border", expand=False, padding=(0, 2)))

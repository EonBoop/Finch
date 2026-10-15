"""Entry point: interactive REPL (default) or headless `finch -p "task"`."""
import argparse
import base64
import os
import re
import shlex
import subprocess
import sys
import threading
import time

from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __version__, config, heuristics as H, osutil, scheduler as SCH, souls, themes, tools as T, tricks as TR
from .agent import Agent, Session
from .llm import make_backend
from .mcp import MCPManager
from .safety import Gate
from .ui import THEME, PlainUI, TerminalUI, banner

COMMANDS = {
    "/help": "show this help",
    "/settings": "settings menu: model, context, thinking, tricks, tools, skills, theme",
    "/theme": "pick a theme (live preview), or /theme dracula",
    "/soul": "switch persona (interactive) · /soul <name> · off · list · show · new <name> · edit",
    "/profile": "settings profiles (interactive) · /profile <name> loads · save <name> · list · rm <name>",
    "/tricks": "toggle harness tricks (interactive), /tricks list, or /tricks crawl_guard off",
    "/schedule": "scheduled jobs (interactive) · /schedule <when> <task> · list · new · run · pause · resume · rm · log",
    "/model": "list models or switch: /model gemma4:12b",
    "/think": "thinking: /think on | off | auto",
    "/ctx": "context window: /ctx 65536",
    "/mode": "force routing: /mode auto | task | chat",
    "/timebox": "time budget for tasks: /timebox 40m | 1.5h | off",
    "/todo": "show the current plan",
    "/undo": "roll back the last file change made by the agent",
    "/compact": "summarise the conversation now",
    "/clear": "start a new session",
    "/sessions": "list saved sessions",
    "/resume": "resume a session: /resume [number|id]",
    "/cd": "change working directory: /cd path",
    "/tools": "toggle tools (interactive), /tools list, or /tools run_bash off",
    "/skills": "toggle skills (interactive), /skills list, or /skills bug_fix off",
    "/memory": "show long-term memory",
    "/offline": "toggle offline mode (web tools not assembled)",
    "/unsafe": "toggle the behaviour-tiering safety ring (ring 2)",
    "/verbose": "toggle full thinking / tool output",
    "/status": "show settings and context usage",
    "/init": "create FINCH.md project notes (injected into the system prompt)",
    "/exit": "quit",
}

IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def expand_mentions(text, workdir):
    """@path mentions: text files are inlined, images are attached."""
    images, blocks = [], []
    for m in re.finditer(r"(?<![\w/])@([\w./~\-]+)", text):
        p = os.path.expanduser(m.group(1))
        real = p if os.path.isabs(p) else os.path.join(workdir, p)
        if not os.path.isfile(real):
            continue
        if real.lower().endswith(IMG_EXTS):
            with open(real, "rb") as f:
                images.append(base64.b64encode(f.read()).decode())
        else:
            try:
                body = T.read_text(real)
            except Exception:
                continue
            if len(body) > 20000:
                body = body[:20000] + "\n...(truncated; use read_file for the rest)"
            blocks.append(f'<file path="{m.group(1)}">\n{body}\n</file>')
    if blocks:
        text += "\n\n" + "\n\n".join(blocks)
    return text, images


class App:
    def __init__(self, args):
        config.ensure_dirs()
        self.s = config.load_settings()
        if args.profile:
            try:
                config.apply_profile(self.s, config.load_profile(args.profile))
            except ValueError as e:
                sys.exit(f"finch: {e}")
        if args.host:
            self.s.host = config.normalize_host(args.host)
        if args.model:
            self.s.model = args.model
        if args.ctx:
            self.s.num_ctx = args.ctx
        if args.think:
            self.s.think = config._tri(args.think)
        if args.offline:
            self.s.offline = True
        if args.verbose:
            self.s.verbose = True
        if args.theme:
            self.s.theme = args.theme
        if args.soul is not None:
            self.s.soul = "" if args.soul in ("none", "off", "") else args.soul
        self.headless = bool(args.print)
        if self.headless and not args.yes:
            self.s.unattended = True
        self.console = Console(theme=THEME, highlight=False)
        ui_cls = PlainUI if self.headless else TerminalUI
        self.ui = ui_cls(self.console, self.s.verbose, theme=self.s.theme)
        workdir = os.path.abspath(os.path.expanduser(args.cwd or os.getcwd()))
        self.backend = make_backend(self.s)
        self.gate = Gate(self.s, confirm=(lambda *a: "yes") if args.yes else self.ui.confirm)
        self.mcp = MCPManager(self.s)
        self.tb = T.ToolBox(self.s, self.gate, workdir, mcp=self.mcp)
        self.agent = Agent(self.s, self.backend, self.tb, self.gate, self.ui, self.mcp)
        if args.timebox:
            self.agent.timebox_min = H.parse_duration(args.timebox)
        self._sched_stop = self._sched_wake = None
        self._sched_seen, self._sched_cache = {}, (0, [])
        if args.resume:
            self.resume(args.resume, quiet=self.headless)
        else:
            self.agent.session = Session(workdir, self.s.model)

    # ---------------- model selection ----------------
    def ensure_model(self):
        try:
            models = self.backend.list_models()
        except Exception as e:
            self.ui.notice(f"cannot reach the model server at {self.backend.host}: {e}", "error")
            return False
        if self.s.model and self.s.model in models:
            self.agent.session.model = self.s.model
            return True
        if self.s.model and any(m.split(":")[0] == self.s.model for m in models):
            self.s.model = next(m for m in models if m.split(":")[0] == self.s.model)
            self.agent.session.model = self.s.model
            return True
        if self.s.model:
            self.ui.notice(f"model '{self.s.model}' is not installed", "warn")
        if self.headless:
            return False
        return self.pick_model(models)

    def pick_model(self, models):
        if not models:
            self.ui.notice("no models installed. Pull one, e.g. `ollama pull gemma4:12b`", "error")
            return False
        t = Table.grid(padding=(0, 2))
        for i, m in enumerate(models, 1):
            info = self.backend.show(m) if hasattr(self.backend, "show") else {}
            caps = ", ".join(c for c in (info.get("capabilities") or []) if c in ("tools", "thinking", "vision"))
            mark = "●" if m == self.s.model else " "
            t.add_row(Text(f"{mark} {i}.", style="accent"), Text(m, style="bold"),
                      Text(info.get("parameter_size", ""), style="dim"), Text(caps, style="dim"))
        self.console.print(t)
        try:
            from prompt_toolkit import prompt
            ans = prompt("  select model › ").strip()
        except (EOFError, KeyboardInterrupt):
            return False
        if ans.isdigit() and 1 <= int(ans) <= len(models):
            ans = models[int(ans) - 1]
        if ans not in models:
            self.ui.notice("no model selected", "warn")
            return False
        self.set_model(ans)
        return True

    def set_model(self, m, quiet=False):
        self.s.model = m
        self.agent.session.model = m
        info = self.backend.show(m) if hasattr(self.backend, "show") else {}
        config.save_settings(self.s)
        if quiet:
            return
        if "tools" not in (info.get("capabilities") or ["tools"]):
            self.ui.notice(f"{m} does not advertise tool support; Finch will salvage text-format calls", "warn")
        self.ui.notice(f"model set to {m}", "ok")

    def load_profile(self, name):
        """Apply a saved profile live and make it the current config. Raises ValueError."""
        s = self.s
        data = config.load_profile(name)
        conn = (s.host, s.backend, s.openai_base_url, s.openai_api_key)
        config.apply_profile(s, data)
        if (s.host, s.backend, s.openai_base_url, s.openai_api_key) != conn:
            self.backend = self.agent.llm = make_backend(s)
        if not self.apply_theme(s.theme):
            self.apply_theme(themes.DEFAULT)
        self.ui.verbose = s.verbose
        self.agent.session.model = s.model
        config.save_settings(s)
        return f"profile '{name}' loaded · {s.model or 'no model'} · ctx {s.num_ctx // 1024}K · theme {s.theme}"

    def profile_command(self, arg):
        ui = self.ui
        a = arg.split()
        try:
            if not a:
                self.open_settings(tab="Profiles")
            elif a[0] in ("list", "ls") and len(a) == 1:
                self.show_profiles()
            elif a[0] == "save" and len(a) == 2:
                path = config.save_profile(self.s, a[1])
                ui.notice(f"saved profile '{a[1]}'", "ok")
                self.console.print(Text(f"  {path}", style="dim"))
            elif (a[0] == "load" and len(a) == 2) or (len(a) == 1 and a[0] not in ("save", "load", "rm", "delete")):
                ui.notice(self.load_profile(a[-1]), "ok")
            elif a[0] in ("rm", "delete") and len(a) == 2:
                ok = config.delete_profile(a[1])
                ui.notice(f"deleted profile '{a[1]}'" if ok else f"no profile named '{a[1]}'", "ok" if ok else "error")
            else:
                ui.notice("usage: /profile | /profile <name> | save <name> | load <name> | list | rm <name>", "error")
        except ValueError as e:
            ui.notice(str(e), "error")

    def show_profiles(self):
        profs = config.list_profiles()
        if not profs:
            self.console.print(Text(f"no profiles yet: /profile save <name> stores the current settings "
                                    f"({config.PROFILES_DIR})", style="dim"))
            return
        t = Table.grid(padding=(0, 2))
        for p in profs:
            diff = config.profile_diff(self.s, p["data"])
            d = p["data"]
            t.add_row(Text(("  ● " if not diff else "    ") + p["name"], style="ok" if not diff else "bold"),
                      Text(f"{d.get('model') or '-'} · ctx {int(d.get('num_ctx') or 0) // 1024}K · "
                           f"theme {d.get('theme', '-')} · soul {d.get('soul') or 'none'}", style="dim"),
                      Text("current" if not diff else "differs: " + ", ".join(diff[:5])
                           + (" …" if len(diff) > 5 else ""), style="dim"))
        self.console.print(t)
        self.console.print(Text("load: /profile <name> · save: /profile save <name> · or use /settings", style="dim"))

    def apply_theme(self, name):
        if name not in themes.THEMES:
            return False
        self.s.theme = name
        self.ui.set_theme(name)
        return True

    def soul_command(self, arg):
        from .settings_menu import edit_soul
        s, ui = self.s, self.ui
        wd = self.tb.workdir
        a = arg.split(maxsplit=1)
        if not a:
            self.open_settings(tab="Soul")
            return
        sub = a[0].lower()
        if sub == "list":
            for sl in souls.list_souls(wd):
                mark = "●" if sl["name"] == s.soul else " "
                self.console.print(Text.assemble((f" {mark} ", "accent"), (sl["name"].ljust(14), "bold"),
                                                 (f"{sl['scope']:<9}", "dim"), (f"~{sl['tokens']:>4} tok  ", "dim"),
                                                 (sl["desc"], "")))
            self.console.print(Text(f"active: {s.soul or 'none'} · your souls live in "
                                    f"{os.path.join(config.FINCH_HOME, 'souls')}", style="dim"))
        elif sub in ("off", "none"):
            s.soul = ""
            config.save_settings(s)
            ui.notice("soul off", "ok")
        elif sub == "show":
            body = souls.load(s.soul, wd)
            self.console.print(Text(body or "(no soul active)", style="output"))
        elif sub == "new":
            if len(a) < 2:
                ui.notice("usage: /soul new <name>", "error")
                return
            name, path = souls.create(a[1])
            s.soul = name
            config.save_settings(s)
            ui.notice(edit_soul(self, name) + f" · soul set to {name}", "ok")
        elif sub == "edit":
            name = a[1] if len(a) > 1 else s.soul
            if not name:
                ui.notice("no active soul. /soul <name> first, or /soul edit <name>", "error")
                return
            ui.notice(edit_soul(self, name), "ok")
        else:
            info = souls.find(a[0], wd)
            if not info:
                ui.notice(f"no soul '{a[0]}'. /soul list shows them; /soul new {a[0]} creates it", "error")
                return
            s.soul = info["name"]
            config.save_settings(s)
            ui.notice(f"soul: {info['name']} ({info['desc']}) · ~{info['tokens']} tokens per request", "ok")

    def show_tricks(self):
        t = Table.grid(padding=(0, 2))
        for g in TR.groups():
            t.add_row(Text(g, style="accent"), Text(""), Text(""))
            for key, grp, label, desc in TR.TRICKS:
                if grp == g:
                    on = self.s.on(key)
                    t.add_row(Text(("  ● " if on else "  ○ ") + key, style="ok" if on else "err"),
                              Text(label, style="bold"), Text(desc, style="dim"))
        self.console.print(t)
        n = sum(1 for k in TR.KEYS if self.s.on(k))
        self.console.print(Text(f"{n}/{len(TR.KEYS)} on · toggle: /tricks <key> on|off · /tricks all on|off · "
                                f"or use /settings", style="dim"))

    def tool_keys(self):
        return ([n for n in T.ALL_BUILTIN if n not in config.LOCKED_TOOLS]
                + ["mcp:" + srv for srv in self.mcp.config()])

    def toggle_command(self, arg, kind, tab, show, keys, setter):
        """/tools and /skills: no argument opens the settings tab; list; <name>|all on|off."""
        a = arg.split()
        if not a:
            self.open_settings(tab=tab)
        elif a == ["list"]:
            show()
        elif len(a) == 2 and a[1] in ("on", "off"):
            names = keys if a[0] == "all" else [k for k in keys if a[0] in (k, "mcp:" + k)]
            if not names:
                hint = " (always on)" if a[0] in config.LOCKED_TOOLS else f". /{kind}s list shows them"
                self.ui.notice(f"unknown {kind} '{a[0]}'{hint}", "error")
                return
            for k in names:
                setter(k, a[1] == "on")
            config.save_settings(self.s)
            self.ui.notice(f"{a[0]} → {a[1]}", "ok" if a[1] == "on" else "warn")
        else:
            self.ui.notice(f"usage: /{kind}s | /{kind}s list | /{kind}s <name> on|off | /{kind}s all on|off", "error")

    def _toggle_row(self, t, name, on, desc):
        t.add_row(Text(("  ● " if on else "  ○ ") + name, style="ok" if on else "err"), Text(desc, style="dim"))

    def show_tools(self):
        from .agent import READ_ONLY_CHAT_TOOLS
        for head, defs in (("core", T.CORE_TOOLS), ("advanced (category routing / enable_tools)", T.ADVANCED_TOOLS)):
            t = Table.grid(padding=(0, 2))
            for d in defs:
                n = d["function"]["name"]
                self._toggle_row(t, n, self.s.tool_on(n), d["function"]["description"][:90]
                                 + (" (always on)" if n in config.LOCKED_TOOLS else ""))
            self.console.print(Text(head, style="accent"), t)
        cfg = self.mcp.config()
        if cfg:
            man = self.mcp.manifest(force=True)
            t = Table.grid(padding=(0, 2))
            for srv in cfg:
                tl = man.get(srv) or []
                err = (man.get("_errors") or {}).get(srv)
                self._toggle_row(t, srv, "mcp:" + srv not in self.s.tools_off,
                                 ", ".join(x["name"] for x in tl) or f"(error: {err})")
            self.console.print(Text("mcp servers", style="accent"), t)
        else:
            self.console.print(Text(f"mcp: none configured ({config.MCP_CONFIG})", style="dim"))
        n = sum(1 for k in self.tool_keys() if k not in self.s.tools_off)
        self.console.print(Text(f"{n}/{len(self.tool_keys())} on · chat mode offers {', '.join(READ_ONLY_CHAT_TOOLS)} · "
                                f"toggle: /tools <name> on|off · /tools all on|off · or use /settings", style="dim"))

    def show_skills(self):
        infos = T.skill_infos(self.tb.workdir)
        t = Table.grid(padding=(0, 2))
        for i in infos:
            self._toggle_row(t, i["name"], self.s.skill_on(i["name"]), f"{i['scope']:<9} {i['desc']}")
        self.console.print(t if infos else Text(f"no skills (add .md files to {config.SKILLS_DIR_HOME})", style="dim"))
        n = sum(1 for i in infos if self.s.skill_on(i["name"]))
        self.console.print(Text(f"{n}/{len(infos)} on · toggle: /skills <name> on|off · /skills all on|off · "
                                f"or use /settings", style="dim"))

    # ---------------- sessions ----------------
    def resume(self, which, quiet=False):
        lst = Session.list(50)
        sid = None
        if which in ("last", True, "1") and lst:
            sid = lst[0]["id"]
        elif str(which).isdigit() and 1 <= int(which) <= len(lst):
            sid = lst[int(which) - 1]["id"]
        else:
            sid = next((x["id"] for x in lst if x["id"].startswith(str(which))), None)
        if not sid:
            self.ui.notice(f"no session '{which}'", "error")
            if self.agent.session is None:
                self.agent.session = Session(self.tb.workdir, self.s.model)
            return
        sess = Session.load(sid)
        if sess.workdir and os.path.isdir(sess.workdir):
            self.tb.set_workdir(sess.workdir)
        if not self.s.model and sess.model:
            self.s.model = sess.model
        sess.model = self.s.model or sess.model
        self.agent.session = sess
        self.tb.todo = sess.todo
        if not quiet:
            self.ui.notice(f"resumed '{sess.title}' ({len(sess.messages)} messages) in {sess.workdir}", "ok")
            if sess.todo:
                self.ui.todo(sess.todo)

    def list_sessions(self):
        lst = Session.list(20)
        if not lst:
            self.ui.notice("no saved sessions", "info")
            return
        t = Table.grid(padding=(0, 2))
        for i, x in enumerate(lst, 1):
            t.add_row(Text(f"{i}.", style="accent"), Text(time.strftime("%m-%d %H:%M", time.localtime(x["updated"])), style="dim"),
                      Text(x["title"][:60], style="bold"), Text(f"{x['n']} msgs", style="dim"),
                      Text(x["workdir"].replace(os.path.expanduser("~"), "~"), style="dim"))
        self.console.print(t)

    # ---------------- slash commands ----------------
    def command(self, line):
        parts = line.strip().split(maxsplit=1)
        cmd, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        s, ui = self.s, self.ui
        if cmd in ("/exit", "/quit", "/q"):
            return "exit"
        if cmd == "/help":
            t = Table.grid(padding=(0, 2))
            for k, v in COMMANDS.items():
                t.add_row(Text(k, style="accent"), Text(v, style="dim"))
            t.add_row(Text("!cmd", style="accent"), Text("run a shell command; output goes into the conversation", style="dim"))
            t.add_row(Text("@file", style="accent"), Text("mention a file: text is inlined, images attached", style="dim"))
            t.add_row(Text("esc+enter", style="accent"), Text("newline in the prompt", style="dim"))
            self.console.print(t)
        elif cmd in ("/model", "/models"):
            if arg:
                try:
                    models = self.backend.list_models()
                except Exception as e:
                    ui.notice(str(e), "error")
                    return
                match = arg if arg in models else next((m for m in models if m.startswith(arg)), None)
                if match:
                    self.set_model(match)
                else:
                    ui.notice(f"'{arg}' not installed. Installed: {', '.join(models)}", "error")
            else:
                try:
                    self.pick_model(self.backend.list_models())
                except Exception as e:
                    ui.notice(str(e), "error")
        elif cmd == "/think":
            s.think = config._tri(arg or "auto")
            config.save_settings(s)
            ui.notice(f"thinking: {({None: 'auto (model default)', True: 'on', False: 'off'})[s.think]}", "ok")
        elif cmd == "/ctx":
            try:
                v = arg.lower().replace("k", "000") if arg else ""
                n = int(v)
                n = n * 1024 // 1000 if arg.lower().endswith("k") else n
                s.num_ctx = max(2048, n)
                config.save_settings(s)
                ui.notice(f"context window: {s.num_ctx} tokens (the model reloads on the next call)", "ok")
            except ValueError:
                ui.notice(f"context window: {s.num_ctx}. Usage: /ctx 65536 or /ctx 64k", "info")
        elif cmd == "/mode":
            self.agent.force_mode = None if arg in ("", "auto") else arg
            ui.notice(f"routing: {arg or 'auto'}", "ok")
        elif cmd == "/timebox":
            if arg in ("", "off", "0", "none"):
                self.agent.timebox_min = None
                ui.notice("timebox off (a time limit written in the task text still applies)", "ok")
            else:
                v = H.parse_duration(arg)
                self.agent.timebox_min = v
                ui.notice(f"timebox: {v:.0f} min per task" if v else "usage: /timebox 40m | 1.5h | off", "ok" if v else "error")
        elif cmd == "/todo":
            if self.tb.todo:
                ui.todo(self.tb.todo)
            else:
                ui.notice("no plan yet", "info")
        elif cmd == "/undo":
            r = self.tb.rollback.undo()
            ui.notice(r or "nothing to undo", "ok" if r else "info")
            if r:
                self.agent.session.messages.append({"role": "user", "content": f"[harness] The user rolled back a change: {r}."})
        elif cmd == "/compact":
            sess = self.agent.session
            if len(sess.messages) > 4:
                sess.messages[:] = self.agent.compact(sess.messages, 2)
                sess.save()
            else:
                ui.notice("nothing to compact", "info")
        elif cmd in ("/clear", "/new"):
            self.agent.session = Session(self.tb.workdir, s.model)
            self.tb.todo = []
            self.tb.crawl = {}
            ui.notice("new session", "ok")
        elif cmd == "/sessions":
            self.list_sessions()
        elif cmd == "/resume":
            if arg:
                self.resume(arg)
            else:
                self.list_sessions()
                try:
                    from prompt_toolkit import prompt
                    a = prompt("  resume which › ").strip()
                except (EOFError, KeyboardInterrupt):
                    return
                if a:
                    self.resume(a)
        elif cmd == "/cd":
            p = os.path.abspath(os.path.expanduser(arg or "~"))
            if os.path.isdir(p):
                self.tb.set_workdir(p)
                self.agent.session.workdir = p
                ui.notice(f"working directory: {p}", "ok")
            else:
                ui.notice(f"not a directory: {p}", "error")
        elif cmd == "/tools":
            self.toggle_command(arg, "tool", "Tools", self.show_tools, self.tool_keys(), s.set_tool)
        elif cmd == "/skills":
            self.toggle_command(arg, "skill", "Skills", self.show_skills,
                                [i["name"] for i in T.skill_infos(self.tb.workdir)], s.set_skill)
        elif cmd == "/memory":
            mem = T._load_memory()
            self.console.print(Text("\n".join(f"[{m['ts']}] {m['text']}" for m in mem[-30:]) or "(empty)", style="dim"))
        elif cmd == "/offline":
            s.offline = not s.offline
            config.save_settings(s)
            ui.notice(f"offline mode {'ON: web tools are not assembled into the prompt' if s.offline else 'off'}", "ok")
        elif cmd == "/settings":
            self.open_settings()
        elif cmd == "/theme":
            if not arg:
                from .settings_menu import pick_theme
                chosen = pick_theme(self)
                ui.notice(f"theme: {chosen}" if chosen else f"theme unchanged ({s.theme})", "ok" if chosen else "info")
            elif self.apply_theme(arg):
                config.save_settings(s)
                ui.notice(f"theme: {arg}", "ok")
            else:
                ui.notice(f"unknown theme '{arg}'. Themes: {', '.join(themes.THEMES)}", "error")
        elif cmd == "/soul":
            self.soul_command(arg)
        elif cmd in ("/profile", "/profiles"):
            self.profile_command(arg)
        elif cmd == "/schedule":
            self.schedule_command(arg)
        elif cmd == "/tricks":
            a = arg.split()
            if not a:
                self.open_settings(tab="Tricks")
            elif a == ["list"]:
                self.show_tricks()
            elif len(a) == 2 and a[1] in ("on", "off"):
                keys = TR.KEYS if a[0] == "all" else ([a[0]] if a[0] in TR.BY_KEY else [])
                if not keys:
                    ui.notice(f"unknown trick '{a[0]}'. /tricks lists them", "error")
                    return
                for k in keys:
                    s.set_trick(k, a[1] == "on")
                config.save_settings(s)
                ui.notice(f"{a[0]} → {a[1]}", "ok" if a[1] == "on" else "warn")
            else:
                ui.notice("usage: /tricks | /tricks list | /tricks <key> on|off | /tricks all on|off", "error")
        elif cmd == "/unsafe":
            s.set_trick("behaviour_tiering", s.unsafe)
            config.save_settings(s)
            ui.notice(f"behaviour-tiering ring {'BYPASSED' if s.unsafe else 'active'} (hard rings always stay on)",
                      "warn" if s.unsafe else "ok")
        elif cmd == "/verbose":
            s.verbose = not s.verbose
            self.ui.verbose = s.verbose
            ui.notice(f"verbose {'on' if s.verbose else 'off'}", "ok")
        elif cmd == "/status":
            used = self.agent.last_ctx
            self.console.print(Text(
                f"model {s.model} · engine {s.backend} @ {self.backend.host}\n"
                f"context {used}/{s.num_ctx} ({used * 100 // max(1, s.num_ctx)}%) · output cap {s.num_predict} · "
                f"think {s.think} · temp {s.temperature} · keep_alive {s.keep_alive}\n"
                f"workdir {self.tb.workdir} · session {self.agent.session.id} ({len(self.agent.session.messages)} msgs)\n"
                f"soul {s.soul or 'none'} · offline {s.offline} · unsafe {s.unsafe} · timebox {self.agent.timebox_min} · mode {self.agent.force_mode or 'auto'}\n"
                f"scheduler: {len(SCH.load_jobs())} job(s) · {SCH.runner_text()}",
                style="dim"))
        elif cmd == "/init":
            p = os.path.join(self.tb.workdir, "FINCH.md")
            if os.path.exists(p):
                ui.notice("FINCH.md already exists", "info")
            else:
                with open(p, "w", encoding="utf-8") as f:
                    f.write("# Project notes for Finch\n\n- How to run tests: \n- Conventions: \n- Don't touch: \n")
                ui.notice("created FINCH.md: edit it; it's added to the task system prompt", "ok")
        else:
            ui.notice(f"unknown command {cmd}. /help lists commands", "error")

    def shell(self, cmd):
        try:
            rc, out, err, timed_out = T._run_proc(cmd, self.tb.workdir, self.s.bash_timeout)
            out = (out + err).rstrip() + ("\n(timed out)" if timed_out else "")
        except OSError as e:
            out, rc = f"(could not run {osutil.shell_name()}: {e})", 127
        self.console.print(Text(out[-4000:] or "(no output)", style="output"))
        self.agent.session.messages.append({"role": "user", "content":
            f"[harness] The user ran a shell command.\n$ {cmd}\n[exit {rc}]\n{out[-3000:]}"})

    # ---------------- scheduler ----------------
    def start_scheduler(self):
        """Run due jobs from this window while it is open (any number of runners is safe)."""
        self._sched_stop, self._sched_wake = threading.Event(), threading.Event()
        self._sched_seen = {j["id"]: j.get("last_finished") for j in SCH.load_jobs()}

        def loop():
            while not self._sched_stop.is_set():
                try:
                    SCH.tick()
                except Exception:
                    pass
                self._sched_wake.wait(SCH.TICK)
                self._sched_wake.clear()
        threading.Thread(target=loop, name="finch-scheduler", daemon=True).start()

    def kick_scheduler(self):
        if self._sched_wake:
            self._sched_wake.set()

    def _jobs_cached(self):
        t, jobs = self._sched_cache
        if time.time() - t > 3:
            jobs = SCH.load_jobs()
            self._sched_cache = (time.time(), jobs)
        return jobs

    def _new_results(self, jobs):
        return [j for j in jobs if j.get("last_finished") and not j.get("running")
                and j.get("last_finished") != self._sched_seen.get(j["id"])]

    def sched_flag(self):
        jobs = self._jobs_cached()
        if not jobs:
            return ""
        run = next((j for j in jobs if j.get("running")), None)
        new = self._new_results(jobs)
        if run:
            out = f"⏰ running {run['name']}"
        else:
            nxt = sorted((j for j in jobs if j["enabled"] and j.get("next_run")), key=lambda j: j["next_run"])
            out = f"⏰ {nxt[0]['name']} {SCH.fmt_time(nxt[0]['next_run'])}" if nxt else ""
        if new:
            out += f" · {len(new)} new result{'s' if len(new) > 1 else ''}"
        return out

    def show_job_results(self):
        jobs = SCH.load_jobs()
        for j in self._new_results(jobs):
            self._sched_seen[j["id"]] = j["last_finished"]
            lvl = {"ok": "ok", "failed": "error", "timeout": "error"}.get(j["last_status"], "warn")
            took = f" · {SCH.fmt_dur(j['last_duration'])}" if j.get("last_duration") else ""
            self.ui.notice(f"⏰ job '{j['name']}' {j['last_status']}{took} · /schedule log {j['name']}", lvl)
            if j.get("last_summary"):
                self.console.print(Text("\n".join("    " + ln for ln in j["last_summary"].splitlines()[-6:]), style="dim"))
        self._sched_cache = (0, [])

    def show_jobs(self):
        jobs = SCH.load_jobs()
        if not jobs:
            self.ui.notice("no scheduled jobs. /schedule <when> <task>, e.g. /schedule every day at 9am summarise ~/notes", "info")
        else:
            t = Table(box=None, padding=(0, 2), header_style="dim")
            for c in ("#", "name", "schedule", "state", "last run", ""):
                t.add_column(c)
            for i, j in enumerate(jobs, 1):
                st = SCH.state_text(j)
                t.add_row(str(i), Text(j["name"], style="bold"), j["desc"],
                          Text(st, style="dim" if st in ("paused", "done", "missed") else "accent"),
                          SCH.last_text(j), Text("auto-approve" if j.get("yes") else "", style="warn"))
            self.console.print(t)
        self.console.print(Text("  " + SCH.runner_text(), style="dim"))

    def schedule_command(self, arg):
        ui = self.ui
        sub, _, rest = arg.strip().partition(" ")
        sub, rest = sub.lower(), rest.strip()
        try:
            if not arg.strip():
                self.open_settings(tab="Schedule")
            elif sub in ("list", "ls"):
                self.show_jobs()
            elif sub in ("help", "?"):
                self.console.print(Text(
                    "  /schedule                      interactive job manager (Settings › Schedule)\n"
                    "  /schedule <when> <task>        add a job, e.g. /schedule every weekday at 8:30 summarise new commits\n"
                    "  /schedule <when> -- <task>     same, with an explicit separator\n"
                    "  /schedule new                  guided: when (live preview), task, folder, auto-approve\n"
                    "  /schedule list                 all jobs\n"
                    "  /schedule run|pause|resume|rm|log <job>\n"
                    "  /schedule when <job> <when>    change a job's schedule\n"
                    "  /schedule daemon start|stop|status   run jobs with no Finch window open\n\n"
                    "  when: in 10m · at 17:30 · tomorrow 9am · friday at 8am · oct 20 at 9am · every 30m · hourly ·\n"
                    "        daily at 9am · weekdays at 8:30 · every mon, wed and fri at 7pm · monthly on the 1st · cron 0 9 * * 1-5",
                    style="dim"))
            elif sub == "new":
                from .settings_menu import new_job_wizard
                self.console.print(Text("  " + new_job_wizard(self), style="ok"))
                self.kick_scheduler()
            elif sub in ("run", "pause", "resume", "rm", "delete", "log", "when") and not rest:
                ui.notice(f"usage: /schedule {sub} <job>" + (" <when>" if sub == "when" else ""), "error")
            elif sub == "run":
                j = SCH.request_run(rest)
                self.kick_scheduler()
                ui.notice(f"⏰ '{j['name']}' queued to run now; you'll see the result here when it finishes", "ok")
            elif sub in ("pause", "resume"):
                j = SCH.set_enabled(rest, sub == "resume")
                ui.notice(f"'{j['name']}' {'resumed · ' + SCH.state_text(j) if j['enabled'] else 'paused'}", "ok")
            elif sub in ("rm", "delete"):
                ui.notice(f"deleted '{SCH.remove(rest)['name']}'", "ok")
            elif sub == "log":
                j = SCH.find(rest)
                if not j:
                    raise ValueError(f"no job '{rest}'")
                if not j.get("last_log"):
                    ui.notice(f"'{j['name']}' has not run yet", "info")
                else:
                    self.console.print(Text(SCH.tail(j["last_log"], 80).rstrip(), style="output"))
                    self.console.print(Text(f"  {j['last_log']}", style="dim"))
            elif sub == "when":
                ref, _, when = rest.partition(" ")
                j = SCH.reschedule(ref, when)
                ui.notice(f"'{j['name']}' · {j['desc']} · next {SCH.fmt_time(j['next_run'])}", "ok")
            elif sub == "daemon":
                if rest == "start":
                    ui.notice(SCH.start_daemon()[1], "ok")
                elif rest == "stop":
                    ui.notice(SCH.stop_daemon(), "ok")
                else:
                    ui.notice(SCH.runner_text(), "info")
            else:
                text = rest if sub == "add" else arg.strip()
                when, task = SCH.split_when(text)
                if not task:
                    raise ValueError("what should Finch do? e.g. /schedule in 10m remind me to stretch")
                j = SCH.add_job(when, task, self.tb.workdir)
                ui.notice(f"⏰ scheduled '{j['name']}' · {j['desc']}", "ok")
                self.console.print(Text(
                    f"    first run {SCH.fmt_time(j['next_run'])} · task: {task[:80]}"
                    f" · folder: {self.tb.workdir.replace(os.path.expanduser('~'), '~')}\n"
                    + ("    runs while a Finch window is open; /schedule daemon start runs jobs without one"
                       if not SCH.daemon_pid() else "    the background daemon will run it"), style="dim"))
                self.kick_scheduler()
        except ValueError as e:
            ui.notice(str(e), "error")
        self._sched_cache = (0, [])

    # ---------------- main loops ----------------
    def run_task(self, text):
        text, images = expand_mentions(text, self.tb.workdir)
        cancel = threading.Event()
        try:
            if self.headless:
                return self.agent.run_turn(text, images=images, cancel=cancel)
            with SCH.busy():                  # scheduled jobs wait while you work (one GPU)
                return self.agent.run_turn(text, images=images, cancel=cancel)
        except KeyboardInterrupt:
            self.ui.abort()
            return None
        finally:
            self.ui.abort()

    def toolbar(self):
        s = self.s
        used = self.agent.last_ctx
        pct = used * 100 // max(1, s.num_ctx)
        wd = self.tb.workdir.replace(os.path.expanduser("~"), "~")
        if len(wd) > 32:
            wd = "…/" + "/".join(wd.rstrip("/").split("/")[-2:])
        flags = []
        if s.offline:
            flags.append("offline")
        if s.unsafe:
            flags.append("UNSAFE")
        n_off = sum(1 for k in TR.KEYS if not s.on(k))
        if n_off:
            flags.append(f"{n_off} trick{'s' if n_off > 1 else ''} off")
        if s.tools_off:
            flags.append(f"{len(s.tools_off)} tool{'s' if len(s.tools_off) > 1 else ''} off")
        if self.agent.timebox_min:
            flags.append(f"timebox {self.agent.timebox_min:.0f}m")
        if self.agent.force_mode:
            flags.append(f"mode:{self.agent.force_mode}")
        if s.soul:
            flags.append(f"soul:{s.soul}")
        sched = self.sched_flag()
        think = {None: "auto", True: "on", False: "off"}[s.think]
        ctxc = "tberr" if pct >= 75 else ("tbwarn" if pct >= 60 else "tbdim")
        from prompt_toolkit.formatted_text import HTML
        from html import escape
        return HTML(f" <tbbold>{escape(s.model or '-')}</tbbold> · <{ctxc}>ctx {used / 1000:.1f}K/{s.num_ctx // 1024}K ({pct}%)</{ctxc}>"
                    f" · think {think}" + (f" · <tbbold>{escape(sched)}</tbbold>" if sched else "")
                    + (f" · <tbwarn>{escape(' · '.join(flags))}</tbwarn>" if flags else "")
                    + f" · {escape(wd)}   <tbdim>/settings · /help · ctrl+d quit</tbdim>")

    def open_settings(self, tab="General"):
        from .settings_menu import TABS, SettingsMenu
        m = SettingsMenu(self)
        m.tab = TABS.index(tab)
        m.run()
        n = sum(1 for k in TR.KEYS if self.s.on(k))
        self.ui.notice(f"settings saved · {self.s.model} · ctx {self.s.num_ctx // 1024}K · theme {self.s.theme} · "
                       f"soul {self.s.soul or 'none'} · "
                       f"{n}/{len(TR.KEYS)} tricks on", "ok")

    def repl(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion, PathCompleter
        from prompt_toolkit.document import Document
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style

        app = self

        class FinchCompleter(Completer):
            def __init__(self):
                self.paths = PathCompleter(expanduser=True)

            def get_completions(self, document, ev):
                text = document.text_before_cursor
                if text.startswith("/") and " " not in text:
                    for k, v in COMMANDS.items():
                        if k.startswith(text):
                            yield Completion(k, -len(text), display_meta=v)
                    return
                m = re.search(r"@([\w./~\-]*)$", text)
                if m:
                    frag = m.group(1)
                    base = os.path.join(app.tb.workdir, frag) if not frag.startswith(("/", "~")) else frag
                    sub = Document(base, len(base))
                    for c in self.paths.get_completions(sub, ev):
                        yield Completion(c.text, c.start_position, display=c.display)

        kb = KeyBindings()

        @kb.add("escape", "enter")
        def _(event):
            event.current_buffer.insert_text("\n")

        from prompt_toolkit.styles import DynamicStyle
        style = DynamicStyle(lambda: Style.from_dict(themes.pt_style_dict(self.s.theme)))
        ps = PromptSession(history=FileHistory(config.HISTORY_FILE), completer=FinchCompleter(),
                           key_bindings=kb, complete_while_typing=True, style=style,
                           bottom_toolbar=self.toolbar, prompt_continuation=lambda w, n, s: "  ",
                           refresh_interval=5)
        backend_ver = "?"
        try:
            backend_ver = self.backend.version()
        except Exception:
            pass
        banner(self.console, self.s, self.s.model, self.tb.workdir, __version__, backend_ver)
        self.ensure_model()
        self.start_scheduler()
        last_ctrl_c = 0
        while True:
            self.show_job_results()
            try:
                self.console.print(Text("─" * self.console.size.width, style="rule"))
                line = ps.prompt(lambda: [("class:prompt", themes.glyph(self.s.theme, "prompt") + " ")])
            except KeyboardInterrupt:
                if time.time() - last_ctrl_c < 2:
                    break
                last_ctrl_c = time.time()
                self.ui.notice("press ctrl+c again to exit", "info")
                continue
            except EOFError:
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                if self.command(line) == "exit":
                    break
                continue
            if line.startswith("!"):
                self.shell(line[1:].strip())
                continue
            if not self.agent.session.model:
                if not self.ensure_model():
                    continue
            self.run_task(line)
        self.mcp.close()
        if self._sched_stop:
            self._sched_stop.set()
        self.console.print(Text("bye 👋", style="dim"))

    def headless_run(self, task):
        if not self.ensure_model():
            self.console.print(Text(f"model not available: {self.s.model!r}. Pass --model.", style="err"))
            return 2
        self.console.print(Text(f"finch · {self.s.model} · {self.tb.workdir}", style="dim"))
        try:
            res = self.run_task(task)
        finally:
            self.mcp.close()
        return 0 if res is not None else 1


def main(argv=None):
    osutil.utf8_stdio()
    ap = argparse.ArgumentParser(prog="finch", description="A local-first agent harness for small models.")
    ap.add_argument("-p", "--print", metavar="TASK", help="run one task headless and exit")
    ap.add_argument("-m", "--model")
    ap.add_argument("-C", "--cwd", help="working directory (default: current)")
    ap.add_argument("--host", help="Ollama host (default: $OLLAMA_HOST)")
    ap.add_argument("--ctx", type=int, help="context window (num_ctx)")
    ap.add_argument("--think", choices=["on", "off", "auto"])
    ap.add_argument("--timebox", help="time budget per task, e.g. 40m, 1.5h")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--resume", nargs="?", const="last", help="resume the last (or given) session")
    ap.add_argument("-y", "--yes", action="store_true", help="auto-approve permission prompts (hard safety rings stay on)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--theme", choices=list(themes.THEMES))
    ap.add_argument("--soul", help="persona to use (name from /soul list, or 'none')")
    ap.add_argument("--profile", help="start with a saved settings profile (other flags still override it)")
    ap.add_argument("--version", action="version", version=f"finch {__version__}")
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["schedule"]:
        sys.exit(SCH.cli_main(argv[1:]))
    args = ap.parse_args(argv)
    app = App(args)
    if args.print:
        task = args.print
        if task == "-":
            task = sys.stdin.read()
        sys.exit(app.headless_run(task))
    app.repl()


if __name__ == "__main__":
    main()

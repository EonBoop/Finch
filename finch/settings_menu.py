"""/settings: an inline, keyboard-driven settings menu (prompt_toolkit Application).

Tabs: General · Tricks · Theme · Soul · Schedule.  ↑/↓ move · ←/→ change · space/enter toggle or edit ·
tab switch tabs · esc/q save & close. Changes apply live and are persisted on close."""
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style

import os
import subprocess

from . import config, scheduler as SCH, souls, themes, tricks as TR

TABS = ("General", "Tricks", "Theme", "Soul", "Schedule")


class Item:
    def __init__(self, key, label, kind, get, set=None, options=None, desc="", fmt=None, group=None, target=None):
        self.key, self.label, self.kind = key, label, kind      # kind: bool|choice|int|action|nav|theme|header
        self.target = target                                     # nav: tab index to open
        self.get, self.set = get, set
        self.options = options or []
        self.desc = desc
        self.fmt = fmt or (lambda v: str(v))
        self.group = group


def _cycle(options, cur, step):
    if not options:
        return cur
    try:
        i = options.index(cur)
    except ValueError:
        i = -1 if step > 0 else 0
    return options[(i + step) % len(options)]


class SettingsMenu:
    def __init__(self, app):
        self.app = app                      # cli.App
        self.s = app.s
        self.tab = 0
        self.cursor = {i: 0 for i in range(len(TABS))}
        self.edit_request = None
        self.soul_request = None
        self.sched_request = None
        self.flash = ""
        try:
            self.models = app.backend.list_models()
        except Exception:
            self.models = [self.s.model] if self.s.model else []

    # ---------------- item lists ----------------
    def general_items(self):
        s, app = self.s, self.app
        think_fmt = {None: "auto (model default)", True: "on", False: "off"}

        def set_model(v):
            app.set_model(v, quiet=True)

        n_on = sum(1 for k in TR.KEYS if s.on(k))
        return [
            Item("_nav_tricks", "Harness tricks", "nav", lambda: f"{n_on}/{len(TR.KEYS)} on", target=1,
                 desc="Turn Mingbird's small-model mechanisms on or off individually. Enter to open (or press 2)."),
            Item("_nav_theme", "Theme", "nav", lambda: s.theme, target=2,
                 desc="Pick a colour theme with live preview. Enter to open (or press 3)."),
            Item("_nav_soul", "Soul", "nav", lambda: s.soul or "none", target=3,
                 desc="Switch the SOUL.md persona injected into every system prompt. Enter to open (or press 4)."),
            Item("_nav_sched", "Scheduled jobs", "nav", self._sched_summary, target=4,
                 desc="One-off and recurring jobs Finch runs on its own. Enter to open (or press 5)."),
            Item("_h_general", "General", "header", lambda: None),
            Item("model", "Model", "choice", lambda: s.model, set_model, self.models,
                 "Model used for new turns. ←/→ to cycle through installed models."),
            Item("think", "Thinking", "choice", lambda: s.think, lambda v: setattr(s, "think", v),
                 [None, True, False], "Tri-state: auto sends no 'think' field; on/off sends it top-level.",
                 fmt=lambda v: think_fmt[v]),
            Item("num_ctx", "Context window", "int", lambda: s.num_ctx, lambda v: setattr(s, "num_ctx", max(2048, int(v))),
                 [4096, 8192, 16384, 32768, 65536, 131072, 262144],
                 "num_ctx sent to the model. Bigger = more memory; the model reloads when it changes. Enter = custom.",
                 fmt=lambda v: f"{v // 1024}K ({v})"),
            Item("num_predict", "Output cap", "int", lambda: s.num_predict,
                 lambda v: setattr(s, "num_predict", max(256, int(v))), [2048, 4096, 8192, 16384, 32768],
                 "Max tokens per model call (a cap, not a target). Mingbird uses 8192.", fmt=lambda v: f"{v} tokens"),
            Item("temperature", "Temperature", "choice", lambda: s.temperature,
                 lambda v: setattr(s, "temperature", v), [None, 0.0, 0.2, 0.4, 0.7, 1.0],
                 "None = don't send it (the model manifest decides).",
                 fmt=lambda v: "model default" if v is None else str(v)),
            Item("keep_alive", "Keep model loaded", "choice", lambda: s.keep_alive,
                 lambda v: setattr(s, "keep_alive", v), ["5m", "30m", "1h", "4h", "-1"],
                 "keep_alive sent with every request, so long tool chains don't unload the model. -1 = forever."),
            Item("bash_timeout", "Command timeout", "int", lambda: s.bash_timeout,
                 lambda v: setattr(s, "bash_timeout", max(5, int(v))), [30, 60, 120, 300, 600, 1800],
                 "Seconds before run_bash / run_python is killed (whole process tree).", fmt=lambda v: f"{v}s"),
            Item("mode", "Routing", "choice", lambda: app.agent.force_mode,
                 lambda v: setattr(app.agent, "force_mode", v), [None, "task", "chat"],
                 "Force every message to task or chat mode, or let Q&A layering decide (auto). Not persisted.",
                 fmt=lambda v: v or "auto"),
            Item("timebox", "Timebox", "choice", lambda: app.agent.timebox_min,
                 lambda v: setattr(app.agent, "timebox_min", v), [None, 10.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0],
                 "Per-task time budget with pacing nudges at 50/75/90%. Not persisted.",
                 fmt=lambda v: "off" if not v else f"{v:.0f} min"),
            Item("offline", "Offline mode", "bool", lambda: s.offline, lambda v: setattr(s, "offline", v),
                 desc="Web tools are not assembled into the prompt at all."),
            Item("verbose", "Verbose output", "bool", lambda: s.verbose, lambda v: (setattr(s, "verbose", v),
                 setattr(app.ui, "verbose", v)), desc="Show full thinking and full tool output."),
            Item("lang_hint", "Force English", "bool", lambda: s.lang_hint == "en",
                 lambda v: setattr(s, "lang_hint", "en" if v else ""),
                 desc="Append 'Always respond in English' to the system prompt."),
        ]

    def trick_items(self):
        s = self.s
        out = [
            Item("_back", "‹ Back to General", "nav", lambda: "", target=0, desc="Return to the General tab (or backspace / 1)."),
            Item("_all_on", "Enable all tricks", "action", lambda: None, lambda v: self._all(True),
                 desc="The full Mingbird harness (default)."),
            Item("_all_off", "Disable all tricks (raw model)", "action", lambda: None, lambda v: self._all(False),
                 desc="Plain agent loop: useful to see how much the harness is doing. The hard safety ring stays on."),
        ]
        for g in TR.groups():
            out.append(Item("_h_" + g, g, "header", lambda: None))
            for key, grp, label, desc in TR.TRICKS:
                if grp == g:
                    out.append(Item(key, label, "bool", (lambda k=key: s.on(k)),
                                    (lambda v, k=key: s.set_trick(k, v)), desc=desc, group=g))
        return out

    def theme_items(self):
        back = Item("_back", "‹ Back to General", "nav", lambda: "", target=0,
                    desc="Return to the General tab (or backspace / 1).")
        out = [back]
        for grp, names in themes.GROUPS.items():
            out.append(Item("_h_" + grp, grp, "header", lambda: None))
            for name in names:
                out.append(Item(name, name, "theme", lambda: self.s.theme,
                                lambda v, n=name: self.app.apply_theme(n), desc=themes.get(name)["desc"]))
        return out

    def soul_items(self):
        s = self.s
        wd = self.app.tb.workdir
        back = Item("_back", "‹ Back to General", "nav", lambda: "", target=0,
                    desc="Return to the General tab (or backspace / 1).")
        out = [back,
               Item("_soul_new", "Create a new soul…", "action", lambda: None, lambda v: self._soul_req("create"),
                    desc="Name it, Finch writes a template to ~/.finch/souls/<name>.md and opens it in your editor."),
               Item("_soul_edit", "Edit the active soul…", "action", lambda: None, lambda v: self._soul_req("edit"),
                    desc="Open the active soul in $VISUAL / $EDITOR (nano / vim fallback). "
                         "Built-in souls are copied to ~/.finch/souls first."),
               Item("_h_souls", "Souls", "header", lambda: None),
               Item("", "none", "soul", lambda: s.soul, lambda v: setattr(s, "soul", ""),
                    desc="No persona: Finch's built-in identity line only (smallest prompt).")]
        for sl in souls.list_souls(wd):
            out.append(Item(sl["name"], sl["name"], "soul", lambda: s.soul,
                            lambda v, n=sl["name"]: setattr(s, "soul", n),
                            desc=f"{sl['desc']}  ·  {sl['scope']} · ~{sl['tokens']} tokens per request · {sl['path']}"))
        return out

    def _sched_summary(self):
        jobs = SCH.load_jobs()
        n_on = sum(1 for j in jobs if j["enabled"] and j.get("next_run"))
        return f"{len(jobs)} job{'s' if len(jobs) != 1 else ''}" + (f", {n_on} active" if jobs else "")

    def schedule_items(self):
        jobs = SCH.load_jobs()
        back = Item("_back", "‹ Back to General", "nav", lambda: "", target=0,
                    desc="Return to the General tab (or backspace / 1).")
        out = [back,
               Item("_sched_new", "New scheduled job…", "action", lambda: None, lambda v: self._sched_req("new"),
                    desc="Choose when (one-off or recurring, with a live preview of the next runs), what Finch should "
                         "do, and the folder. Jobs run headless and keep a log of every run."),
               Item("_sched_daemon", "Background daemon", "bool", lambda: bool(SCH.daemon_pid()), self._toggle_daemon,
                    desc="On: jobs run even when no Finch window is open (same as `finch schedule start`). "
                         "Off: jobs run only while a Finch window is open. Only one job runs at a time either way."),
               Item("_h_jobs", f"Jobs ({len(jobs)})  ·  enter on a job for actions" if jobs
                    else "No jobs yet: create one above, or type /schedule <when> <task>", "header", lambda: None)]
        for j in jobs:
            flags = " · auto-approve" if j.get("yes") else ""
            last = j.get("last_summary", "").strip().splitlines()
            out.append(Item(j["id"], j["name"], "job", lambda: None,
                            desc=f"{j['prompt'][:160]}  ·  {j['workdir'].replace(os.path.expanduser('~'), '~')}{flags}"
                                 + (f"\n  last output: {last[-1][:150]}" if last else "")))
        return out

    def _toggle_daemon(self, on):
        self.flash = SCH.start_daemon()[1] if on else SCH.stop_daemon()

    def _sched_req(self, kind, jid=None):
        self.sched_request = (kind, jid)
        self.application.exit(result="sched")

    def _soul_req(self, kind):
        self.soul_request = kind
        self.application.exit(result="soul")

    def _all(self, on):
        for k in TR.KEYS:
            self.s.set_trick(k, on)
        self.flash = "all tricks " + ("enabled" if on else "disabled")

    def items(self):
        return (self.general_items, self.trick_items, self.theme_items, self.soul_items, self.schedule_items)[self.tab]()

    # ---------------- rendering ----------------
    def _selectable(self, items):
        return [i for i, it in enumerate(items) if it.kind != "header"]

    def render(self):
        items = self.items()
        sel = self._selectable(items)
        cur = self.cursor[self.tab] = min(self.cursor[self.tab], len(sel) - 1)
        cur_idx = sel[cur]
        out = [("class:sm.title", " ⚙  Settings  ")]
        for i, t in enumerate(TABS):
            out.append(("class:sm.tab.current" if i == self.tab else "class:sm.tab", f" {i + 1} {t} "))
            out.append(("", " "))
        if self.tab == 1:
            n_on = sum(1 for k in TR.KEYS if self.s.on(k))
            out.append(("class:sm.desc", f"   {n_on}/{len(TR.KEYS)} on"))
        out.append(("", "\n\n"))
        try:
            rows = self.application.output.get_size().rows
        except Exception:
            rows = 30
        height = max(6, min(len(items), rows - 9))
        top = max(0, min(cur_idx - height // 2, len(items) - height))
        width = max(len(it.label) for it in items if it.kind != "header") + 2
        if top > 0:
            out.append(("class:sm.desc", "   ↑ more\n"))
        for i in range(top, min(len(items), top + height)):
            it = items[i]
            if it.kind == "header":
                out.append(("class:sm.group", f"\n  {it.label}\n" if i > 0 else f"  {it.label}\n"))
                continue
            selected = i == cur_idx
            out.append(("class:sm.cursor", " ❯ " if selected else "   "))
            out.append(("class:sm.selected" if selected else "class:sm.label", it.label.ljust(width)))
            out += self._value_frag(it)
            out.append(("", "\n"))
        if top + height < len(items):
            out.append(("class:sm.desc", "   ↓ more\n"))
        it = items[cur_idx]
        out.append(("", "\n"))
        out.append(("class:sm.desc", "  " + (it.desc or "") + "\n"))
        if self.flash:
            out.append(("class:sm.value", "  " + self.flash + "\n"))
        out.append(("class:sm.help", "  ↑↓ move · ←→ change · enter select/open · 1 General · 2 Tricks · 3 Theme · 4 Soul · "
                                     "5 Schedule · tab next tab · esc save & close"))
        return out

    def _value_frag(self, it):
        if it.kind == "bool":
            return [("class:sm.on", "● on ")] if it.get() else [("class:sm.off", "○ off")]
        if it.kind in ("choice", "int"):
            return [("class:sm.value", f"‹ {it.fmt(it.get())} ›")]
        if it.kind == "action":
            return [("class:sm.desc", "↵")]
        if it.kind == "nav":
            v = it.get()
            return [("class:sm.value", f"{v}  ›" if v else "")] if it.target else [("", "")]
        if it.kind == "soul":
            info = next((x for x in souls.list_souls(self.app.tb.workdir) if x["name"] == it.key), None)
            meta = f"  {info['scope']} · ~{info['tokens']} tok" if info else "  0 tok"
            mark = [("class:sm.on", "  ✓ active")] if (it.get() or "") == it.key else []
            return [("class:sm.desc", meta)] + mark
        if it.kind == "job":
            j = SCH.find(it.key)
            if not j:
                return [("", "")]
            st, last = SCH.state_text(j), SCH.last_text(j)
            lcls = ("class:sm.on" if j.get("last_status") == "ok" else
                    "class:sm.off" if j.get("last_status") in ("failed", "timeout", "interrupted") else "class:sm.desc")
            return [("class:sm.value", f"{j['desc'][:34]:<36}"),
                    ("class:sm.desc" if st in ("paused", "done", "missed") else "class:sm.on", f"{st:<22}"),
                    (lcls, last)]
        if it.kind == "theme":
            sw = [(st, "■ ") for st in themes.swatch(it.key)]
            mark = [("class:sm.on", "  ✓ current")] if it.get() == it.key else []
            return sw + [("class:sm.desc", "  " + themes.get(it.key)["desc"])] + mark
        return [("", "")]

    # ---------------- actions ----------------
    def _cur_item(self):
        items = self.items()
        return items[self._selectable(items)[self.cursor[self.tab]]]

    def change(self, step, activate=False):
        it = self._cur_item()
        self.flash = ""
        if it.kind == "bool":
            it.set((not it.get()) if activate or step == 0 else step > 0)
        elif it.kind == "choice":
            it.set(_cycle(it.options, it.get(), step or 1))
        elif it.kind == "int":
            if activate:
                self.edit_request = it
                self.application.exit(result="edit")
                return
            opts = sorted(set(it.options) | {it.get()})
            it.set(_cycle(opts, it.get(), step))
        elif it.kind == "action" and activate:
            it.set(None)
        elif it.kind == "nav" and (activate or (step > 0 and it.target) or (step < 0 and not it.target)):
            self.goto(it.target)
        elif it.kind == "job" and activate:
            self._sched_req("job", it.key)
        elif it.kind == "soul" and (activate or step):
            it.set(it.key)
            self.flash = f"soul set to {it.key or 'none'}"
        elif it.kind == "theme" and (activate or step):
            it.set(it.key)
            self._restyle()
            self.flash = f"theme set to {it.key} (esc to save & close)"

    def goto(self, tab):
        self.tab = tab
        self.flash = ""
        if tab != 0:
            self.cursor.setdefault(tab, 0)

    def _restyle(self):
        self.application.style = Style.from_dict(themes.pt_style_dict(self.s.theme))

    def run(self):
        kb = KeyBindings()

        def move(d):
            sel = self._selectable(self.items())
            self.cursor[self.tab] = (self.cursor[self.tab] + d) % len(sel)

        @kb.add("up")
        @kb.add("k")
        def _(e): move(-1)

        @kb.add("down")
        @kb.add("j")
        def _(e): move(1)

        @kb.add("pageup")
        def _(e): move(-8)

        @kb.add("pagedown")
        def _(e): move(8)

        @kb.add("tab")
        def _(e):
            self.tab = (self.tab + 1) % len(TABS)
            self.flash = ""

        @kb.add("s-tab")
        def _(e):
            self.tab = (self.tab - 1) % len(TABS)
            self.flash = ""

        for n in range(len(TABS)):
            @kb.add(str(n + 1))
            def _(e, n=n):
                self.goto(n)

        @kb.add("backspace")
        def _(e):
            self.goto(0)

        @kb.add("left")
        @kb.add("h")
        def _(e):
            self.change(-1)
            self._restyle()

        @kb.add("right")
        @kb.add("l")
        def _(e):
            self.change(1)
            self._restyle()

        @kb.add("space")
        @kb.add("enter")
        def _(e):
            self.change(1, activate=True)
            if self.application.is_running:
                self._restyle()

        @kb.add("escape")
        @kb.add("q")
        @kb.add("c-c")
        def _(e):
            e.app.exit(result="done")

        control = FormattedTextControl(self.render, focusable=True, show_cursor=False)
        self.application = Application(
            layout=Layout(HSplit([Window(control, height=Dimension(min=8), wrap_lines=True)])),
            key_bindings=kb, style=Style.from_dict(themes.pt_style_dict(self.s.theme)),
            full_screen=False, erase_when_done=True, mouse_support=False)
        self.application.ttimeoutlen = 0.05      # snappy esc without swallowing arrow / shift-tab sequences
        while True:
            self.edit_request = None
            self.soul_request = None
            res = self.application.run()
            if res == "sched":
                kind, jid = self.sched_request
                self.flash = new_job_wizard(self.app) if kind == "new" else job_actions(self.app, jid)
                if hasattr(self.app, "kick_scheduler"):
                    self.app.kick_scheduler()
                continue
            if res == "soul":
                self._handle_soul_request()
                continue
            if res == "edit" and self.edit_request is not None:
                it = self.edit_request
                try:
                    from prompt_toolkit import prompt
                    v = prompt(f"  {it.label} [{it.get()}] › ").strip().lower()
                    if v:
                        mult = 1
                        if v.endswith("k"):          # "64k" -> 65536
                            v, mult = v[:-1], 1024
                        it.set(int(float(v) * mult))
                except (ValueError, EOFError, KeyboardInterrupt):
                    self.flash = "invalid value"
                continue
            break
        config.save_settings(self.s)

    def _handle_soul_request(self):
        from prompt_toolkit import prompt
        kind = self.soul_request
        try:
            if kind == "create":
                name = prompt("  new soul name › ").strip()
                if not name:
                    self.flash = "cancelled"
                    return
                desc = prompt("  one-line description › ").strip() or "my custom soul"
                name, _ = souls.create(name, desc=desc)
                self.s.soul = name
                self.flash = edit_soul(self.app, name) + f" · soul set to {name}"
            elif kind == "edit":
                if not self.s.soul:
                    self.flash = "no active soul: pick one first"
                    return
                self.flash = edit_soul(self.app, self.s.soul)
        except (EOFError, KeyboardInterrupt):
            self.flash = "cancelled"


def edit_soul(app, name):
    """Open a soul in the user's editor; built-in souls are copied to ~/.finch/souls first.
    Returns a status message."""
    wd = app.tb.workdir
    info = souls.find(name, wd)
    if not info:
        return f"no soul named '{name}'"
    path = info["path"]
    if info["scope"] == "built-in":
        dst_dir = os.path.join(config.FINCH_HOME, "souls")
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, name + ".md")
        if not os.path.exists(dst):
            with open(path, encoding="utf-8") as src, open(dst, "w", encoding="utf-8") as out:
                out.write(src.read())
        path = dst
    ed = souls.editor_cmd()
    if not ed:
        return f"no editor found (set $EDITOR). Edit the file directly: {path}"
    subprocess.call(ed.split() + [path])
    return f"saved {path}"


def pick_theme(app):
    """/theme with no argument: ↑↓ preview live · enter apply · esc revert. Returns the chosen name or None."""
    names = [n for ns in themes.GROUPS.values() for n in ns]
    original = app.s.theme
    state = {"i": names.index(original) if original in names else 0}

    def render():
        out = [("class:sm.title", " Theme  "), ("class:sm.desc", "↑↓ preview · enter select · esc cancel\n")]
        for i, n in enumerate(names):
            grp = next(g for g, ns in themes.GROUPS.items() if n in ns)
            if themes.GROUPS[grp][0] == n:
                out.append(("class:sm.group", f"\n  {grp}\n"))
            sel = i == state["i"]
            out.append(("class:sm.cursor", " ❯ " if sel else "   "))
            out.append(("class:sm.selected" if sel else "class:sm.label", n.ljust(17)))
            out += [(st, "■ ") for st in themes.swatch(n)]
            out.append(("class:sm.desc", "  " + themes.get(n)["desc"]))
            if n == original:
                out.append(("class:sm.on", "  (current)"))
            out.append(("", "\n"))
        return out

    def preview(event):
        app.apply_theme(names[state["i"]])
        event.app.style = Style.from_dict(themes.pt_style_dict(names[state["i"]]))

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _(e):
        state["i"] = (state["i"] - 1) % len(names)
        preview(e)

    @kb.add("down")
    @kb.add("j")
    def _(e):
        state["i"] = (state["i"] + 1) % len(names)
        preview(e)

    @kb.add("enter")
    @kb.add("space")
    def _(e):
        e.app.exit(result=names[state["i"]])

    @kb.add("escape")
    @kb.add("q")
    @kb.add("c-c")
    def _(e):
        e.app.exit(result=None)

    application = Application(
        layout=Layout(HSplit([Window(FormattedTextControl(render, focusable=True, show_cursor=False),
                                     height=Dimension(min=len(names) + 2 * len(themes.GROUPS) + 2))])),
        key_bindings=kb, style=Style.from_dict(themes.pt_style_dict(original)),
        full_screen=False, erase_when_done=True)
    application.ttimeoutlen = 0.05
    choice = application.run()
    if choice is None:
        app.apply_theme(original)
        return None
    app.apply_theme(choice)
    config.save_settings(app.s)
    return choice


def choose(app, title, options, hint="↑↓ move · enter select · esc cancel"):
    """Small inline picker. options: [(key, label)] -> chosen key or None."""
    state = {"i": 0}

    def render():
        out = [("class:sm.title", f" {title} "), ("", "\n"), ("class:sm.desc", f"  {hint}\n\n")]
        for i, (_, label) in enumerate(options):
            sel = i == state["i"]
            out += [("class:sm.cursor", " ❯ " if sel else "   "),
                    ("class:sm.selected" if sel else "class:sm.label", label), ("", "\n")]
        return out

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _(e):
        state["i"] = (state["i"] - 1) % len(options)

    @kb.add("down")
    @kb.add("j")
    def _(e):
        state["i"] = (state["i"] + 1) % len(options)

    @kb.add("enter")
    @kb.add("space")
    def _(e):
        e.app.exit(result=options[state["i"]][0])

    @kb.add("escape")
    @kb.add("q")
    @kb.add("c-c")
    def _(e):
        e.app.exit(result=None)

    application = Application(
        layout=Layout(HSplit([Window(FormattedTextControl(render, focusable=True, show_cursor=False),
                                     height=Dimension(min=len(options) + 4), wrap_lines=True)])),
        key_bindings=kb, style=Style.from_dict(themes.pt_style_dict(app.s.theme)),
        full_screen=False, erase_when_done=True)
    application.ttimeoutlen = 0.05
    return application.run()


def _ask(app, label, default="", validator=None, toolbar=None):
    """One prompt_toolkit question in the current theme. Raises EOFError/KeyboardInterrupt on cancel."""
    from prompt_toolkit import prompt
    return prompt(f"  {label} › ", default=default, validator=validator, validate_while_typing=False,
                  bottom_toolbar=toolbar, style=Style.from_dict(themes.pt_style_dict(app.s.theme))).strip()


def _when_tools():
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.validation import ValidationError, Validator

    def preview():
        txt = get_app().current_buffer.text
        if not txt.strip():
            return " e.g. in 10m · tomorrow 9am · friday at 17:00 · every 2h · daily at 9am · weekdays at 8:30 · cron 0 9 * * 1-5"
        try:
            sp = SCH.parse_when(txt)
            return " → " + sp["desc"] + " · next: " + ", ".join(SCH.fmt_time(t) for t in SCH.upcoming(sp))
        except ValueError as e:
            return " ✗ " + str(e)

    class WhenValidator(Validator):
        def validate(self, doc):
            if doc.text.strip():
                try:
                    SCH.parse_when(doc.text)
                except ValueError as e:
                    raise ValidationError(message=str(e))
    return preview, WhenValidator()


def new_job_wizard(app):
    """Guided job creation. Returns a status message."""
    from prompt_toolkit.validation import Validator
    preview, when_v = _when_tools()
    try:
        when = _ask(app, "When? (one-off or recurring)", validator=when_v, toolbar=preview)
        if not when:
            return "cancelled"
        task = _ask(app, "What should Finch do?",
                    toolbar=lambda: " Runs headless like `finch -p`: describe the task and where the result should go "
                                    "(a file, a summary...)")
        if not task:
            return "cancelled"
        isdir = Validator.from_callable(lambda t: os.path.isdir(os.path.expanduser(t.strip() or ".")),
                                        error_message="no such directory")
        wd = _ask(app, "Working directory", default=app.tb.workdir, validator=isdir)
        yes = _ask(app, "Auto-approve risky actions? [y/N]",
                   toolbar=lambda: " No (default): installs, deletes outside the project, etc. are denied, because "
                                   "nobody is there to confirm. The hard safety ring is always on.").lower()
        j = SCH.add_job(when, task, wd or app.tb.workdir, yes=yes.startswith("y"))
    except (EOFError, KeyboardInterrupt):
        return "cancelled"
    except ValueError as e:
        return f"not scheduled: {e}"
    msg = f"⏰ scheduled '{j['name']}' · {j['desc']} · next {SCH.fmt_time(j['next_run'])}"
    if not SCH.daemon_pid():
        msg += " · runs while a Finch window is open (or turn on the background daemon)"
    return msg


def _view_log(path):
    import shutil
    pager = os.environ.get("PAGER") or ("less" if shutil.which("less") else "")
    if pager:
        cmd = pager.split() + (["+G"] if pager.split()[0] == "less" else []) + [path]
        subprocess.call(cmd)
        return f"log: {path}"
    print(SCH.tail(path, 80))
    try:
        input("  (enter to return) ")
    except (EOFError, KeyboardInterrupt):
        pass
    return f"log: {path}"


def job_actions(app, jid):
    """Enter on a job in the Schedule tab. Returns a status message."""
    j = SCH.find(jid)
    if not j:
        return "job no longer exists"
    opts = [("run", "Run now"),
            ("toggle", "Pause" if j["enabled"] else "Resume"),
            ("log", "View last log" if j.get("last_log") else "View last log (no runs yet)"),
            ("prompt", "Edit task…"),
            ("when", "Change schedule…"),
            ("dir", "Change working directory…"),
            ("yes", f"Auto-approve risky actions: {'on → turn off' if j.get('yes') else 'off → turn on'}"),
            ("delete", "Delete…"),
            ("cancel", "Back")]
    title = f"{j['name']} · {j['desc']} · {SCH.state_text(j)}"
    act = choose(app, title, opts)
    try:
        if act in (None, "cancel"):
            return ""
        if act == "run":
            SCH.request_run(jid)
            return f"'{j['name']}' queued to run now; the result shows in the REPL when it finishes"
        if act == "toggle":
            jj = SCH.set_enabled(jid, not j["enabled"])
            return f"'{jj['name']}' " + ("resumed · " + SCH.state_text(jj) if jj["enabled"] else "paused")
        if act == "log":
            return _view_log(j["last_log"]) if j.get("last_log") else "no runs yet"
        if act == "prompt":
            t = _ask(app, "Task", default=j["prompt"])
            if t:
                SCH.update(jid, prompt=t)
                return "task updated"
            return "unchanged"
        if act == "when":
            preview, when_v = _when_tools()
            w = _ask(app, "When?", default=j["when"], validator=when_v, toolbar=preview)
            if w:
                jj = SCH.reschedule(jid, w)
                return f"'{jj['name']}' · {jj['desc']} · next {SCH.fmt_time(jj['next_run'])}"
            return "unchanged"
        if act == "dir":
            from prompt_toolkit.validation import Validator
            isdir = Validator.from_callable(lambda t: os.path.isdir(os.path.expanduser(t.strip())),
                                            error_message="no such directory")
            d = _ask(app, "Working directory", default=j["workdir"], validator=isdir)
            SCH.update(jid, workdir=os.path.abspath(os.path.expanduser(d)))
            return "working directory updated"
        if act == "yes":
            SCH.update(jid, yes=not j.get("yes"))
            return f"auto-approve {'off' if j.get('yes') else 'on'} for '{j['name']}'"
        if act == "delete":
            if choose(app, f"Delete '{j['name']}'?", [("no", "Keep it"), ("yes", f"Delete '{j['name']}' and its logs")]) == "yes":
                SCH.remove(jid)
                return f"deleted '{j['name']}'"
            return "kept"
    except (EOFError, KeyboardInterrupt):
        return "cancelled"
    except ValueError as e:
        return str(e)
    return ""

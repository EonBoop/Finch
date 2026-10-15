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
| `finch/tricks.py` | Registry of the 35 toggleable tricks (key, group, label, description) and ablation aliases. |
| `finch/themes.py` | 7 colour themes: rich styles + prompt_toolkit styles, swatch helpers. |
| `finch/settings_menu.py` | `/settings`: inline prompt_toolkit app with General · Tricks · Theme · Soul · Schedule tabs, plus the job wizard and `choose()` picker. |
| `finch/scheduler.py` | Task scheduler: schedule phrase parser + 5-field cron engine, file-locked job store (`~/.finch/schedule/jobs.json`), runner `tick()`, per-run supervisor (`finch schedule exec`), daemon, `finch schedule …` CLI. |
| `finch/souls.py`, `finch/builtin_souls/` | SOUL.md personas: discovery (project > user > built-in), frontmatter parsing, create/edit, injection text. |
| `finch/skills/*.md` | Built-in skills: bug_fix, test_driven, python_project, web_research, data_analysis, code_review. |
| `scripts/pty_drive.py` | Dev tool: drives the REPL in a pty (pexpect + pyte, answers CPR) and prints the screen. Use it to verify menus/toolbars. |
| `CLAUDE.md` | Local only (gitignored). Short orientation for Claude: commands, layout, invariants, gotchas, how this user works. |
| `finch/osutil.py` | Every OS difference: shell choice and argv, process groups / tree kill, pid liveness, file lock, retried `os.replace`, console decoding, python discovery, UTF-8 child env. |
| `tests/test_harness.py` | 162 tests, including a scripted `FakeLLM` that drives the full loop. |

Runtime state lives in `~/.finch/`: `config.json`, `sessions/`, `memory.json`, `mcp.json`, `skills/`,
`cache/`, `history` and `schedule/` (jobs.json, logs/, daemon.pid, daemon.log, busy/). Per-workdir state lives in `<workdir>/.finch/`: `backups/`, `trash/` and `sources/` (fetched web pages).

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
| Stall feedback (Finch) | No data for `stream_timeout` (a huge tool call: Ollama streams nothing while one is written) → "write it in pieces", line budget shrinking per repeat, no back-off. |
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
   keep_alive, command timeout, routing, timebox, offline, verbose, force English), Tricks (32 toggles; 33 since the stream-loop guard
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

### Follow-up: repo restore

- The `finch/` package directory had been replaced by a clone of `github.com/EonBoop/Finch` (only `.git`, `LICENSE`,
  `README.md`). Finch's own agent was not involved (that session only ran `git remote -v` and `git branch`).
- Rebuilt by replaying every Write/Edit/patch-script from the Claude Code session transcript into a staging dir. Verified:
  README, NOTES and pyproject.toml matched the surviving copies byte for byte, and all 93 tests passed.
- The git repo was moved from the package dir to the project root (`LICENSE` to the root, the repo's README tagline merged
  into README.md, `.gitignore` added) and committed as `d67560b` on top of "Initial commit". Not pushed.

### Follow-up: task scheduler

- `finch/scheduler.py` (no UI) plus UI in cli.py / settings_menu.py. One-off and recurring jobs; schedule phrases are parsed
  to `once` (timestamp), `interval` (seconds) or `cron` (5-field, own engine, dom/dow OR-semantics like cron).
- Design choices:
  - Jobs run as `finch -p` subprocesses, so they get the full harness and headless mode's unattended safety
    (risky actions denied unless the job has `yes`).
  - Every run has its own supervisor process (`python -I -m finch schedule exec <id>`, new session). It enforces the
    hard timeout (killpg), writes the log and records the result, so closing the window that launched it doesn't
    orphan the record.
  - Runners (any REPL's background thread every 15 s, and/or the daemon) only call `tick()`. That claims the next
    due job under an `fcntl` lock on `schedule/.lock` and starts nothing while any job is running, so there are no
    double runs and only one job runs at a time across processes.
  - A dead runner's pid gets the job marked `interrupted` (zombies count as dead via /proc state).
  - Interactive tasks write `schedule/busy/<pid>`, and due jobs wait for them (max 30 min).
  - Missed runs: recurring jobs catch up once, then re-anchor to now. One-offs run if less than 24 h late, else
    they're marked `missed`.
- UI:
  - The Schedule tab (tab 5) has "New scheduled job…" (wizard with a live preview of the next runs in the bottom
    toolbar), a Background daemon toggle, and job rows. Enter on a row opens the actions picker (`choose()` helper).
  - `/schedule` commands: `<when> <task>` splits on the longest leading phrase that parses, or on ` -- `.
  - The toolbar shows "⏰ next/running job · N new results". Finished-job notices with the last output lines
    appear before the next prompt. `/status` has a scheduler line.
- Bugs found while testing:
  - `python -m finch` puts the cwd on sys.path. Started from `~/workspace`, it imported the project folder `finch/`
    as a namespace package and crashed. All spawned processes now use `python -I`, and the daemon runs with
    `cwd=~/.finch/schedule`.
  - **Pre-existing:** `main()` set `PROMPT_TOOLKIT_NO_CPR=1` (added earlier to silence a CPR warning under pyte).
    prompt_toolkit only draws bottom toolbars once the renderer height is known, which needs CPR, so **the REPL
    status bar was never visible**. Removed. The pty driver now answers CPR like a real terminal.
  - ui.notice truncates long messages at the first ":" past 160 chars, which cut times like 13:44. Scheduler messages
    are now short, with details on a dim second line.
- Verified:
  - 132 tests pass (39 new: phrase parsing, cron edge cases incl. leap day/year rollover/dom-dow OR, split, store
    lifecycle, tick claiming/serialisation/reaping/missed/busy deferral/run-now, real supervisor execution ok/failed/
    timeout, CLI dispatch).
  - Live with gemma4:12b: `finch schedule run` wrote stamp.txt. The daemon ran a 2-minute recurring job and a one-off
    poem job in order (the one-off waited for the running job).
  - pty walkthrough: `/schedule <when> <task>`, list, Schedule tab, the job actions picker, the wizard with live
    preview and error, and result notices appearing in the REPL while the daemon was also running.

### Follow-up: stream-loop guard

- New trick `stream_loop_guard` (Anti-loop). `repeat_detect` only catches the same reply two turns in a row; this one catches a
  model that degenerates *inside* one generation, in the answer text or in the reasoning channel, until it burns the token cap.
- Detection (`heuristics.find_repeat` / `RepeatWatch`): the streamed tail is one unit repeated back to back (≥16 chars; 5 reps
  for units under 40 chars, 3 above; units with <6 distinct characters, such as `-----`, never count). Checked every 80
  streamed chars from the `on_token` / `on_think` callbacks, which raise `Looping`; both backends close the response on the way
  out. Tool-call arguments are not streamed through the callbacks, so a long legit `create_file` is never inspected.
- Recovery ladder in `Agent._loop` (the partial output is dropped, not added to the context): 1st/2nd hit → user-role nudge that
  quotes the loop and demands one concrete action, and the next 2 calls run with `overrides` (temperature 0.9, repeat_penalty
  1.15, and `think: false` if the loop was in the reasoning). 3rd hit → `snapshot_reset`. 4th → stop the turn. The counter clears
  after 3 clean model turns.
- `chat()` of both backends takes `overrides` (OpenAI maps repeat_penalty to `frequency_penalty`). Test fakes need the kwarg;
  the agent only passes it when it is set.
- Verified: 137 tests (5 new: detector, streaming watch, nudge+boost, reset→stop, trick off); live gemma4:12b task unaffected;
  `think: false` override confirmed against Ollama. A real degenerate loop was not reproduced live.

### Follow-up: partial-read gate

- New trick `partial_read_gate` (Finish gates). A pasted run showed the model read a file ("194 lines · truncated"), then
  summarised it with invented generic content. `read_file` already says "[truncated…]", but small models ignore a note
  inside a tool result.
- `ToolBox.partial` records which line ranges of each file the model has actually seen (a cut-off last line does not count).
  `unread_parts()` merges them and returns files with unseen lines plus the first unseen line. A `run_bash` command that
  names the file removes it from the list (a script is how big files get processed whole).
- `Agent._partial_read_nudge` runs first in `_finish_gates` (before the Q&A shortcut) and on the Q&A text-answer path.
  It queues a user-role nudge listing the files, the unseen line counts and where to continue, and suggests a script for
  binary files (xlsx/docx/pdf are read as text, so they come back as junk). Capped at 2 per turn so it can never deadlock.
- The extraction itself is the next follow-up (below).
- Live gemma4:12b on a 68 KB CSV: truncated read → the gate challenged the first prose answer; after the model counted with a
  script (run_python or `grep -c` via run_bash) it was not nagged again and gave the right count. A first live run used a file
  under the read cap (nothing truncated, correctly no gate), and a second showed `run_python` was missing from the
  script exemption (fixed). Separate, pre-existing: after a prose answer gemma keeps replying "I understand…" to the
  CONTINUE nudge instead of calling finish, until `repeat_detect` ends the turn.
- Verified: 144 tests at the time (7 new: coverage tracking, bash and run_python scripts count, gate rejects then allows, trick off).

### Follow-up: office / PDF extraction

- New trick `office_extract` (Output repair). `read_file` used to decode xlsx/docx as latin-1 zip bytes, so the model summarised
  noise (the original pasted failure). `finch/docs.py` now returns text: xlsx/xlsm (shared strings incl. rich text, inline
  strings, booleans, gaps kept as empty columns, date detection from `cellXfs`/`numFmt`, one `## Sheet:` block each, every line
  prefixed with its real row number), docx (headings as `#`, tables as `| a | b |`), pptx (slides in numeric order plus notes), pdf
  (`pypdf` if importable, else the `pdftotext` binary, else an error that says what to install). Stdlib only, no new dependency.
  Parsed text is cached by mtime so ranged reads don't re-parse. Legacy .xls/.doc/.ppt return a LibreOffice conversion hint.
- The extracted text flows through the normal `read_file` path, so line ranges, the 6000-char cap, the crawl guard and the
  partial-read gate all apply unchanged.
- Discoverability was the real catch: with only the extractor, gemma ignored `read_file` and wrote pandas/openpyxl scripts that
  failed on missing modules. Two fixes: `read_file`'s description advertises office support only while the trick is on
  (`Agent._tool_def`), and a `ModuleNotFoundError` for openpyxl/pandas/xlrd/docx/pptx/PyPDF2/pypdf/fitz/pdfplumber appends a hint
  to use `read_file` (`env_hints`).
- Bug found by the tests: `ElementTree.clear()` drops attributes, so row numbers were read after clearing and drifted.
- Verified: 152 tests; extractor on real Excel/Word files (CMSIS spec sheet: 62 rows, multi-column); live gemma4:12b read the
  xlsx via `read_file` and quoted the correct cells. Not verified: PDF (neither pypdf nor pdftotext is installed here, only the
  error path is tested), xlsx files with merged/hidden sheets or password protection.

## 6. Ideas / open items

- Scheduler: desktop notifications on job completion (notify-send / Windows toast from WSL); an agent tool so the model can
  schedule follow-ups itself; per-job model pinning in the wizard; check whether WSL keeps the daemon alive after the last
  terminal closes (untested; Windows Task Scheduler running `wsl … finch schedule tick` is a possible fallback).

- Port parallel sub-agent dispatch: a sandboxed child process per simple todo item, with fallback to running serially.
- Esc-to-interrupt (currently Ctrl+C) and expandable tool output (currently `/verbose`).
- HTTP/SSE MCP transport.
- Consider `/think off` as the default for gemma4 on simple tasks; thinking spirals cost minutes.
- Benchmark Finch vs Mingbird on a few LRAB tasks to validate parity.
- `prompt_eval_count` semantics under Ollama prompt caching should be verified; the ctx% display relies on it.

## Follow-up: thinking off works on any model

- The `think` setting was only reliable on gemma4: the OpenAI-compatible backend ignored it, gpt-oss ignores boolean `think`
  on Ollama, and models that think regardless of the flag still streamed their reasoning.
- `llm.py`: Ollama sends `"low"` for off on gpt-oss (`_think_field`); the OpenAI backend sends `reasoning_effort` ("none"/"medium"),
  retrying once without it on HTTP 400. When think is off, any thinking (channel or inline `<think>`) is dropped from the UI and the message.

## Follow-up: OS agnostic (Linux, macOS, Windows)

Goal: run Finch natively on Windows as well as Linux without configuration. All platform branching now lives in
`finch/osutil.py`; no other module tests `os.name`.

| POSIX-only before | Now |
|---|---|
| `run_bash` / `!cmd` hardcoded `/bin/bash` | `osutil.shell_path()`: bash on POSIX; on Windows Git Bash > PowerShell > cmd. `System32\bash.exe` and WindowsApps are skipped (that is the WSL launcher: commands would run on a different machine). `FINCH_SHELL` overrides. The system prompt names the shell (`shell: {shell}`), plus a one-line syntax hint for PowerShell/cmd. |
| `start_new_session` + `os.killpg` | `new_group`/`hidden_group`/`detached` Popen kwargs and `kill_tree` (`taskkill /F /T` on Windows). `run_bash` children get their own hidden console on Windows, so a code page change or ctrl+c in the child never reaches the REPL's console. |
| `fcntl.flock` on the job store | `osutil.lock`/`unlock` (`msvcrt.locking` on Windows). `os.replace` is retried (Windows refuses while a reader has the file open) and `scheduler._read` retries `PermissionError`. |
| `os.kill(pid, 0)` + `/proc/<pid>/stat` | `osutil.pid_alive`: on Windows `OpenProcess` + `GetExitCodeProcess` through ctypes. On Windows **`os.kill(pid, 0)` terminates the process**, so it is never called there. |
| Daemon stopped by SIGTERM | `stop_daemon` also writes `schedule/daemon.stop`, which the daemon polls every 0.5 s (Windows can't deliver SIGTERM to another process); SIGTERM is still sent on POSIX. A hard terminate is the fallback after 4 s. |
| Locale encodings | Children get `PYTHONUTF8=1` (`osutil.child_env`): without it, a job's log redirected on Windows is cp1252 and crashes on `✓`. Finch's own redirected stdout/stderr switch to UTF-8 (`utf8_stdio`). PowerShell/cmd are told to emit UTF-8 (PowerShell 5.1 otherwise turns `✓` into `√`). Output that isn't UTF-8 is decoded with the OEM code page on Windows. |
| `python3` everywhere | `osutil.find_python()` skips the Microsoft Store stub, which opens the Store instead of running anything. Hints say `python` on Windows (`osutil.PY`). |
| `$EDITOR.split()`, nano/vim only | `osutil.split_cmd` (an unquoted `C:\Program Files\...` path works); Notepad on Windows. |
| MCP `command: npx` | Resolved with `shutil.which`, because on Windows it is `npx.cmd`, which `Popen` won't find by name. |
| Safety | `cd C:\` / `Set-Location D:` / `pushd \` count as leaving the workdir, and backslashes count as path separators in the delete-glob and in-place-edit snapshot checks. |

- Verified on this machine: 162 tests on Linux **and natively on Windows** (Windows Python 3.11 from WSL, scratch venv in
  `%TEMP%`). On Windows: shell detection picked Git Bash rather than the WSL launcher, and python detection picked Python 3.11
  rather than the Store stub. A live gemma4:12b task wrote code and ran `python -m unittest` through Git Bash. `finch schedule
  start/status/stop` worked. A scheduled job ran end to end (detached supervisor, hidden child, UTF-8 log). PowerShell and cmd
  fallbacks returned `✓ é` intact and tree-killed on timeout. Linux: live gemma4:12b run and pty snapshot (`!cmd`) unchanged.
- Not verified: the interactive REPL in a real Windows console (prompt_toolkit supports it, but it can't be driven from WSL),
  macOS, and PowerShell 7 (`pwsh`, not installed here; same code path as 5.1). `scripts/pty_drive.py` stays Linux-only (pexpect).
- Tests that assumed Linux were made portable: `encoding="utf-8"` on reads, `sys.executable` instead of `python3`/`true`.

## Follow-up: toggleable tools and skills

Goal: every tool and skill can be switched on and off from `/settings`, like the tricks.

- `config.Settings`: `tools_off` (built-in names and `"mcp:<server>"`) and `skills_off` lists, persisted;
  `tool_on(name)` (a flat MCP tool `server.tool` follows its server) and `skill_on(name)`. Lists of what's *off*,
  so new tools and skills default to on. `LOCKED_TOOLS = ("finish",)`: the finish gates hang off it, so like the
  hard safety ring it isn't switchable (`set_tool` ignores it).
- Enforced at every entry: `agent.tool_surface` (task, chat and no-flat-prefill surfaces, MCP defs), the agent's
  dispatch (an off tool is refused before auto-enable, so it's never loaded on the fly), `_enable_tools` (reports
  "turned off by the user" and doesn't lift the ban the way it lifts anti-loop bans), and `ToolBox.run`
  after alias mapping (covers `batch_tools`, `bash`→`run_bash`, salvaged calls) plus `t_mcp_call`.
- Not a trick: tricks are harness mechanisms; these switch off capabilities. They don't go in `tricks.TRICKS`.
- Prompt cost: `enable_tools` advertises only the advanced tools that are on (`tools.enable_tools_def`); with nothing
  off it returns the original def, so the default prompt is byte-identical. The `skills` tool leaves the surface
  when every skill is off.
- Skills: `tools.skill_infos()` returns name/desc/scope/path with project > user > built-in precedence, used by the
  Skills tab, `/skills list`, and the `skills` tool (`list_skills`/`load_skill` take the off list).
- Settings tabs are now General · Tricks · Tools · Skills · Theme · Soul · Schedule; tab indices are named
  constants (`settings_menu.TOOLS` …) and `cli.open_settings` takes a tab name, so nothing hardcodes a number.
  A `locked` item kind renders `finish` as "● on always". `/tools` and `/skills` with no argument open their tab,
  and keep `list` / `<name> on|off` / `all on|off` forms (`App.toggle_command`).
- Known limitation: the anti-loop nudges name tools ("write it with create_file", "run_bash: python …") whether or not
  they're on. A model with those tools off is told to use the tool, then refused with "turned off by the user".
- **Test hazard fixed:** the `finch_home` fixture didn't patch `config.CONFIG_FILE` (or `SKILLS_DIR_HOME`), so any test
  calling `save_settings` wrote the developer's real `~/.finch/config.json`. That happened once in this session, it
  reset the user's model/theme/think and had to be restored by hand. The fixture now patches both.
- Verified: 171 tests (9 new); pty walkthrough of the Tools tab (toggle, locked finish, 18/19 count), the Skills tab,
  the General nav counts, the save on esc, `/tools list`, `/skills code_review off`, `/skills list`, and the toolbar
  "1 tool off". Live gemma4:12b with `run_bash`/`run_python` off: "Run a shell command to count the lines…" was done with
  read_file + create_file (count.txt = 4) and passed the finish gates.

## Follow-up: settings profiles

Goal: save the whole settings state under a name and switch between them.

- A profile is `{k: value for k in config.PERSISTED}` in `~/.finch/profiles/<name>.json` (`config.PROFILES_DIR`). Using
  `PERSISTED` as the schema means a new persisted setting is automatically part of profiles. Runtime-only state
  (routing override, timebox, `unattended`) is deliberately not in it, and `load_profile` drops any key outside
  `PERSISTED`, so a hand-edited profile can't switch on `unattended`/`allow_env_mutation`.
- `config`: `save_profile` (validates the name: `[\w][\w.-]{0,47}`, plus Windows reserved names like `con`),
  `load_profile` (ValueError on missing/unreadable), `apply_profile` (deep copies; missing keys keep their current value),
  `profile_diff` (lists compared as sets, so `tools_off` order doesn't matter), `delete_profile`, `list_profiles`.
- Loading in the REPL is `App.load_profile`: it applies the profile, rebuilds the backend when host/backend/URL/key changed
  (`agent.llm` too), re-applies the theme and verbose, updates the session model, and saves config.json. So "load" means
  "make this my config". `--profile` at startup applies before the other flags and doesn't save.
- Settings tab 8 (appended so the 1–7 keys didn't move again): save action (name prompt, overwrite confirmation), one
  row per profile with a summary and "✓ current", and enter → `choose()` Load / Update / Delete. Uses the same
  `*_request` + `application.exit` pattern as souls and jobs. `/profile` mirrors it.
- Not done: scheduled jobs can't name a profile (they keep their own model/soul overrides and otherwise use config.json).
- Verified: 183 tests (12 new); pty: save two profiles, `/profile list` (current + "differs: num_ctx, theme"), Profiles tab
  render, load from the tab (ctx 64K→32K, hermes→claude live), `/profile big`, unknown name error, config.json updated.
  `finch --profile work -p` ran headless and left config.json unchanged; `--profile nope` exits 1 with a message.

## Follow-up: custom system prompt

Goal: let the user change the system prompt from General settings.

- `Settings.system_prompt` (text) and `system_prompt_mode` (`builtin` | `append` | `replace`), both in `PERSISTED`, so
  profiles pick them up. Keeping the mode separate from the text lets the user switch back to built-in without losing it.
- `Agent.system_prompt`: `append` adds `# Custom instructions` after the built-in prompt in both modes; `replace` swaps
  out `SYSTEM` for task turns only. Chat keeps `CHAT_SYSTEM` because a replacement is usually an edited task prompt that
  mentions todo/finish, tools chat mode doesn't have. Placeholders are filled by `str.replace` from `prompt_vars()`, not
  `str.format`, so stray braces in user text can't raise. The soul, FINCH.md, shell hint and lang hint still layer on.
- Settings: three General rows (mode choice, "Edit system prompt…" action, reset). Edit uses the `application.exit`
  pattern (result `"sysprompt"`) → `edit_system_prompt()`: temp file + `souls.editor_cmd()`, falling back to a multiline
  prompt. In replace mode an empty text is pre-filled with the raw `SYSTEM` template. Saving text while in `builtin`
  switches to `append`; saving empty text goes back to `builtin`. Action rows can now show a value before "↵".
- Not a trick: it's a user setting, not a harness mechanism.
- Verified: 185 tests (2 new); pty: rows render, edit via a fake `$EDITOR` saves and flips to append, replace mode
  pre-fills the built-in prompt, esc persists to config.json. Live gemma4:12b with an appended "end every reply with
  BANANA" rule: chat answer ended with BANANA.

## Follow-up: trick explanations

Goal: the one-line trick descriptions were shorthand ("60% truncate · 75% summarise · 90% rebuild") that only made
sense if you'd read Mingbird. Users need to know what each trick is for before they switch it off.

- `tricks.DETAILS` maps every key to a 2–4 sentence explanation: the small-model failure it addresses, what Finch does,
  and what changes when it's off. Written from the call sites in agent/tools/safety (thresholds and caps match the code).
  The `TRICKS` tuples are unchanged, so existing `for key, grp, label, desc in TRICKS` unpacking still works.
- Settings: `Item.detail`; the footer shows the short desc (highlighted) and then the detail, both wrapped to the
  terminal width with an indent. The list height is now fitted to what the tab bar, footer and help line actually use
  (including their wrapping and the extra blank line before each group header). Before, it reserved a fixed 9 rows, so
  the help line was already being pushed off on tabs with several group headers, and longer text made it worse.
- `/tricks <key>` prints label, group, state, short desc, explanation and the toggle command.
- Test: every key has a detail of at least 120 chars.
- Verified: pty: Tricks tab at 110x50 shows desc + explanation + help without overflow; `/tricks stream_loop_guard`.
  The pre-existing uncommitted `test_custom_system_prompt_modes` failure is unrelated (system prompt starts with
  the built-in "You are an autonomous agent…", not "You are Finch").

## Follow-up: stalled streams ("stream error: timed out")

Symptom: `⚑ API error: stream error: timed out (retry 1/8 in 3s)`, seemingly at random, always when the model was about
to write a big file (an Asteroids clone on qwen3.6 35b).

Cause: `api_timeout` (240 s) was the urlopen socket timeout, i.e. the limit on silence between *any* two reads, not
just the first byte. Ollama's tool-call parser buffers the whole call and sends it as one chunk when it is complete,
so nothing at all arrives while the model generates a large `create_file`. Measured with gemma4:12b: a ~150-line file
is 60–70 s of silence; a 35B model writing a full game easily passes 240 s. In real agent runs gemma opens with the
tool call (zero content tokens), so the silence starts before the first byte, and a "first byte vs mid-stream"
split doesn't help: the whole reply is one silence. The retry then sent the identical request, regenerated the same
oversized call and timed out again.

- `Settings.stream_timeout` (900 s, persisted, General tab "Stall timeout") replaces `api_timeout` for every
  streamed request in both backends. A legacy `api_timeout` in config.json is used as a minimum.
- `llm._no_data`: any socket timeout → `LLMError(stalled=True)` ("no response … for Ns" before the first byte,
  "stream stalled: no data … for Ns" after it). The OpenAI backend now converts mid-stream OSErrors too (it used to
  let a raw TimeoutError escape).
- Trick `stall_feedback` (Output repair): on a stalled error, no back-off sleep; drop the previous stall note and add
  a stricter one ("happened N times in a row", create_file with at most 150/N lines, min 40, then append_file).
  Counts toward the 8-failure cap; `stalls` resets on a successful call. Off: the old back-off retry.
- Tests: a local HTTP server that pauses mid-stream (both backends: survives a 1 s pause under a 5 s limit, raises
  stalled under 0.4 s); stall nudge without sleep; escalation text; trick off → plain retry; legacy key migration.
- Verified live with gemma4:12b and `stream_timeout` 40 s (so a whole-file write stalls): before escalation the model
  ignored the nudge 4 times, then wrote 74 lines + append_file (294 s). With escalation it switched after one stall
  each time: 55 lines + append_file, 136-line snake.py compiles (175 s). pty: "Stall timeout ‹ 900s ›" on General.


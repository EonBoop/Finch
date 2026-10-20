# 🐦 Finch

*Vibe coded terminal agent built for local models using tricks pioneered by Mingbird (https://github.com/Mingbird/Mingbird-agent).
No I will not be maintaining this, and frankly I got claude to make this mostly to take advantage of someone elses genuine work. Do with it as thee will, and I hope it makes your life a little easier.*

A terminal agent harness for **small local models**. It carries over the harness-level tricks from
[Mingbird](https://github.com/Mingbird/Mingbird-agent) and puts them in a terminal UI modelled on Claude Code:
streamed markdown, `●` tool lines with `⎿` results, a live spinner, permission prompts, a todo
checklist, slash commands and `@file` mentions.

```
╭──────────────────────────────────────────────────────────╮
│  ✻ Welcome to Finch  v0.1.0                              │
│    cwd:     ~/workspace/myproj                           │
│    model:   gemma4:12b                                   │
│    engine:  ollama 0.40.1 @ http://127.0.0.1:11434       │
╰──────────────────────────────────────────────────────────╯
❯ add a --json flag to cli.py and test it
  task mode · tools: code, file
✻ Thought for 2.1s
● Update Todos
  ⎿ ☐ Read cli.py
    ☐ Add --json flag
    ☐ Write test_cli.py and run tests
● Read(cli.py)
  ⎿ Read 84 lines
● Update(cli.py)
  ⎿ Updated with 3 line(s) replacing 1
    - parser.add_argument("path")
    + parser.add_argument("path")
    + parser.add_argument("--json", action="store_true")
● Bash(python3 -m pytest -q)
  ⎿ ...                                                  [100%]
    3 passed in 0.02s
  ↳ running tests before accepting finish…
  ✓ tests pass ✓
● Added a --json flag …
  ✻ done in 48s
```

## Install

```bash
cd finch
python3 -m venv .venv && .venv/bin/pip install -e .
export OLLAMA_HOST=http://0.0.0.0:11434      # 0.0.0.0 is rewritten to 127.0.0.1 automatically
.venv/bin/finch -m gemma4:12b                # interactive
.venv/bin/finch -m gemma4:12b -p "task"      # headless: one task, then exit (non-zero if not finished)
```

On **Windows** (native, no WSL), from PowerShell or cmd:

```powershell
cd finch
py -m venv .venv; .venv\Scripts\pip install -e .
.venv\Scripts\finch -m gemma4:12b          # Ollama for Windows listens on 127.0.0.1:11434 by default
```

Finch runs the same on Linux, macOS and Windows; nothing needs configuring. What differs:

- **Shell.** `run_bash` (and `!cmd` in the REPL) uses bash on Linux/macOS. On Windows it uses **Git Bash** if
  [Git for Windows](https://git-scm.com/download/win) is installed (recommended: small models write bash), otherwise
  PowerShell, otherwise cmd. In those last two cases the model is told which shell it has. It never uses
  `System32\bash.exe`, which runs commands inside WSL instead of on Windows. To choose a shell yourself, set `FINCH_SHELL`
  (`FINCH_SHELL=pwsh`, or a full path).
- **Python.** `run_python` and the test guard use `python3` on Linux/macOS and `python`/`py` on Windows, skipping the
  Microsoft Store `python3.exe` stub.
- **Editor / pager.** `$VISUAL`/`$EDITOR` (paths with spaces work), else nano/vim on Linux and Notepad on Windows.
  Without `less`, logs are shown inline.
- Config lives in `~/.finch` on every platform (`C:\Users\<you>\.finch` on Windows); `FINCH_HOME` overrides it.

Options: `-C dir` (working directory), `--ctx 65536`, `--think on|off|auto`, `--timebox 40m`, `--offline`,
`--resume [id]`, `-y` (auto-approve permission prompts; the hard safety rings stay on), `-v`.
Scheduled jobs from the shell: `finch schedule add|list|run|pause|resume|rm|log|when|test|start|stop|status`
(see [Scheduled jobs](#scheduled-jobs)).

### In the REPL

| | |
|---|---|
| `/settings` | **settings menu**: General · Tricks · Tools · Skills · Theme · Soul · Schedule · Profiles. The "›" rows on General (or keys 1–8, tab) open the other tabs; ↑↓ move, ←→ change, enter select, backspace back, esc save & close |
| `/theme [name]` | pick a theme (↑↓ live preview, enter select, esc cancel) or switch directly. 18 themes in three groups: **Finch** (claude, ocean, forest, dracula, solarized-light, high-contrast, mono), **Hermes Agent** (hermes, ares, poseidon, charizard, slate: Hermes's palettes, glyphs like ☤ and `┊`, and its spinner faces/verbs) and **Editor palettes** (nord, gruvbox, tokyo-night, catppuccin, monokai, rose-pine) |
| `/soul` | switch persona (interactive Soul tab) · `/soul <name>` · `/soul off` · `/soul list` · `/soul show` · `/soul new <name>` · `/soul edit [name]` |
| `/schedule` | scheduled jobs (interactive Schedule tab) · `/schedule <when> <task>` · `/schedule new` (guided) · `list` · `run` · `pause` · `resume` · `rm` · `log` · `when <job> <when>` · `daemon start\|stop\|status` |
| `/profile` | settings profiles (interactive Profiles tab) · `/profile <name>` loads one · `/profile save <name>` · `list` · `rm <name>` |
| `/tricks` | interactive trick toggles · `/tricks list` prints them · `/tricks <key>` explains one · `/tricks <key> on\|off` · `/tricks all off` for a raw model |
| `/tools` | interactive tool toggles (Tools tab) · `/tools list` · `/tools run_bash off` · `/tools <mcp server> off` · `/tools all on` |
| `/skills` | interactive skill toggles (Skills tab) · `/skills list` · `/skills bug_fix off` · `/skills all off` · `/skills new <name>` · `/skills import <file>` · `/skills edit [name]` · `/skills rm [name]` |
| `/model [name]` | pick or switch model (shows size + tools/thinking/vision capabilities) |
| `/think on\|off\|auto` | thinking tri-state (`auto` = don't send the field; model default) |
| `/ctx 64k` | context window (`num_ctx`) |
| `/mode auto\|task\|chat` | override Q&A/task routing |
| `/timebox 40m` | time budget per task (also detected in task text: "timebox 40 minutes", "within 1.5 hours") |
| `/todo` `/undo` | show the plan · roll back the agent's last file change |
| `/compact` `/clear` | summarise now · new session |
| `/sessions` `/resume` | saved sessions (`~/.finch/sessions/`) |
| `/cd` `/memory` | |
| `/offline` `/unsafe` `/verbose` `/status` `/init` | |
| `!cmd` | run a shell command; its output is added to the conversation |
| `@path` | mention a file: text is inlined, images are attached |
| `esc+enter` | newline · **ctrl+c** interrupts a running turn |

## Mingbird's tricks, and where they live

| Small models can't… | Finch does it for them | Where |
|---|---|---|
| …fit all tools in context | **Flat prefill**: 9 core tools with minimal schemas (no per-param descriptions); categories (file/code/web/memory/mcp) loaded by keyword routing; advanced tools via `enable_tools`; skills listed inline (name + one line) in the `skills` tool, so one call loads the right one (**inline skill index**; off = list first, then load), and the task prompt says to load a fitting skill before planning (**skill-first rule**). A skill can also be **preloaded**: one with `preload: agent` is added to the task when routing picks that category, so "use subagents…" starts with the `delegate` steps. A known tool that isn't loaded is loaded on the fly instead of refused. | `heuristics.route_categories`, `agent.tool_surface` |
| …tell chit-chat from work | **Q&A/task layering**: questions get a chat system prompt and read-only tools, answer once and stop; write/run calls are blocked in chat mode. Short follow-ups continue an in-progress task. | `heuristics.is_qa`, `agent._loop` |
| …escape tool loops | **Signature-level anti-loop ladder**: identical consecutive call is blocked (3×); 5-in-a-row nudge; 8 in a row with 4 identical → tool disabled; calling a disabled tool: nudge at 3, **context hard reset** at 10. Research guard counts only *repeated identical* queries. Todo guard. Per-tool failure ladder: 3 fails → strategy hint (edit_file → disabled, rewrite with create_file). | `agent._loop` |
| …stop circling on one file | **Shotgun-edit guard**: 4 successful edits of the same file without convergence → "find the root cause first" (2 hints max, never disables). | `heuristics.EditConvergenceGuard` |
| …recover from empty output | **Empty-turn ladder**: nudge at 3, hard reset to task + plan + file listing at 6 (max 3 resets), then graceful exit. Unclosed `<think>` counts as empty. Truncation at the token cap gets **"skeleton then append"** chunking feedback. A reply that **stalls** (no data for `stream_timeout`, usually one huge tool call) gets "write it in pieces" feedback, stricter each time, instead of an identical retry. | `agent._loop` |
| …emit valid tool calls | **Format-leak salvage**: JSON in text, ```json fences, `<tool_call>` tags, gemma `call:name{…}`, placeholder quotes `<\|"\|>`, bare `finish(summary="x")`. **Loose salvage** also takes the name under `action`/`function`/`tool` with flat arguments (`{"action": "list_dir", "path": "."}`) and Python `None`/`True`/`False`. Tool-name aliases (`write_file`, `bash`, …) and argument aliases (`file_path`, `cmd`, …). Missing-argument errors include a correct example. | `heuristics.try_parse_tool_calls`, `tools.ALIASES` |
| …self-debug | **Test guard**: when tests exist, the harness runs them itself before accepting `finish` and feeds back the exact failure (`file:line` + the `E` line). pytest → unittest fallback → skip if Python is missing. A broken third-party pytest plugin is detected and retried with autoload off. | `heuristics.run_test_guard` |
| …stay honest at the end | **Finish gates**, each capped so they can't deadlock: fake finish (no work done) · test guard · claimed files must exist somewhere below the workdir (subdirs, `build/`, `dist/` and dot dirs included) · files named in the plan must exist · plan must be ticked · **delivery self-check** re-injects the original task once. An empty final summary triggers a wrap-up call. | `agent._finish_gates` |
| …edit code safely | **Rollback everywhere**: backup before every write (`.finch/backups/`), including in-place shell edits (`sed -i`, `>` redirects) detected in commands, ignoring redirects that write no file (`2>/dev/null`, `2>&1`, `>&2`); `delete_file` goes to `.finch/trash/`; **clobber guard** refuses to overwrite a big file with a much smaller one unless `replace=true`; `/undo`. | `tools.Rollback`, `tools.bash_snapshot` |
| …write escaped code | **Auto-repair**: a `.py` file that fails to compile because of literal `\n` sequences is unescaped (kept only if it then compiles). Edits retry with unescaped `old` text. | `tools.auto_repair` |
| …crawl huge files | **Crawl guard v2**: byte budget (2× file size, clamped 64KB–512KB). It never refuses: past the budget it throttles to 2000 chars/read and teaches the "one script" approach. Nudges at reads 4 and 10. | `tools.ToolBox.t_read_file` |
| …manage long contexts | **Tiered compaction** by preflight estimate *and* actual `prompt_eval_count`: 60% → truncate old tool outputs, 75% → summarise older turns, 90% → full rebuild. The summary carries the verbatim task, the live plan and the workdir listing. Overflow errors force L3. Tiers re-arm when usage drops. | `agent.compact` |
| …keep the plan alive | Stale-plan nudge after 4 productive steps without a todo update. | `agent._loop` |
| …survive a flaky server | 500s → compact + inject a path-breaking message (same messages on a degraded runner reproduce the same failure); exponential backoff; optional restart hook `FINCH_OLLAMA_RESTART_CMD` after 4 consecutive 500s. `keep_alive=30m` is sent top-level so the model isn't unloaded mid-task. Bash runs in its own process group and the whole tree is killed on timeout. | `agent._loop`, `llm.py` |
| …be trusted with your disk | **Safety rings**: ① irreversible ops refused outright (`rm -rf /`, `mkfs`, `dd of=/dev/sd*`, `curl … \| sh`, `shutdown`…) · ② uninstalls / env mutation need confirmation, and are denied when unattended; recursive deletes only inside the workdir · ③ out-of-workdir paths and sensitive files (`.ssh`, `.env`, keys…) need confirmation; anything below the workdir needs none, including absolute paths to it in shell commands and sensitive-named files you put in the project (unless the workdir is your home dir); sensitive writes are always refused · ④ rollback (above). `/unsafe` bypasses ring ② only. | `safety.py` |
| …manage wall-clock time | Timebox pacing nudges at 50/75/90%. | `heuristics.budget_nudge` |
| …read office files | `read_file` extracts text from **.xlsx / .xlsm / .docx / .pptx** with the standard library (spreadsheets come back as numbered rows with dates formatted, one `## Sheet:` block per sheet) and from **.pdf** via `pypdf` or `pdftotext` if either is installed (otherwise it says how to get one). Old .xls/.doc/.ppt get a LibreOffice conversion hint. A script that dies on `import openpyxl/pandas/docx…` is told to use `read_file` instead. | `docs.py`, `tools.ToolBox.t_read_file` |
| …see | `read_file` on an image attaches it to the next request (vision models); `@image.png` in the prompt too. | `tools._read_image` |
| (engines) | Ollama native (thinking tri-state, top-level `think`/`keep_alive`, 400-retry without `think`) or any OpenAI-compatible server (`"backend": "openai"` in config). Inline `<think>` tags are split out of content, even when torn across stream chunks. | `llm.py` |
| (small-model protocol) | ≤4B models: edit_file disabled up front, bash output trimmed to the tail, shell-vs-python teaching hint. | `agent.run_turn` |
| (extensibility) | Skills (`.finch/skills/`, `~/.finch/skills/`, built-in), MCP stdio servers in `~/.finch/mcp.json` exposed as flat `server.tool` tools, long-term memory, `FINCH.md` project notes. | `tools.py`, `mcp.py` |

**Interactive deviation:** in the REPL, a task-mode reply that ends with a question is handed back to
you, so the agent can ask for clarification. Mingbird would push it with "Continue:". Headless `-p`
mode keeps Mingbird's behaviour.

**Not ported:** Mingbird's parallel sub-agent dispatch (off by default there), the voice input, the GUI-only
features, and HTTP MCP transports (only stdio is supported).

## Souls (SOUL.md)

A soul is a short markdown persona placed at the top of **every** system prompt, in both task and chat
mode. It sets voice and priorities. The task rules still follow it, because the tricks depend on them.
Pick one from the Soul tab in `/settings`, with `/soul <name>`, with `--soul name` at startup, or with
`FINCH_SOUL`. The choice is saved.

| Where | Scope |
|---|---|
| `<workdir>/.finch/souls/<name>.md` | project souls (win over the same name elsewhere) |
| `~/.finch/souls/<name>.md` | your souls (`/soul new <name>` creates one from a template and opens `$EDITOR`) |
| `finch/builtin_souls/` | built-in: **concise** (Hermes-style directness), **mentor**, **careful**, **friendly**, **pirate** |
| `<workdir>/.finch/SOUL.md`, `~/.finch/SOUL.md` | appear as the souls **project** and **personal** |

```markdown
---
description: one line shown in the picker
---
Be direct: match the length of your reply to the weight of the ask…
```

Every character is sent with every request, so the picker shows each soul's token cost. Souls are capped at
4,000 characters. Editing a built-in soul copies it to `~/.finch/souls/` first.

## Custom system prompt

`/settings` → General → **System prompt** chooses how your own text is used, and **Edit system prompt…** opens it in
`$VISUAL` / `$EDITOR`:

| Mode | Effect |
|---|---|
| built-in | Finch's own prompt only (your text is kept but not sent) |
| built-in + yours | your text is added under `# Custom instructions` in task **and** chat mode |
| yours | your text replaces the task-mode prompt; chat mode keeps its short built-in prompt |

In "yours" mode an empty prompt starts as a copy of the built-in one, so you can change it rather than start from
nothing. Keep the tool rules (todo, enable_tools, finish) if you do: small models lean on them. `{workdir}`, `{home}`,
`{os}`, `{shell}`, `{python}` and `{date}` are filled in. The soul, FINCH.md and "Force English" still apply on top.
**Reset system prompt** deletes your text. Both are saved in config.json (`system_prompt`, `system_prompt_mode`) and
in profiles.

## Scheduled jobs

Finch can run a task later, once or on a schedule. A job is a task, a folder and a schedule:

```text
/schedule in 10m remind me to stretch
/schedule every weekday at 8:30 summarise yesterday's commits in this repo into standup.md
/schedule friday at 17:00 -- tidy the downloads folder        (" -- " separates schedule and task explicitly)
finch schedule add "daily at 9am" "check the build and write status.md" -C ~/proj
```

| Schedule | Examples |
|---|---|
| one-off | `in 10m` · `in 1h30m` · `at 17:30` · `5pm` · `tomorrow 9am` · `friday at 08:15` · `oct 20 at 9am` · `2026-10-20 09:00` |
| recurring | `every 30m` · `every 2 hours` · `hourly` · `daily at 9am` · `weekdays at 8:30` · `weekends at 10am` · `every mon, wed and fri at 7pm` · `weekly on friday at 5pm` · `monthly on the 1st at 9am` |
| cron | `cron 0 9 * * 1-5` or any bare 5-field expression (names, ranges, lists and steps work) |

Times are local. A time of day that has already passed means tomorrow, and a day without a time means 09:00.
`finch schedule test "<phrase>"` (or the live preview in `/schedule new`) shows how a phrase is read and its next runs.

**Managing jobs.** Use the Schedule tab in `/settings` (key 7) or `/schedule` with no arguments. It lists every job with its
next run and last result. Enter on a job opens: run now, pause/resume, view last log, edit task, change schedule, change
folder, toggle auto-approve, delete. "Background daemon" switches the daemon on and off.

**How jobs run.** A due job runs headless, exactly like `finch -p "<task>" -C <folder>`, with your current settings
(model, soul, tricks), a 30-minute timebox and a 2-hour hard limit. Output goes to `~/.finch/schedule/logs/<job>/`
(the last 30 runs are kept). When a job finishes, the next prompt in any open Finch window shows its result, and the
status bar shows the next or running job.

- **Who runs them:** any open Finch window checks every 15 s. To run jobs with no window open, start the daemon:
  `finch schedule start` (or the toggle in the Schedule tab). Windows and the daemon can all be active: a job is
  claimed under a file lock, and **only one job runs at a time** so jobs don't fight over the GPU.
- **While you work:** a due job waits until your current task in a Finch window finishes (at most 30 minutes).
- **Missed runs:** a recurring job that was missed runs once on catch-up, then continues from now (no backfill). A one-off
  job runs late if it's less than 24 hours overdue, otherwise it's marked missed.
- **Safety:** nobody is there to answer permission prompts, so risky actions (installs, deletes outside the project,
  system changes) are **denied** unless the job has auto-approve on. The hard safety ring is always on.
- **Windows:** the daemon and job runs are detached background processes with no console window, so closing the
  terminal doesn't stop them. `finch schedule stop` works the same as on Linux.
- **WSL:** the daemon only runs while the WSL VM is up. If Windows shuts WSL down when no terminal is open, jobs wait
  until a window or the daemon is running again (then the catch-up rules above apply).

## Toggleable tricks

Every mechanism in the table above is a named **trick** (`finch/tricks.py`, 41 of them in 7 groups) that
can be switched off. Use the Tricks tab in `/settings`, `/tricks <key> off`, `"tricks": {"crawl_guard": false}`
in config.json, or `FINCH_ABLATION=key1,key2` (Mingbird's ablation names `finish_gate`, `anti_loop` and
`flat_prefill` are accepted as aliases). "Disable all tricks" gives you a plain agent loop for comparison.
The toolbar shows how many tricks are off.

Not sure what a trick does? Select it in the Tricks tab: under the one-line summary is a plain-language
explanation of the problem it solves, what it does, and what changes when you turn it off. `/tricks <key>`
(e.g. `/tricks crawl_guard`) prints the same explanation. **The hard safety ring (irreversible commands) can't be
turned off.**

## Toggleable tools and skills

Every built-in tool, every MCP server and every skill can be switched off too: the Tools and Skills tabs in
`/settings` (keys 3 and 4), `/tools <name> off`, `/skills <name> off`, or `"tools_off": ["run_bash", "mcp:fs"]` /
`"skills_off": ["bug_fix"]` in config.json. A tool that is off is left out of every prompt (task and chat mode),
can't be loaded by `enable_tools`, auto-loading or `batch_tools`, and a call to it gets "turned off by the user".
An MCP server is switched as a whole (all its `server.tool` tools). A skill that is off disappears from
`skills list` and can't be loaded; with every skill off the `skills` tool itself leaves the prompt.
**`finish` can't be turned off**: the finish gates depend on it. The toolbar shows how many tools are off.

## Subagents

The agent can hand a self-contained piece of work to a **subagent** with `spawn_agent(task="…")`. The subagent is a
fresh agent with an empty context and the full harness (plan, finish gates, test guard, anti-loop ladders,
compaction). It works in the same directory with the same model, and only its final summary comes back to the
parent. That keeps the parent's context small: reading ten files for one answer costs the parent a few lines,
not ten file dumps, which matters on a 32K window.

```
● Agent(Read a.txt and give a one-sentence summary…)
    ● Read(a.txt)
      ⎿  Read 2 lines
  ⎿  subagent finished (3 steps, 19s)
     The text describes a fox jumping over a dog and notes that foxes are clever animals.
```

- Ask for it in the task ("use a subagent to…", "delegate the tests to a subagent") and the tool is loaded up front.
  Otherwise the model can load it with `enable_tools`, like the other advanced tools.
- The subagent can't see the conversation, so the parent has to put everything it needs into `task`. It can't ask
  questions either: nobody answers it, so it decides for itself and reports back.
- **Limits:** one level deep (a subagent can't start its own), at most 6 per turn, and 40 model calls each
  ("Subagent step limit" on the General tab, `subagent_steps` in config.json). A subagent that hits the limit is
  stopped and the parent is told what it got done.
- **Shared, not separate:** safety confirmations go to you as usual, and an "allow for the rest of this turn"
  answer covers the subagent too. Its file writes are in the same backup journal, so `/undo` reverts them.
  ctrl+c stops the subagent and the parent turn together.
- Subagents run one at a time, not in parallel. A local model serves one request at a time anyway, and parallel
  subagents would race on the same files.
- Each subagent's transcript is saved to `~/.finch/sessions/subagents/` (named after the parent session), and
  `/resume` doesn't list them.
- Turn it off like any tool: the Tools tab, or `/tools spawn_agent off`.
- The built-in `delegate` skill tells the model how to split work and brief subagents (see below). It only
  shows up where `spawn_agent` can be used, so it's hidden inside subagents and when the tool is off.

## Built-in skills

| Skill | For |
|---|---|
| `bug_fix` | reproduce, locate, minimal fix, verify |
| `test_driven` | tests first, then the code, until green |
| `python_project` | layout and conventions for a small Python project or CLI |
| `code_review` | bugs, edge cases and clarity, most severe first |
| `data_analysis` | CSV/JSON through one script instead of reading rows |
| `web_research` | several search angles, fetch, a sourced report |
| `explore_codebase` | map an unfamiliar project (README, search, tests) before changing it |
| `long_input` | a file, PDF or log too long for the context: search first, else read in chunks and take notes |
| `long_output` | a long report or file written in sections so it doesn't get cut off |
| `multi_file_change` | rename a function/class/setting across files, verified by search and tests |
| `bulk_files` | move, sort or rename many files with a dry-run script first |
| `delegate` | split big work into independent parts, one subagent each, with a GOAL / CONTEXT / OUTPUT / DONE WHEN / DON'T brief |

They're written for small models: each is a short numbered checklist under 1000 characters that names the real
tools, and the descriptions are kept distinct because the model chooses by description alone. The inline index
lists up to 16 skills, so a few of your own still fit without a list round-trip.

## Custom skills

A skill is a markdown file of step-by-step instructions the model loads when a task fits. The model picks
skills by their one-line `description`, so make it say when to use the skill:

```markdown
---
name: standup
description: Write the daily standup report from the git log
---
# Standup report
1. Run `git log --since=yesterday --oneline` in the project.
2. Write STANDUP.md with three headings: "## Yesterday", "## Today", "## Blockers".
3. Verify: read STANDUP.md back before calling finish.
```

Skills are looked up in `<project>/.finch/skills/` first, then `~/.finch/skills/`, then the built-ins. A skill
with the same name as a built-in replaces it. To add one:

- **`/skills new standup`**, or *Create a new skill…* on the Skills tab: asks for the description and where the
  skill should live (your skills, or this project's), writes a template and opens it in `$VISUAL` / `$EDITOR`.
- **`/skills import path/to/file.md`**, or *Import a skill file…*: copies an existing `.md` / `.txt` file in. A
  Claude-style `pdf-tools/SKILL.md` is imported under its frontmatter `name`.
- **`/skills edit [name]`**, or *Edit a skill…*: opens it in your editor. Editing a built-in copies it to
  `~/.finch/skills/` first, and the copy is used from then on.
- **`/skills rm [name]`**, or *Delete a skill…*: deletes one of your skills, after you confirm. If you delete your
  copy of a built-in, the built-in is used again. Built-ins themselves can only be switched off.
- Or just drop a `.md` file into either folder; it shows up on the next turn.

Keep skills short and numbered: small models follow a short checklist much better than prose.

A skill that only works with certain tools can say so with `requires: spawn_agent` (comma-separated tool names) in
its frontmatter. It is left out wherever one of those tools can't be used: switched off, a web tool in offline
mode, or `spawn_agent` inside a subagent. That way the model never loads a skill it can't follow.

Small models rarely look up a skill on their own. `preload: web` (a routing category: `file`, `code`, `web`,
`memory`, `mcp` or `agent`) adds the skill's steps to the task whenever routing picks that category, and the task
header shows `skill: <name>`. Use it sparingly: a preloaded skill costs its full text on every matching task. The
**Skill preload** trick switches it off.

## Settings profiles

A profile is a named snapshot of everything `/settings` saves: model, context window, output cap, thinking, temperature,
keep-alive, command timeout, stall timeout, theme, soul, tricks, tools, skills, offline, verbose, language, and the model server
(host, backend, OpenAI-compatible URL). Profiles live in `~/.finch/profiles/<name>.json`.

- **Save:** `/profile save <name>`, or "Save current settings as a profile…" in the Profiles tab (key 8). Saving
  under an existing name overwrites it (the tab asks first).
- **Load:** `/profile <name>`, or enter on a profile in the Profiles tab → Load. It applies at once (theme, model,
  and model server included) and becomes your config.json, so it's also what scheduled jobs use from then on.
  Keys a profile doesn't contain keep their current value, so hand-written partial profiles work.
- **For one run:** `finch --profile <name>` (with `-p` too) starts with a profile without changing config.json.
  Other flags (`-m`, `--ctx`, …) still override it.
- `/profile list` and the Profiles tab mark the profile that matches the current settings and say what the others
  change ("differs: num_ctx, theme"). Enter on a profile also offers "Update it with the current settings" and delete.

## Config

`~/.finch/config.json` (written by `/settings` and by `/model`, `/ctx`, `/think`, `/offline`, `/tools`, `/skills`):

```json
{"model": "gemma4:12b", "num_ctx": 32768, "think": null, "host": "http://127.0.0.1:11434", "theme": "claude",
 "tricks": {"self_check": false}, "tools_off": ["web_fetch"], "skills_off": [],
 "backend": "ollama", "openai_base_url": "http://127.0.0.1:1234/v1",
 "num_predict": 8192, "keep_alive": "30m", "bash_timeout": 120, "stream_timeout": 900}
```

`stream_timeout` (General tab: "Stall timeout") is how long a reply may go without sending any data. Ollama sends
nothing at all while the model writes a tool call, so writing a whole program in one `create_file` on a slow or large
model can be silent for minutes. When the limit is hit you see `reply stalled for 900s … asking for smaller pieces`
and the model is told to write the file in parts (the `stall_feedback` trick). If big models still stall, raise it.
The old `api_timeout` key (240 s, which caused random `stream error: timed out` on big writes) is read as a
minimum for `stream_timeout` and otherwise ignored.

Env overrides: `OLLAMA_HOST`, `FINCH_SOUL`, `FINCH_MODEL`, `FINCH_CTX`, `FINCH_THINK`, `FINCH_TEMP`, `FINCH_OFFLINE=1`,
`FINCH_THEME`, `FINCH_UNSAFE=1`, `FINCH_ALLOW_ENV_MUTATION=1`, `FINCH_OLLAMA_RESTART_CMD`, `FINCH_HOME`, `FINCH_SHELL`,
`FINCH_ABLATION=<trick keys or Mingbird ablation names>` (for experiments).

MCP (`~/.finch/mcp.json`):

```json
{"servers": {"fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
                    "keywords": ["files"], "always": false}}}
```

## Tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests
```

207 tests, no backend needed; they pass on Linux and natively on Windows. For the terminal UI itself, `scripts/pty_drive.py` runs the REPL in a pseudo-terminal and
prints the screen (see its docstring). The suite includes a scripted fake model that drives the full loop through the finish gates, the
anti-loop ladder, the empty-turn ladder and the chat mode. It doesn't need a running backend.

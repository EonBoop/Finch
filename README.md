# 🐦 Finch

*Vibe coded terminal agent built for local models using tricks pioneered by Mingbird (https://github.com/Mingbird/Mingbird-agent).*

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

Options: `-C dir` (working directory), `--ctx 65536`, `--think on|off|auto`, `--timebox 40m`, `--offline`,
`--resume [id]`, `-y` (auto-approve permission prompts; the hard safety rings stay on), `-v`.

### In the REPL

| | |
|---|---|
| `/settings` | **settings menu**: General · Tricks · Theme · Soul. "Harness tricks ›", "Theme ›" and "Soul ›" rows (or keys 1–4, tab) open the other tabs; ↑↓ move, ←→ change, enter select, backspace back, esc save & close |
| `/theme [name]` | pick a theme (↑↓ live preview, enter select, esc cancel) or switch directly. 18 themes in three groups: **Finch** (claude, ocean, forest, dracula, solarized-light, high-contrast, mono), **Hermes Agent** (hermes, ares, poseidon, charizard, slate: Hermes's palettes, glyphs like ☤ and `┊`, and its spinner faces/verbs) and **Editor palettes** (nord, gruvbox, tokyo-night, catppuccin, monokai, rose-pine) |
| `/soul` | switch persona (interactive Soul tab) · `/soul <name>` · `/soul off` · `/soul list` · `/soul show` · `/soul new <name>` · `/soul edit [name]` |
| `/tricks` | interactive trick toggles · `/tricks list` prints them · `/tricks <key> on\|off` · `/tricks all off` for a raw model |
| `/model [name]` | pick or switch model (shows size + tools/thinking/vision capabilities) |
| `/think on\|off\|auto` | thinking tri-state (`auto` = don't send the field; model default) |
| `/ctx 64k` | context window (`num_ctx`) |
| `/mode auto\|task\|chat` | override Q&A/task routing |
| `/timebox 40m` | time budget per task (also detected in task text: "timebox 40 minutes", "within 1.5 hours") |
| `/todo` `/undo` | show the plan · roll back the agent's last file change |
| `/compact` `/clear` | summarise now · new session |
| `/sessions` `/resume` | saved sessions (`~/.finch/sessions/`) |
| `/cd` `/tools` `/skills` `/memory` | |
| `/offline` `/unsafe` `/verbose` `/status` `/init` | |
| `!cmd` | run a shell command; its output is added to the conversation |
| `@path` | mention a file: text is inlined, images are attached |
| `esc+enter` | newline · **ctrl+c** interrupts a running turn |

## Mingbird's tricks, and where they live

| Small models can't… | Finch does it for them | Where |
|---|---|---|
| …fit all tools in context | **Flat prefill**: 9 core tools with minimal schemas (no per-param descriptions); categories (file/code/web/memory/mcp) loaded by keyword routing; advanced tools via `enable_tools`; skills listed by name only and loaded on demand. A known tool that isn't loaded is loaded on the fly instead of refused. | `heuristics.route_categories`, `agent.tool_surface` |
| …tell chit-chat from work | **Q&A/task layering**: questions get a chat system prompt and read-only tools, answer once and stop; write/run calls are blocked in chat mode. Short follow-ups continue an in-progress task. | `heuristics.is_qa`, `agent._loop` |
| …escape tool loops | **Signature-level anti-loop ladder**: identical consecutive call is blocked (3×); 5-in-a-row nudge; 8 in a row with 4 identical → tool disabled; calling a disabled tool: nudge at 3, **context hard reset** at 10. Research guard counts only *repeated identical* queries. Todo guard. Per-tool failure ladder: 3 fails → strategy hint (edit_file → disabled, rewrite with create_file). | `agent._loop` |
| …stop circling on one file | **Shotgun-edit guard**: 4 successful edits of the same file without convergence → "find the root cause first" (2 hints max, never disables). | `heuristics.EditConvergenceGuard` |
| …recover from empty output | **Empty-turn ladder**: nudge at 3, hard reset to task + plan + file listing at 6 (max 3 resets), then graceful exit. Unclosed `<think>` counts as empty. Truncation at the token cap gets **"skeleton then append"** chunking feedback. | `agent._loop` |
| …emit valid tool calls | **Format-leak salvage**: JSON in text, ```json fences, `<tool_call>` tags, gemma `call:name{…}`, placeholder quotes `<\|"\|>`, bare `finish(summary="x")`. Tool-name aliases (`write_file`, `bash`, …) and argument aliases (`file_path`, `cmd`, …). Missing-argument errors include a correct example. | `heuristics.try_parse_tool_calls`, `tools.ALIASES` |
| …self-debug | **Test guard**: when tests exist, the harness runs them itself before accepting `finish` and feeds back the exact failure (`file:line` + the `E` line). pytest → unittest fallback → skip if Python is missing. A broken third-party pytest plugin is detected and retried with autoload off. | `heuristics.run_test_guard` |
| …stay honest at the end | **Finish gates**, each capped so they can't deadlock: fake finish (no work done) · test guard · claimed files must exist · files named in the plan must exist · plan must be ticked · **delivery self-check** re-injects the original task once. An empty final summary triggers a wrap-up call. | `agent._finish_gates` |
| …edit code safely | **Rollback everywhere**: backup before every write (`.finch/backups/`), including in-place shell edits (`sed -i`, `>` redirects) detected in commands; `delete_file` goes to `.finch/trash/`; **clobber guard** refuses to overwrite a big file with a much smaller one unless `replace=true`; `/undo`. | `tools.Rollback`, `tools.bash_snapshot` |
| …write escaped code | **Auto-repair**: a `.py` file that fails to compile because of literal `\n` sequences is unescaped (kept only if it then compiles). Edits retry with unescaped `old` text. | `tools.auto_repair` |
| …crawl huge files | **Crawl guard v2**: byte budget (2× file size, clamped 64KB–512KB). It never refuses: past the budget it throttles to 2000 chars/read and teaches the "one script" approach. Nudges at reads 4 and 10. | `tools.ToolBox.t_read_file` |
| …manage long contexts | **Tiered compaction** by preflight estimate *and* actual `prompt_eval_count`: 60% → truncate old tool outputs, 75% → summarise older turns, 90% → full rebuild. The summary carries the verbatim task, the live plan and the workdir listing. Overflow errors force L3. Tiers re-arm when usage drops. | `agent.compact` |
| …keep the plan alive | Stale-plan nudge after 4 productive steps without a todo update. | `agent._loop` |
| …survive a flaky server | 500s → compact + inject a path-breaking message (same messages on a degraded runner reproduce the same failure); exponential backoff; optional restart hook `FINCH_OLLAMA_RESTART_CMD` after 4 consecutive 500s. `keep_alive=30m` is sent top-level so the model isn't unloaded mid-task. Bash runs in its own process group and the whole tree is killed on timeout. | `agent._loop`, `llm.py` |
| …be trusted with your disk | **Safety rings**: ① irreversible ops refused outright (`rm -rf /`, `mkfs`, `dd of=/dev/sd*`, `curl … \| sh`, `shutdown`…) · ② uninstalls / env mutation need confirmation, and are denied when unattended; recursive deletes only inside the workdir · ③ out-of-workdir paths and sensitive files (`.ssh`, `.env`, keys…) need confirmation; sensitive writes are always refused · ④ rollback (above). `/unsafe` bypasses ring ② only. | `safety.py` |
| …manage wall-clock time | Timebox pacing nudges at 50/75/90%. | `heuristics.budget_nudge` |
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

## Toggleable tricks

Every mechanism in the table above is a named **trick** (`finch/tricks.py`, 32 of them in 7 groups) that
can be switched off. Use the Tricks tab in `/settings`, `/tricks <key> off`, `"tricks": {"crawl_guard": false}`
in config.json, or `FINCH_ABLATION=key1,key2` (Mingbird's ablation names `finish_gate`, `anti_loop` and
`flat_prefill` are accepted as aliases). "Disable all tricks" gives you a plain agent loop for comparison.
The toolbar shows how many tricks are off. **The hard safety ring (irreversible commands) can't be
turned off.**

## Config

`~/.finch/config.json` (written by `/model`, `/ctx`, `/think`, `/offline`):

```json
{"model": "gemma4:12b", "num_ctx": 32768, "think": null, "host": "http://127.0.0.1:11434", "theme": "claude",
 "tricks": {"self_check": false},
 "backend": "ollama", "openai_base_url": "http://127.0.0.1:1234/v1",
 "num_predict": 8192, "keep_alive": "30m", "bash_timeout": 120}
```

Env overrides: `OLLAMA_HOST`, `FINCH_SOUL`, `FINCH_MODEL`, `FINCH_CTX`, `FINCH_THINK`, `FINCH_TEMP`, `FINCH_OFFLINE=1`,
`FINCH_THEME`, `FINCH_UNSAFE=1`, `FINCH_ALLOW_ENV_MUTATION=1`, `FINCH_OLLAMA_RESTART_CMD`, `FINCH_HOME`,
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

The suite includes a scripted fake model that drives the full loop through the finish gates, the
anti-loop ladder, the empty-turn ladder and the chat mode. It doesn't need a running backend.

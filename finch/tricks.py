"""Registry of the harness "tricks" (Mingbird's small-model mechanisms). Each one can be
switched off from /settings, in config.json ("tricks": {"key": false}) or with
FINCH_ABLATION=key1,key2 for experiments. Everything defaults to on.

The hard safety ring (irreversible ops refused) is deliberately NOT in this list."""

# (key, group, label, description)
TRICKS = [
    # ---- prompt & routing
    ("flat_prefill", "Prompt & routing", "Flat prefill",
     "Only send the tool categories the task needs (smaller prompt). Off = all tools every turn."),
    ("qa_layering", "Prompt & routing", "Q&A / task layering",
     "Questions get a chat prompt + read-only tools and answer once. Off = everything is a task."),
    ("auto_enable", "Prompt & routing", "Auto-load known tools",
     "A real tool that isn't loaded gets loaded on the fly instead of refused."),
    ("small_model", "Prompt & routing", "Small-model protocol",
     "For ≤4B models: edit_file off up front, bash output trimmed, shell-vs-python hint."),
    ("timebox_parse", "Prompt & routing", "Timebox from task text",
     "Detect 'timebox 40 minutes' / 'within 1.5 hours' in the task and pace the run."),
    # ---- output repair
    ("salvage", "Output repair", "Tool-call salvage",
     "Turn tool calls written as text (JSON, fences, call:name{…}, finish(...)) into real calls."),
    ("aliases", "Output repair", "Name aliases",
     "Map write_file→create_file, bash→run_bash, file_path→path, … instead of failing."),
    ("auto_repair", "Output repair", "Escape auto-repair",
     "Unescape literal \\n in .py files that fail to compile (kept only if it then compiles)."),
    ("truncation_feedback", "Output repair", "Truncation feedback",
     "When output hits the token cap: 'write a skeleton, then append' instead of a generic nudge."),
    ("stall_feedback", "Output repair", "Stall feedback",
     "A reply that goes silent mid-stream (a huge tool call) → 'write it in pieces' instead of an identical retry."),
    ("office_extract", "Output repair", "Office / PDF extraction",
     "read_file turns xlsx, docx, pptx and pdf into text (rows numbered) instead of returning zip bytes."),
    ("env_hints", "Output repair", "Environment hints",
     "Explain exit 127, broken pytest plugins, interactive commands, missing arguments."),
    # ---- anti-loop
    ("dup_block", "Anti-loop", "Duplicate-call block",
     "Block an exact repeat of the previous call (same tool, same args)."),
    ("streak_guard", "Anti-loop", "Streak guard",
     "Nudge after 5 same-tool calls; disable a tool after 8 (4 identical); refusal ladder → hard reset."),
    ("failure_ladder", "Anti-loop", "Failure ladder",
     "3 consecutive failures of a tool → strategy hint (edit_file → disabled, rewrite instead)."),
    ("research_guard", "Anti-loop", "Research guard",
     "Disable web tools after 6 repeats of an identical query."),
    ("todo_guard", "Anti-loop", "Todo guard",
     "Disable todo after 15 plan calls without real work."),
    ("edit_guard", "Anti-loop", "Shotgun-edit guard",
     "4 successful edits of one file without converging → 'find the root cause first'."),
    ("empty_ladder", "Anti-loop", "Empty-turn ladder",
     "Empty outputs: nudge at 3, context hard reset at 6 (×3), then stop."),
    ("repeat_detect", "Anti-loop", "Repeated-text detection",
     "Same prose twice in a row → end the turn."),
    ("stream_loop_guard", "Anti-loop", "Stream-loop guard",
     "Model repeats itself mid-stream (text or reasoning) → cut it off, nudge, retry hotter / without thinking, hard reset."),
    # ---- finish gates
    ("fake_finish_gate", "Finish gates", "Fake-finish gate",
     "Reject finish when no real work was done."),
    ("test_guard", "Finish gates", "Test guard",
     "Harness runs the tests before accepting finish and feeds back the exact failure."),
    ("artifact_gate", "Finish gates", "Artifact check",
     "Files claimed in the summary or named in the plan must exist."),
    ("plan_sync_gate", "Finish gates", "Plan sync gate",
     "The plan must be ticked before finishing."),
    ("self_check", "Finish gates", "Delivery self-check",
     "Re-inject the original task once before accepting finish."),
    ("partial_read_gate", "Finish gates", "Partial-read gate",
     "Reject an answer built on a file the model only read the start of; make it read the rest or script it."),
    ("plan_nudge", "Finish gates", "Stale-plan nudge",
     "Remind the model to tick the plan after 4 productive steps without an update."),
    ("wrap_up", "Finish gates", "Wrap-up summary",
     "If finish has no summary, ask the model for a short closing message."),
    # ---- context
    ("compaction", "Context", "Tiered compaction",
     "60% truncate old tool output · 75% summarise · 90% rebuild (with task, plan, file list)."),
    ("crawl_guard", "Context", "Crawl guard",
     "Throttle repeated reads of the same big file (byte budget 2× size); never refuses."),
    ("error_recovery", "Context", "Server-error recovery",
     "On repeated 500s: compact + inject a path-breaking message (+ optional restart hook)."),
    # ---- safety (the hard ring is always on)
    ("backups", "Safety", "Backups & trash",
     "Back up before every write (incl. sed -i / redirects); delete_file goes to trash; /undo."),
    ("clobber_guard", "Safety", "Clobber guard",
     "Refuse to overwrite a big file with a much smaller one unless replace=true."),
    ("behaviour_tiering", "Safety", "Behaviour tiering (ring 2)",
     "Uninstalls / system changes need confirmation; recursive delete only inside the workdir."),
    ("boundary_confirm", "Safety", "Boundary confirmation (ring 3)",
     "Paths outside the workdir and sensitive files need confirmation."),
]
# Longer plain-language explanations: the problem each trick solves, what it does, and what turning it off means.
# Shown under the short description in /settings and by `/tricks <key>`.
DETAILS = {
    "flat_prefill":
        "Small models get confused by long tool lists, and every tool schema costs context. Finch sends a core "
        "set of tools and adds the file, code, web, memory or MCP groups only when the task's wording calls for "
        "them. Off: every enabled tool is sent on every turn, which uses more context and gives the model more "
        "ways to pick the wrong tool.",
    "qa_layering":
        "A question like \"what does this function do?\" shouldn't start a multi-step task. Finch spots questions, "
        "gives them a chat prompt with read-only tools, and stops after one answer; tools that change files are "
        "blocked in this mode. Short follow-ups to a running task still count as part of the task. Off: every "
        "message is handled as a task, with all the planning and finish checks that go with it.",
    "auto_enable":
        "Because of flat prefill, the model sometimes calls a real tool that wasn't sent this turn. Finch loads "
        "that tool and runs the call instead of returning an error. Off: the call is refused, and the model has "
        "to load the tool with enable_tools first, which small models often fail to do.",
    "small_model":
        "Models of 4B parameters or fewer get a lighter protocol: edit_file is off from the start (they rarely "
        "match the exact old text), so they rewrite whole files; long bash output is cut to its tail; and they get "
        "a hint about when to use a shell command and when to write a Python script. Off: tiny models are treated "
        "like large ones. Has no effect on models over 4B.",
    "timebox_parse":
        "If the task says \"timebox 40 minutes\" or \"within 1.5 hours\", Finch uses that as the time budget and "
        "warns the model at 50%, 75% and 90% of it. Off: only the timebox from /settings applies, and time "
        "phrases in the task are ignored.",
    "salvage":
        "Small models often write a tool call as text (JSON, a ```json block, <tool_call> tags, gemma's "
        "call:name{…} or a bare finish(summary=\"…\")) instead of making a real call. Finch recognises these and "
        "runs them as real calls. Off: such text is treated as a normal reply, so the action never happens and "
        "the run usually stalls.",
    "aliases":
        "Models trained on other agent harnesses use their tool and argument names (write_file, bash, file_path). "
        "Finch maps these onto its own (create_file, run_bash, path). Off: a wrong name fails with an "
        "unknown-tool or missing-argument error, and the model has to correct itself.",
    "auto_repair":
        "A common small-model mistake is writing Python with literal \\n sequences in place of real line breaks, "
        "so the file won't compile. When a .py file fails to compile, Finch unescapes it and keeps the fix only "
        "if the file then compiles. Off: the broken file stays as written.",
    "truncation_feedback":
        "When a reply hits the output-token cap (usually while writing a big file in one go), the call is lost. "
        "Finch tells the model exactly what to do: create a skeleton with create_file, then add to it with "
        "append_file. Off: the model only gets a generic \"continue\" nudge and tends to retry the same "
        "oversized write.",
    "stall_feedback":
        "Ollama sends nothing while the model writes a tool call, so a reply that writes a whole program in one "
        "create_file can go silent for longer than the stall timeout (General tab) and be cut off. Retrying the "
        "same request just writes the same huge call again. Finch instead tells the model to deliver the file in "
        "pieces: create_file with the first part, then append_file the rest. Off: a plain retry after a back-off.",
    "office_extract":
        "Office files are zip archives, so reading them as text gives the model garbage. read_file turns xlsx "
        "(numbered rows), docx, pptx and pdf into plain text, cached until the file changes. Off: read_file "
        "returns the raw contents and the model has to write its own parser.",
    "env_hints":
        "Raw errors don't tell a small model what to do next. Finch adds a short explanation to common failures: "
        "exit code 127 (command not found), a pytest crash caused by a broken plugin, a command that waits for "
        "keyboard input and hangs, or a tool call missing a required argument (with a correct example). Off: the "
        "model sees only the raw error.",
    "dup_block":
        "Calling the same tool with exactly the same arguments twice in a row almost always means the model is "
        "stuck. Finch refuses the repeat and tells the model it already has that result. Off: repeats run again, "
        "costing time and context.",
    "streak_guard":
        "Catches loops that dup_block misses because the arguments change slightly. 5 calls in a row to one tool "
        "get a nudge; 8 in a row (4 of them identical) disable that tool. If the model keeps calling the disabled "
        "tool, it gets nudged after 3 tries and its context is reset after 10. Off: nothing stops a long "
        "single-tool loop.",
    "failure_ladder":
        "After 3 failures in a row of the same tool, Finch suggests a different approach. For edit_file "
        "(usually old text that doesn't match) it disables the tool and asks for whole-file rewrites. Off: the "
        "model can keep retrying a failing call indefinitely.",
    "research_guard":
        "Small models can search for the same thing over and over. After 6 identical web queries Finch disables "
        "the web tools for the rest of the task. Different queries never count. Off: there is no limit on "
        "repeated searches.",
    "todo_guard":
        "Some models keep rewriting their plan instead of doing the work. After 15 todo calls with no real work "
        "in between, the todo tool is disabled. Planning and ticking off steps as you go doesn't trigger it. "
        "Off: the plan can be rewritten forever.",
    "edit_guard":
        "4 successful edits to one file without the problem going away usually means guessing. Finch asks the "
        "model to stop and find the root cause, at most twice per file, and never disables anything. Off: "
        "no warning about repeated edits to one file.",
    "empty_ladder":
        "Sometimes a model returns nothing (or only an unfinished <think> block) turn after turn. After 3 empty "
        "turns Finch nudges; after 6 it resets the context to the task, plan and file list (up to 3 times); after "
        "that it stops the run cleanly. Off: empty turns just repeat until the step limit.",
    "repeat_detect":
        "If the model writes the same reply twice in a row, it's going round in circles. Finch ends the turn "
        "rather than letting it write the same thing a third time. Off: repeated replies continue.",
    "stream_loop_guard":
        "Watches the reply as it streams. If the model starts repeating the same phrase (in its answer or its "
        "thinking), Finch cuts it off and discards the partial output, then escalates: a nudge, then a retry with "
        "hotter sampling and thinking switched off, then a context reset, then a stop. Off: a looping reply runs "
        "until it hits the token cap.",
    "fake_finish_gate":
        "Small models often call finish straight away and claim success. Finch rejects a finish when nothing "
        "real has happened (no files changed, no commands run, no answer written). Analysis tasks that answered "
        "by reading are allowed through. At most 2 rejections. Off: an empty finish is accepted.",
    "test_guard":
        "If the working directory has tests, Finch runs them itself before accepting finish. On failure it sends "
        "the model the exact file:line and error and asks for a fix. Allowed through after 3 failed attempts, so "
        "it can't loop forever. Off: the model's own claim that the tests pass is trusted.",
    "artifact_gate":
        "Checks that every file the finish summary claims to have made actually exists, and that every output "
        "file named in the plan exists too. Missing files mean the finish is rejected (at most twice per check). "
        "Off: the summary can mention files that were never written.",
    "plan_sync_gate":
        "If the model made a todo plan, every item must be ticked before finish is accepted, so the plan matches "
        "what was actually done. At most 2 rejections. Off: finish is accepted with unticked steps.",
    "self_check":
        "On long tasks the model loses track of the details it was asked for. Just before accepting finish, Finch "
        "shows it the original task once more and asks it to check formats, names, ranges and every sub-item. "
        "Only happens once per task. Off: no final re-read.",
    "partial_read_gate":
        "read_file returns large files in pieces. If the model is about to answer about a file it only saw part "
        "of, Finch lists the line ranges it never saw and asks it to read the rest or process the whole file with "
        "a script. At most twice per task. Off: answers can be based on a partial read.",
    "plan_nudge":
        "After 4 productive steps without a todo update, Finch reminds the model to tick off what it has done "
        "(at most twice). This keeps the plan, and the progress shown to you, up to date. Off: no reminder.",
    "wrap_up":
        "If the model finishes without saying anything, Finch asks it for a short plain-text summary of what it "
        "did, so you always get a closing message. Off: the run can end with just \"(done)\".",
    "compaction":
        "Stops the conversation outgrowing the context window. At 60% full, old tool output is shortened; at 75% "
        "older turns are summarised; at 90% the context is rebuilt from the task, plan and file list. Uses the "
        "real token count reported by Ollama. Off: the context keeps growing until the model starts forgetting "
        "or the request fails.",
    "crawl_guard":
        "Stops the model reading the same big file again and again. Each file gets a read budget of twice its "
        "size; after that, reads are limited to 2000 characters and the model is told to use a script instead. "
        "It never blocks a read outright. Off: unlimited re-reads.",
    "error_recovery":
        "When Ollama keeps returning server errors (often a crashed or overloaded runner), Finch backs off, "
        "compacts the context and tells the model to try a different approach. If FINCH_OLLAMA_RESTART_CMD is "
        "set, it also runs that command. Off: repeated errors end the run.",
    "backups":
        "Before any file is written (including shell edits like sed -i and > redirects), Finch saves a copy in "
        ".finch/backups/. delete_file moves files to the trash instead of deleting them. /undo restores the "
        "latest change. Off: writes and deletes are final.",
    "clobber_guard":
        "Stops an existing file over 300 bytes being replaced by something less than half its size, which "
        "usually means the model rewrote it from a partial memory. The model has to pass replace=true to do it "
        "on purpose. Off: any file can be overwritten with anything.",
    "behaviour_tiering":
        "Commands that uninstall software or change the system ask you first (and are refused when no one is "
        "there to answer, e.g. headless or scheduled runs). Recursive deletes are allowed only inside the working "
        "directory. /unsafe turns this off. The hard ring (irreversible operations) is always on.",
    "boundary_confirm":
        "Using a path outside the working directory, or reading a sensitive file (SSH and cloud keys, .env, "
        "credentials, Finch's own config), asks you first. Off: both are allowed without asking. Writing to a "
        "sensitive file is refused either way.",
}

KEYS = [t[0] for t in TRICKS]
BY_KEY = {t[0]: t for t in TRICKS}

# Mingbird's ablation names -> trick keys
ABLATION_ALIASES = {
    "finish_gate": ["fake_finish_gate", "test_guard", "artifact_gate", "plan_sync_gate", "self_check"],
    "finish_gate_text_only": ["fake_finish_gate", "test_guard", "artifact_gate", "plan_sync_gate"],
    "anti_loop": ["dup_block", "streak_guard", "research_guard", "todo_guard"],
    "verify_feedback": [],      # handled specially: test guard without the exact failure text
}


def expand(names):
    out = set()
    for n in names:
        n = n.strip()
        if n in ABLATION_ALIASES:
            out.update(ABLATION_ALIASES[n])
        elif n in BY_KEY:
            out.add(n)
    return out


def groups():
    seen = []
    for t in TRICKS:
        if t[1] not in seen:
            seen.append(t[1])
    return seen

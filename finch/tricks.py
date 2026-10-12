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

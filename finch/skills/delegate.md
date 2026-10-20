---
name: delegate
description: Split big work into independent parts and hand each to a subagent (spawn_agent)
requires: spawn_agent
preload: agent
---
# Delegating to subagents
The point is a fresh context for each part: let the subagent do the reading. Don't read the inputs yourself first.
1. Plan in todo, one item per part (2-4, max 6). Linked edits to the same files: do them yourself.
2. Fix shared choices first (names, formats, style) and repeat them in every brief.
3. spawn_agent once per part. It can't see this chat, so the task holds everything:
   GOAL: one sentence. CONTEXT: file paths, facts, shared choices.
   OUTPUT: the file to write and its format. DONE WHEN: how to check. DON'T: files to leave alone.
4. read_file each output and check it. If one failed, do that part yourself; don't resend the same brief.
5. Combine the parts, verify the whole, then finish.

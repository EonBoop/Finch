---
name: explore_codebase
description: Understand an unfamiliar project before changing or explaining it
---
# Exploring a codebase
Don't read every file: your context is small. Read only what the task needs.
1. list_dir "." and read the README plus pyproject.toml / package.json / Makefile (start_line=1, end_line=60).
2. search_files for names from the task (function, error text, setting) to find the files that matter.
3. Read those files. For long ones, read only around the search hits (start_line/end_line).
4. Find how to run the tests and run them once, so you know what already fails before you change anything.
5. Before editing, write a 5-line map in your reply: entry point, key files, how to run, how to test.
When explaining, cite file:line for each claim. Never guess what a file contains: read it.

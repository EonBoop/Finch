---
name: code_review
description: Review code for bugs, edge cases and clarity
---
# Code review
1. Read the code and identify what it is supposed to do.
2. Look for: wrong logic, unhandled edge cases (empty input, None, zero, large values), resource leaks, injection risks.
3. Run the code / tests if possible to confirm suspected bugs.
4. Report findings as a list: file:line, the problem, a concrete fix. Most severe first.

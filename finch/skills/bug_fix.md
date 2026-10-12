---
name: bug_fix
description: Systematic debugging: reproduce, locate, minimal fix, verify
---
# Fixing a bug
1. Reproduce: find the smallest input that triggers the error and run it.
2. Locate: read the full traceback; find the exact line and the root cause (print intermediate values if unsure).
3. Fix: make the smallest change that addresses the root cause. Don't refactor unrelated code.
4. Verify: re-run the reproduction and the tests. Both must pass.
5. If the fix didn't work, go back to step 2 with a new hypothesis. Never repeat the same fix.

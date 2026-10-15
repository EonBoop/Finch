---
name: test_driven
description: Write tests first, then the implementation, run until green
---
# Test-driven workflow
1. Write test_<module>.py with pytest functions covering normal cases, edge cases, and errors.
2. Run `python3 -m pytest -q` (`python` on Windows) and confirm the tests fail for the right reason.
3. Implement the code until the tests pass. Re-run after every change.
4. Never edit a test just to make it pass unless the test itself is wrong.

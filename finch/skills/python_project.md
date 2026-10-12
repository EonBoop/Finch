---
name: python_project
description: Layout and conventions for a small Python project or CLI
---
# Small Python project
- One package folder or a single module; entry point under `if __name__ == "__main__":`.
- Use argparse for CLIs; print errors to stderr and exit non-zero on failure.
- Standard library first; only add dependencies when clearly needed (list them in requirements.txt).
- Put tests in test_*.py next to the module; run with `python3 -m pytest -q`.
- Big files: create_file the skeleton first, then append_file the remaining parts.

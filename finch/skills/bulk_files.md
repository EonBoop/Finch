---
name: bulk_files
description: Move, sort or rename many files on disk at once, with a dry run first
---
# Bulk file operations
Always TWO run_python calls: the first only PRINTS the plan, the second moves. Never move in the first call.
1. run_python (dry run): build a list of (old, new) paths with pathlib and print every pair and the count.
2. Read the printed plan: every file has a target, no two files get the same new name, no target exists yet.
3. run_python again with the same list: create the folders, then shutil.move each pair. Don't delete
   anything: move unwanted files into a folder instead.
4. list_dir the target folders and check the file count matches the plan.

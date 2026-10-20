---
name: multi_file_change
description: Change a function, class or setting name that is used across many code files
---
# Changing something used in many files
1. search_files for every use, e.g. pattern \bold_name\b. Put each file in todo.
2. Run the tests before you start, so you know which failures were already there.
3. Change the definition first, then each use. One file at a time: read_file it, then edit_file with old
   text copied exactly from what you read.
4. Many identical replacements? Use run_python, not sed (it differs per OS): re.sub on the listed files, print the count per file.
5. Verify: search_files for the old name returns nothing (unless meant), and the tests pass as before.

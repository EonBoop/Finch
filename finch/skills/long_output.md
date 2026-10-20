---
name: long_output
description: Write a long report, document or file in parts so it doesn't get cut off
---
# Writing long output
One huge create_file call gets cut off mid-file. Write in parts instead.
1. Plan the sections in todo (one item per section, in order).
2. create_file with the title and the first section only.
3. append_file one section per call, in order, each under about 80 lines. Tick it off in todo.
4. When done, read_file the whole file and check that every planned section is there, complete and not repeated.
5. Fix gaps with edit_file (copy the old text exactly from read_file output). Don't rewrite the whole file.

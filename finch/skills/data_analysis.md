---
name: data_analysis
description: Analyse CSV/JSON data with a script instead of reading it row by row
---
# Data analysis
1. Peek at the file: read_file with start_line=1, end_line=20 to see the columns.
2. Write ONE python script (create_file analyse.py) that loads the whole file and computes everything needed.
3. Run it with run_bash; print results clearly.
4. Write the conclusions (numbers with units) to the requested output file.
Never page through a large file with repeated read_file calls; a script reads it all at once.

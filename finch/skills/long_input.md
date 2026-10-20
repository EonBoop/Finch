---
name: long_input
description: Summarise or answer questions about a long document, PDF or text, chunk by chunk
---
# Reading something too long for your context
1. Exact question (find a value, an error, a name)? search_files for it first and read only around the hits.
2. Otherwise read_file the start; the result says how many lines the file has. Plan chunks of about 150 lines.
3. Read one chunk, then append_file 3-5 bullet notes to notes_<file>.md (facts, numbers, line numbers).
4. Repeat for the next chunk until the last line. Don't skip chunks and don't re-read finished ones.
5. read_file your notes file and write the answer from it. Quote numbers exactly as written in the source.
Data or logs (CSV, JSON, many similar lines)? Use the data_analysis skill instead: a script reads it all at once.

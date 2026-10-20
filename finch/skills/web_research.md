---
name: web_research
description: Research a topic on the web and write a sourced report
requires: web_search_multi, web_fetch
---
# Web research
1. Plan 3-5 distinct queries (different angles, not rephrasings). Use web_search_multi.
2. web_fetch the 2-4 most relevant results. Full text is saved to disk; read_file it for details.
3. Write findings to a markdown file as you go (create_file, then append_file). Cite URLs.
4. Don't repeat identical queries. If results are thin, change the angle.
5. Finish with a short summary and the report path.

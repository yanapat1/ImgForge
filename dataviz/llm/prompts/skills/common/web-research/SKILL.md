---
name: web-research
description: answer a public-knowledge question with web search, citing sources, without leaking internal names
when: the question needs facts the app does not hold (a model, a paper, a library, a public dataset) and web search is on
---
1. Check first whether the knowledge topics or your tools already answer it (`read_docs`); the app's own facts beat
   the web.
2. Write the query with general words only: the task, the method, public names. Never the user's bucket, folder,
   file, class or company names, nor keys or tokens (such a query is blocked; rephrase it generally).
3. `web_search` with 1-2 focused queries; open the 1-3 best results with `fetch_url` when the snippet is not enough.
4. Prefer primary sources (official docs, the paper, the repository) over blogs; note the date for fast-moving topics.
5. Answer briefly with the sources as links, and say how it applies to the user's case (the dataset or page).
6. Web search off or blocked: answer from what you know, say it is not checked, and suggest turning web on (/web).

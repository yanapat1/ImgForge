# CLAUDE.md

See `ARCHITECTURE.md` for the layout and `dataviz/llm/README.md` for the ◆ assistant.

## Keep the ◆ assistant in step with every change

After any change to a feature, the system or a program (UI, routes, options, limits, names), re-check the LLM part
so the in-app assistants can still use the feature and explain it correctly. Update what is stale:

- `dataviz/llm/prompts/<role>/knowledge/*.md`: feature docs (front matter `title`, `summary`, `core`, `high`)
- `dataviz/llm/prompts/base.md`, `<role>/system.md`: rules and scope
- `dataviz/llm/prompts/skills/<role|common>/<name>/SKILL.md`: how-to workflows
- `dataviz/llm/tools/*.py` (descriptions, args, risk), `engine/roles.py`, `tasks.py`
- `.claude/skills/datacenter/SKILL.md` and `dataviz/llm/prompts/datacenter.md` (the old Data Center agent)

Then run `.venv/bin/python -m pytest -q tests/test_llm*.py`. In the final report, say what was checked and updated, or "no assistant
impact".

## Every coding task: efficient, correct, committed

- Plan in detail first (files, approach, edge cases, tests) so the change works first time.
- Write the fastest, most efficient code that fits: efficient algorithms, no repeated work or N+1 queries,
  batched DOM updates, virtualized long lists, indexed/streamed backend data.
- Use newer techniques when they suit the code. Add a library only when it makes the system faster,
  less laggy, less error-prone, or easier to understand and use; say why in the report.
- Keep code readable. Never trade correctness for speed: handle errors and edge cases, run the relevant tests.
- When the task is finished and tests pass, commit it: one commit per task, only that task's files
  (never unrelated uncommitted work), message names the task. Report the commit hash.

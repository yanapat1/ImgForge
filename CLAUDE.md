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

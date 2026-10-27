# `dataviz/llm/` — the ◆ AI assistant

LLM providers and the assistant features built on them. Routes: `server/routes/llm.py` (`/api/llm/*`).
Nothing here runs unless a provider is connected (saved, active, last Test connection passed) and the feature's
switch is on (all off by default).

| File | What it does |
|---|---|
| `store.py` | `LLMStore`, **per user** in PostgreSQL: `app.llm_providers` (private to their owner; API key Fernet-encrypted and write-only; `env:NAME` keys admin-only, never resolved for a non-admin) + `app.llm_user_config` (the active one, feature switches, token budget). Every method takes the acting user (default: the signed-in one); `state()` is what the page may show. |
| `providers.py` | Raw httpx clients (no SDKs) for `openai`, `openai_compat`, `anthropic`, `gemini` behind one message format `[{role, parts: [{text} / {jpeg, cache?} / {wav}]}]`: `chat_stream()` (SSE → deltas + usage), `complete()`, `complete_json()` (fences stripped, one retry with the parse error), `list_models()`, `test()`. Errors are `LLMError(msg, status)`; the key never appears in a message. |
| `budget.py` | Token estimates (text ≈ 3.5 chars/token; per-provider image formulas) and `fit()`: older images → `[image omitted]`, oldest turns dropped, context images downscaled / dropped, else 400. Hard cap `min(budget, 100k)`. |
| `context.py` | Compact context: `dataset_summary` (text tables), `eval_summary` (via `routes/models.eval_overview`), `selection_mosaic` (≤ 24 crops in one numbered JPEG + legend), `image_mosaic`, class exemplar sheets (`state_exemplars`, `task_exemplars`, cached per task + annotation count), `prepare_image` (≤ 1024 px). |
| `providers.py` (tool calling) | `complete_tools(p, system, msgs, tools)`: one non-streamed native tool-calling step → `{text, tool_calls: [{id, name, args, sig?}], usage}`. History parts `{tool_call: {id, name, args, sig?}}` (assistant) and `{tool_result: {id, name, content, is_error?}}` (a `tool` message) become OpenAI `tool_calls` + role `tool` messages, Anthropic `tool_use` / `tool_result` blocks (results open the user turn), Gemini `functionCall` (with its `thoughtSignature`) / `functionResponse`; Gemini schemas cut to its OpenAPI subset (`gemini_schema`). Arguments that are not a JSON object parse as `args: None`; a missing id is `''` (the agent assigns one). |
| `tools.py` | The Data Center tool registry (`TOOLS`, `specs()`, `catalog()`): read-only (`list_buckets`, `list_folder`, `search`, `detect_format`, `get_stats`, `read_text`), changes (`create_folder`, `move`, `copy`, `rename`, `delete`, `add_tags`, `remove_tags`, `send_to_session`) and control (`ask_user`, `cannot_do`). `validate()`, `resolve_bucket()` (id or exact name; ambiguous → `ToolError` telling the model to ask), `prepare()` (a change checked for a plan), `run()`. Calls only `datacenter.service` (imported lazily). Shared by the agent, `python -m dataviz dc` (`datacenter_cli.py`) and `.claude/skills/datacenter/SKILL.md`. |
| `agent.py` | The Data Center agent behind `/api/llm/dc/chat` + `/confirm`: ≤ 8 `complete_tools` steps; read-only tools run (`tool` events), `ask_user` / `cannot_do` end the turn (`ask` / `cannot`), changes become a plan (`plan`; in memory, per user, 15 min, bounded) that only `confirm(user, plan_id, action_ids)` runs, in plan order. NDJSON events keyed `type` (datacenter/API.md). |
| `prompts/` | `datacenter.md`: the agent's system prompt (rules: look up, never guess; ask when ambiguous; refuse the impossible with an alternative; changes only via a plan; short). `agent.system_prompt()` appends `tools.catalog()` and the page scope. |
| `tasks.py` | Structured features, JSON answers validated server side: `label_check`, `nl_filter` (+ `clean_filter`, the `core/filter.js` `emptyFilter()` shape), `auto_tag_job` (a job writing `ai:<tag>` tags), `label_assist` (boxes converted from the provider's `box_format` to normalised x1y1x2y2), `voice` (whitelisted actions). |

## Token cost

One mosaic image instead of N crops; full images ≤ 1024 px; summaries as text tables; the exemplar sheet goes
first and is flagged for prompt caching (Anthropic `cache_control`; OpenAI / Gemini cache identical prefixes
automatically); `fit()` runs before every call and the chat reports the estimate (`meta.in_est`) and the provider's
usage (`done.usage`, our estimate when a server reports none).

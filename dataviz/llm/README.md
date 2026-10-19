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
| `tasks.py` | Structured features, JSON answers validated server side: `label_check`, `nl_filter` (+ `clean_filter`, the `core/filter.js` `emptyFilter()` shape), `auto_tag_job` (a job writing `ai:<tag>` tags), `label_assist` (boxes converted from the provider's `box_format` to normalised x1y1x2y2), `voice` (whitelisted actions). |

## Token cost

One mosaic image instead of N crops; full images ≤ 1024 px; summaries as text tables; the exemplar sheet goes
first and is flagged for prompt caching (Anthropic `cache_control`; OpenAI / Gemini cache identical prefixes
automatically); `fit()` runs before every call and the chat reports the estimate (`meta.in_est`) and the provider's
usage (`done.usage`, our estimate when a server reports none).

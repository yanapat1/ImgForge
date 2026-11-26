# 2. Providers: one streamed tool-calling step

Use raw HTTP (httpx) rather than SDKs. You get one code path, fewer dependencies, and full control of retries and errors.
Four kinds cover almost everything: `openai`, `openai_compat` (vLLM, Ollama, LM Studio, llama.cpp, Groq,
OpenRouter …), `anthropic`, `gemini`.

## The contract

```python
def stream_tools(p, system, msgs, tools, think=None, native_web=False, max_tokens=None):
    """Yields, in order:
        ('thinking', {text, sig?, redacted?})   reasoning chunks (a signed one goes back in the history)
        ('text', str)                           answer chunks
        ('tool_call', {id, name, args, sig?})   once per COMPLETE call; args None if not a JSON object
        ('search', {...})                       optional: provider-native web search results
        ('stop', str)                           why it ended ('length' / 'max_tokens' / 'MAX_TOKENS' = cut off)
        ('usage', {'in': int, 'out': int})      last
    Raises LLMError(msg, status, transient=bool, retry_after=float|None)."""
```

`p` is the provider config: `{kind, base_url, model, api_key, temperature?, max_output_tokens?, context_window?}`.
Also offer `complete(p, system, msgs, max_tokens)` (non-streamed text, used for summaries) and `complete_json`
(strip ``` fences, parse, and retry once with the parse error).

## History mapping (the details that bite)

| Neutral | OpenAI / compatible | Anthropic | Gemini |
|---|---|---|---|
| system | first `{'role':'system'}` message | `system: [blocks]` (stable block with `cache_control`, then dynamic) | `systemInstruction` |
| `tool_call` | assistant `tool_calls: [{id, type:'function', function:{name, arguments: json-string}}]` | `tool_use {id, name, input}` | `functionCall {name, args}` (+ its `thoughtSignature`) |
| `tool_result` | a separate `{'role':'tool', tool_call_id, content}` message right after | `tool_result` blocks **first** in the next user turn, `is_error` | `functionResponse {name, response}` |
| `thinking` | never sent back (no signature) | `thinking {thinking, signature}` / `redacted_thinking` must open the assistant turn | `thought` parts + signature |
| image | `image_url` data URL | `image` base64 source | `inlineData` |

- **Merge consecutive same-role messages** before mapping (some APIs refuse two user turns in a row).
- **OpenAI**: current models want `max_completion_tokens`. Compatible servers want `max_tokens`. Send
  `stream_options.include_usage` only to OpenAI itself (some servers refuse it).
- **Gemini** accepts only an OpenAPI subset in function schemas. Strip everything else (`additionalProperties`,
  `default`, `$schema` …) recursively, or you get a 400.
- **Anthropic caching**: a breakpoint on the stable system block, one on the last block of the last message (each loop
  step then reads the earlier steps from the cache), and optionally one on a big reused image. That is at most 4.
- **Anthropic `pause_turn`** (long server-tool turns): send the partial content back and continue, ≤ 3 times.
- Thinking settings by kind: Anthropic `thinking.budget_tokens` (or adaptive), Gemini `thinkingConfig`, OpenAI
  `reasoning_effort`. Local models that print `<think>…</think>` need a small tag parser that turns it into
  thinking events.

## Refused parameters: drop once, remember per model

A compatible server may reject `temperature`, `stream_options`, a thinking field or native web search. Handle
400 / 422 like this: find the parameter the error message names, drop it, **remember it for this (base_url, model)**
and resend. When the error names nothing (a compatible server), drop every optional parameter, and remember them
only once the retry works. If nothing is left to drop, the error is real.

```python
_refused = {}            # (base_url, model) -> {param names}

def _send_with_fallback(p, body, post):
    key = (p.get('base_url'), p.get('model'))
    for name in _refused.get(key, ()):                  # leave out what this model refused before
        body.pop(name, None)
    for _ in range(4):
        r = post(body)
        if r.status_code not in (400, 422):
            return r
        msg = r.text.lower()
        hit = next((n for n in OPTIONAL if n in body and n in msg), None)
        if hit is None:
            return r                                    # a real error: the caller raises LLMError
        body.pop(hit)
        _refused.setdefault(key, set()).add(hit)
    return r

OPTIONAL = ('temperature', 'stream_options', 'reasoning_effort', 'thinking', 'web_search_options')
```

## Errors

```python
class LLMError(ValueError):
    def __init__(self, msg, status=502, transient=False, retry_after=None):
        super().__init__(msg)
        self.status, self.transient, self.retry_after = status, transient, retry_after
```

- **transient** = 408, 409, 425, 429, 5xx, timeouts, connection resets, and a stream that broke *after* it started.
  The engine retries these. Read `Retry-After` into `retry_after`.
- **Scrub the key** from every message (`text.replace(api_key, '***')`) and never log request headers.
- Make the message actionable: "the model has no tool calling", "context too long", "check the API key".

## Model limits

`model_limits(p) -> {context, output}`. Use the context from the provider card if the user set one, else ask the API (Gemini
`inputTokenLimit`, Anthropic `max_input_tokens`, OpenRouter / vLLM / Ollama fields such as `context_length`,
`max_model_len`), else a small built-in table, else unknown (use a 100k default). Ask once per model and cache it.

---
name: agent-engine
description: Blueprint for building an in-app AI assistant / agent engine in any project — a ReAct tool-calling loop over several LLM providers (OpenAI, OpenAI-compatible/local, Anthropic, Gemini), a tool registry with risk levels and user confirmation, modes ask/auto/plan, effort levels, skills and knowledge loaded on demand, prompt caching, token budget with stubbing and compaction, loop guard, retries, a streamed event protocol for the chat UI, and optional sub-agents. Use when asked to build or improve an AI assistant, agent, chatbot with tools, ReAct loop, tool calling, "skills", context compaction or an "AI system like the agent-engine".
---

# Agent engine blueprint

A proven design for an assistant that lives inside an application: the user chats, the model thinks, calls the
app's tools, reads the results and answers. It is **stack-neutral**: the sketches are Python (taken from a working
system), so translate them to the project's language. The domain (what the tools do) belongs to the new project.
This blueprint covers only the agent mechanics.

## The core idea in one paragraph

One **generator** runs a turn: build the system prompt (Markdown files, a stable cached part + a per-message part),
then loop **step** = one streamed provider call → if the model called no tool, that text is the answer; else run
its tool calls (reads at once, changes after the user confirms, or at once in auto mode), append the results to
the history and loop again. Everything the loop does is **yielded as events** (one JSON object per line) that the
chat UI renders. Limits keep it safe: max steps (by effort), a token budget (old results stubbed, then the run's own
work **compacted** into a progress summary so it can go on), a **loop guard** on repeated identical calls, and
backoff retries on transient errors. Knowledge and **skills** (step-by-step playbooks) are listed by one line each
in the prompt and loaded with `read_docs` / `use_skill` only when needed, so the prompt stays small.

## Reference (read the file for the part you are building)

| # | File | What it covers |
|---|---|---|
| 1 | [reference/01-architecture.md](reference/01-architecture.md) | Layers, folder layout, the neutral message format |
| 2 | [reference/02-providers.md](reference/02-providers.md) | One streamed `stream_tools()` over 4 provider kinds, history mapping, errors |
| 3 | [reference/03-react-loop.md](reference/03-react-loop.md) | The turn generator: steps, end conditions, retries, cut-off, verify pass |
| 4 | [reference/04-tools.md](reference/04-tools.md) | `Tool` + registry, JSON schema helpers, `validate`, risks, tool groups |
| 5 | [reference/05-control-tools.md](reference/05-control-tools.md) | `ask_user`, `cannot_do`, `todo`, `submit_plan`, `enter_plan_mode`, … |
| 6 | [reference/06-modes-confirm.md](reference/06-modes-confirm.md) | Modes ask / auto / plan, pausing the stream for a confirmation |
| 7 | [reference/07-prompts-skills-knowledge.md](reference/07-prompts-skills-knowledge.md) | Prompt from Markdown, skills, knowledge, caching split, base rules |
| 8 | [reference/08-effort-profiles.md](reference/08-effort-profiles.md) | Effort low/medium/high, thinking, small/medium/large model profiles |
| 9 | [reference/09-budget-compaction.md](reference/09-budget-compaction.md) | Token estimate, budget, stubbing, run compaction, chat compaction |
| 10 | [reference/10-loop-guard.md](reference/10-loop-guard.md) | Detecting and recovering from repeated identical calls |
| 11 | [reference/11-events-ui.md](reference/11-events-ui.md) | The NDJSON event protocol and what the chat UI shows |
| 12 | [reference/12-subagents-optional.md](reference/12-subagents-optional.md) | Delegate, background tasks, web guard (optional) |
| 13 | [reference/13-testing.md](reference/13-testing.md) | A scripted fake provider and the tests to write |

## How to apply it in a new project

1. **Ask first** (only what you cannot see in the code): which LLM providers (cloud keys, a local server)? The
   backend stack and how it streams to the browser (SSE / NDJSON over fetch / WebSocket)? Is there a chat UI yet?
   Which 3-6 domain actions should the assistant have first? Is there a user/auth model (keys stored per user)?
2. **Build in this order**, each stage working and tested before the next:
   1. Provider adapter: `stream_tools()` for one provider, then the others (02).
   2. Tool registry + 2 domain tools (one read, one change) + `validate` (04).
   3. The loop without confirmation: reads only, the final answer (03).
   4. Events + a minimal chat UI: text, tool rows, done/error (11).
   5. Control tools, modes and confirmation cards (05, 06).
   6. Prompt from Markdown: base rules, role, knowledge, skills, `use_skill` / `read_docs`, the cache split (07).
   7. Budget: estimate, `fit` stubbing, run compaction, chat compaction with saved chats (09).
   8. Effort levels and model profiles (08).
   9. Loop guard, retries, output cut-off retry (10, 03).
   10. Optional: sub-agents, background tasks, web search with a guard (12).
3. **Test each stage** with the scripted fake provider (13). No test should need a real API key.

## Rules that make it work (do not drop these)

- **The server decides, not the model.** Every tool call is validated against its schema and its risk is
  computed on the server. An unknown tool or bad args go back to the model as an error **tool result** (it fixes
  the call), never as a crash.
- **Never claim a change before its result says so.** A change runs only after approval (mode ask), or at once in
  auto. A *dangerous* one (delete, overwrite) is always confirmed. A declined action is not retried.
- **Small prompt, knowledge on demand.** One-line indexes of knowledge and skills; full text through a tool.
- **Stable prompt prefix first** so provider caching works on every step and message (big cost saving).
- **Never fail on size when you can shrink**: stub old tool results, then compact the run, then the chat.
- **Every limit is a tunable default** (steps, budget share, retries, timeouts). Put them in one settings place.
- **Keys never appear** in an error, a log, an event or a prompt.
- **One run per chat** at a time. Stop by run id. Cancellation is checked between every step and while waiting.

## Checklist when done

- [ ] All providers pass the same tool-calling contract test (text, tool call, usage, stop reason).
- [ ] A read tool, a change tool (confirmed in ask, auto in auto), a danger tool (always confirmed) work.
- [ ] ask_user / cannot_do / submit_plan end the turn with a card. todo shows and updates a checklist.
- [ ] Effort changes steps / thinking / the verify pass. A small local model still finishes simple tasks.
- [ ] A long task survives the token budget (stubbing + run compaction) and the step limit (compaction).
- [ ] A model that repeats a call gets nudged, then recovered, then stopped with a clear error.
- [ ] Transient errors retry with backoff and the UI shows it. Stop works mid-step and mid-confirm.
- [ ] Prompt files are Markdown. Editing one takes effect without a restart. User instructions are appended last.

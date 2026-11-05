---
title: The ◆ assistant itself
summary: chat commands (/model, /mode, /effort, /web, /compact ...), modes, sessions, context compaction, limits, the user's own prompts and skills; app-wide ? shortcut sheet, Undo after removing a session / model / connection
---
- **Slash commands** in the chat box: `/model` (pick a provider and model), `/mode ask|auto|plan` (or Shift+Tab),
  `/effort low|medium|high`, `/think off|low|medium|high`, `/web off|public|technical|full`, `/skills`,
  `/skill <name>`, `/sessions`, `/new`, `/rename`, `/delete`, `/cost`, `/compact [focus]`, `/export`, `/prompts`,
  `/plugins`, `/help`.
- **Context compaction**: when a chat's request passes 80% of the input token budget (Settings › AI assistant), or
  it has more than 40 messages, the older messages are folded into one summary (goal, preferences, app state, what
  was done with exact numbers and ids, findings, declined actions, open tasks). The model reads the summary plus
  the last 3 exchanges. A dashed "Context compacted · view summary" line marks the place. `/compact` does it now and
  keeps the last exchange; `/compact <focus>` says what the summary must keep. The page still shows every message.
  If the summary call fails, the chat goes on and the oldest messages are left out instead.
- **Where it is**: a dock on the right of every page (home, Data Center, visualizer, labeling). Ctrl+. or the ◆
  icon opens / collapses it; drag its left edge to resize; on a phone a floating ◆ opens it as a sheet. The ring in
  its head is how much of the input budget the chat uses (compaction at 80%). The Tasks strip shows the agent's
  checklist, what it is driving and a Stop button. Visualizer and Data Center have their own chats (the Visual
  and the Data assistant); home and labeling use the Data assistant.
- **Modes**: ask (every change confirmed), auto (changes run, dangerous ones confirmed), plan (read-only, then a plan
  to approve).
- **Effort** sets how many steps (15 / 30 / 50), thinking, and a final check pass (high).
- **Sessions**: each chat is saved; History lists them (20 per assistant; the one not opened longest is removed
  when a 21st is created; pinned ones are kept). Deleted chats disappear at once.
- **Activity dropdown**: under each answer, one collapsible list of every step (thinking, reads, searches, skills,
  retries); its header says what the model is doing now. Change cards and questions to the user stay in the answer.
- **Your own instructions and skills**: Settings › Prompts & Skills (per account; added to, never replacing, the
  built-in rules). **Plugins** add skills, prompts and web (HTTP) tools.
- **Web search** levels: off, public only, technical, full; organisation "special words" are never sent.
- Providers and models: Settings › AI assistant.
- **App-wide**: press **?** (not while typing) for the keyboard shortcuts of the page on screen (the labeling page
  lists the user's own hotkeys). Removing a session, a labeling session, a model or a connection is undone with the
  **Undo** button on the message that follows (about 8 s); after that it is gone.

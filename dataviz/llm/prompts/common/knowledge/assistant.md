---
title: The ◆ assistant itself
summary: one chat on every page, chat commands (/model, /mode, /effort, /web, /compact ...), modes, sessions, context compaction, limits, the user's own prompts and skills; app-wide ? shortcut sheet, Undo after removing a session / model / connection
---
- **Slash commands** in the chat box: `/model` (pick a provider and model), `/mode ask|auto|plan` (or Shift+Tab),
  `/effort low|medium|high`, `/think off|low|medium|high`, `/web off|public|technical|full`, `/skills`,
  `/skill <name>`, `/sessions`, `/new`, `/rename`, `/delete`, `/cost`, `/compact [focus]`, `/export`, `/prompts`,
  `/plugins`, `/help`.
- **Context compaction**: past 80% of the input budget (Settings › AI assistant) or 40 messages, older messages
  fold into one summary (goal, preferences, app state, work done with numbers and ids, open tasks); the model reads
  it + the last 3 exchanges ("Context compacted · view summary" line). `/compact [focus]` does it now. The page
  keeps every message; if the summary fails, the oldest messages are left out instead.
- **Where it is**: a dock on the right of every page; Ctrl+. or the ◆ icon collapses it, its left edge resizes
  it, a floating ◆ opens it on a phone. Head ring = context used (compaction at 80%); Tasks strip = the checklist,
  "is driving …" and Stop.
- **One chat, every page**: the same chat (history, summary) is open on Home, the Data Center, every visualizer
  session and labeling; the page decides which specialist answers and which tools it has (visualizer: maps,
  stats, models; elsewhere: data). Asked for something on another page, the assistant navigates there and the
  chat goes on ("↪ Continued"). One answer at a time per chat: another tab waits. A page with unsaved work asks
  before it is left. A task over several pages keeps its checklist with the chat (Tasks strip, each step with
  its page). From the Data Center a folder with no visualize session is sent there first (approved in ask; a
  labeling project is always confirmed); from the visualizer the assistant finds the dataset's folder in the
  Data Center (and the folders above it) and opens it there.
- **Modes**: ask (every change confirmed), auto (changes run, dangerous ones confirmed), plan (read-only, then a plan
  to approve).
- **Effort** sets how many steps (15 / 30 / 50), thinking, and a final check pass (high).
- **Sessions**: each chat is saved; History lists them all, with the page each was last on (20 in all; the one
  not opened longest is removed when a 21st is created; pinned ones are kept). Deleted chats disappear at once.
- **Activity dropdown**: under each answer, one collapsible list of every step (thinking, reads, searches, skills,
  retries); its header says what the model is doing now. Change cards and questions to the user stay in the answer.
- **Your own instructions and skills**: Settings › Prompts & Skills (per account; added to, never replacing, the
  built-in rules). **Plugins** add skills, prompts and web (HTTP) tools.
- **Web search** levels: off, public only, technical, full; organisation "special words" are never sent.
- Providers and models: Settings › AI assistant.
- **App-wide**: press **?** (not while typing) for the keyboard shortcuts of the page on screen (the labeling page
  lists the user's own hotkeys). Removing a session, a labeling session, a model or a connection is undone with the
  **Undo** button on the message that follows (about 8 s); after that it is gone.

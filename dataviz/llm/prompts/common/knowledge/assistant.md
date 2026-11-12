---
title: The ◆ assistant itself
summary: one chat on every page, chat commands (/model, /mode, /effort, /web, /compact ...), modes, sessions, context compaction, limits, the user's own prompts and skills; app-wide ? shortcut sheet, Undo after removing a session / model / connection
---
- **Slash commands** in the chat box: `/model` (pick a provider and model), `/mode ask|auto|plan` (or Shift+Tab),
  `/effort low|medium|high`, `/think off|low|medium|high`, `/web off|public|technical|full`, `/skills`,
  `/skill <name>`, `/sessions`, `/new`, `/rename`, `/delete`, `/cost`, `/compact [focus]`, `/export`, `/prompts`,
  `/plugins`, `/help`.
- **Sub-agents** (Settings › AI assistant › Sub-agents, or `/agents on|off`; off by default): the assistant may split
  big read-only look-ups across helper agents (`delegate`, up to 4 at once), each in its own context; only their
  answers enter the chat, so long tasks need less context, while small questions cost a little more. Each helper
  shows as a "Sub-agent" row with its steps and tokens; an optional cheaper Sub-agent model can run them. Never on
  small (<= 14B) models.
- **Token budget** (Settings › AI assistant): the largest request sent to the model. Auto (the default) = the model's own
  maximum, detected from the provider (or the provider card's Model context in Advanced; 100k when unknown); a number
  is kept under it. Bigger keeps more of a long chat, but each step of an answer sends it again.
- **Context compaction**: past 80% of the input budget (Settings › AI assistant) or 40 messages, older messages
  fold into one summary (goal, preferences, app state, work done with numbers and ids, open tasks); the model reads
  it + the last 3 exchanges ("Context compacted · view summary" line). `/compact [focus]` does it now. The page
  keeps every message; if the summary fails, the oldest messages are left out instead.
- **Where it is**: a dock on the right of every page; Ctrl+. or its collapse button folds it to a slim rail whose ◆ opens it again, its left edge resizes
  it, a floating ◆ opens it on a phone. Head ring = how full this chat's context is now (the latest model call's input of the limit; it moves while an answer runs and is kept after a reload; compaction at 80%), not the tokens billed (`/cost` sums those over every step); Tasks strip = the checklist,
  "is driving …" and Stop.
- **The pages**, in the header's order: **Bucket** (buckets of files), **Image** (the visualize and label sessions,
  one tab each; a visualizer session and the labeling page open from it) and **Data** (database queries,
  statistics, dashboards). The DataViz logo opens **Home**, the overview with one card per page.
- **One chat, every page**: the same chat (history, summary) is open on Home, Bucket, Image, every visualizer
  session, labeling and Data; the page decides which specialist answers and which tools it has (visualizer: maps,
  stats, models; Data: queries; elsewhere: buckets). Asked for something on another page, the assistant navigates there and the
  chat goes on ("↪ Continued"). One answer at a time per chat: another tab waits. A page with unsaved work asks
  before it is left. A task over several pages keeps its checklist with the chat (Tasks strip, each step with
  its page). From the Bucket page a folder with no visualize session is sent there first (approved in ask; a
  labeling project is always confirmed); from the visualizer the assistant finds the dataset's folder in the
  Bucket (and the folders above it) and opens it there. A plain question about another page ("mAP of
  yolov8s in a1?" from the Bucket page) is answered without leaving: the other page's specialist looks it up
  read-only ("Asking the Visualizer" in the Tasks strip).
- **Modes**: ask (every change confirmed), auto (changes run, dangerous ones confirmed), plan (read-only, then a plan
  to approve). In Data a database query of the assistant (`run_query`) is confirmed too in ask and plan
  (its SQL can be edited on the card) and runs at once in auto. In auto, a big, unclear or far-reaching request
  makes the assistant switch itself to plan mode first ("Switched to plan mode: …", the mode chip says plan):
  it looks things up read-only, asks what is the user's choice, and shows a plan; Approve · auto runs it.
- **Effort** sets how many steps (15 / 30 / 50), thinking, and a final check pass (high).
- **Regenerate / Edit**: every answer has **Regenerate** (answer the same message again) and every sent message
  has **Edit** on hover (change it, Enter sends, Esc cancels). Either one replaces that message's answer and every
  message after it, on the page and in the saved chat, so the old branch is gone for good (no versions kept); a
  summary that covered those messages is dropped too. Not while an answer is running.
- **Sessions**: each chat is saved; History lists them all, with the page each was last on (20 in all; the one
  not opened longest is removed when a 21st is created; pinned ones are kept). Deleted chats disappear at once.
- **Activity dropdown** ("Worked"): under each answer, one collapsible list of every step (thinking, reads, searches,
  skills, retries); its header says what the model is doing now. It opens by itself while the answer runs and folds
  to "Worked 12s · 3 tools" when it is done (a click on its header keeps it open or closed). Change cards and
  questions to the user stay in the answer.
- **Scrolling**: the chat follows the newest message while an answer streams. Scrolling up stops that, so older
  messages can be read in peace; a "Jump to latest" button (it reads "New reply" when more arrived meanwhile) or
  scrolling back to the end follows again. Sending a message always jumps to the end.
- **Your own instructions and skills**: Settings › Prompts & Skills (per account; added to, never replacing, the
  built-in rules). **Plugins** add skills, prompts and web (HTTP) tools.
- **Web search** levels: off, public only, technical, full; organisation "special words" are never sent.
- Providers and models: Settings › AI assistant.
- **App-wide**: press **?** (not while typing) for the keyboard shortcuts of the page on screen (the labeling page
  lists the user's own hotkeys). Removing a session, a labeling session, a model or a connection is undone with the
  **Undo** button on the message that follows (about 8 s); after that it is gone.
- **Parts of the product (zones)**: core, Bucket, Image (Visualize = zone visualizer, Label = zone labeling), Data. An install can hold only some of them (for example Bucket + Data, without the Visualizer and its GPU libraries): an admin builds it with `ZONES=bucket,data` in `.env` / `--build-arg ZONES=...`, or picks them at start with `DATAVIZ_ZONES`. Pages and tools of a zone that is not installed do not exist; say so instead of suggesting them.

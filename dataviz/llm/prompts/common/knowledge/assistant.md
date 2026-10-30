---
title: The ◆ assistant itself
summary: chat commands (/model, /mode, /effort, /web ...), modes, sessions, limits, the user's own prompts and skills
---
- **Slash commands** in the chat box: `/model` (pick a provider and model), `/mode ask|auto|plan` (or Shift+Tab),
  `/effort low|medium|high`, `/think off|low|medium|high`, `/web off|public|technical|full`, `/skills`,
  `/skill <name>`, `/sessions`, `/new`, `/rename`, `/delete`, `/cost`, `/export`, `/prompts`, `/plugins`, `/help`.
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

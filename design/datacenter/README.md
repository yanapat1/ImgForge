# Data Center: design directions

The Data Center is the page where all imported data lives. It works like a file manager over **buckets**: managed imports, linked local folders and linked S3 buckets. Each bucket gets an auto-detected format badge: YOLO detect / seg / pose, COCO, Pascal VOC, classification folders, images only, embeddings, or Other. It has these features:
- Folder tags, with a tag filter.
- Thumbnails or names-only view for image folders.
- Auto re-tagging to **Other** when an edit breaks the format, with Undo.
- **Send to → Visualize / Label session**.
- An AI assistant that uses tools. Read-only tools run automatically. Changes are shown as a plan you confirm. When a request is ambiguous it asks a clarifying question; when a request is impossible it says so and suggests an alternative.

Pick one direction (or a mix, e.g. "design2 board + design3 AI checklist"). The page will then be built in that style.

| folder | direction | in one line |
|---|---|---|
| [`design1/`](design1/) | **Explorer** | A 3-pane file manager: bucket tree · grid/list · details panel, with the AI in a bottom drawer. |
| [`design2/`](design2/) | **Bucket board** | Visual bucket cards with sample mosaics and big format badges, drill-down with breadcrumbs, AI in a right side panel. |
| [`design3/`](design3/) | **Command console** | A dense, table-first, keyboard-driven page: an omnibar for filters and AI, a tree-table with preview, AI in a split pane with an action checklist. |

Every folder has the same shots, so you can compare them side by side:

| shot | shows |
|---|---|
| `img1` | overview of all buckets with format badges, tags and counts |
| `img2` | inside an image folder, thumbnails grid, a selection |
| `img3` | the same folder in names-only mode, plus the folder tag editor |
| `img4` | the AI assistant: a confirm-the-plan reply, a clarifying-question reply and a "can't do" reply |
| `img5` | the format broke → auto-retagged to "Other" toast, plus the Send-to-session dialog |
| `img6`, `img7` | img1 and img2 in the light theme |

Each folder's `NOTES.md` lists that direction's strengths and weaknesses.

## Sources
`_src/` holds the mockup HTML. It uses the app's real `static/css/tokens.css` and fonts, so colours, type and themes match the product. It also holds the synthetic sample photos (`make_samples.py`) and `render.mjs`, which re-renders the PNGs with the pre-installed Playwright:

```bash
cd design/datacenter/_src && node render.mjs design1 design2 design3
```

## After a design is picked (build plan)
- **Backend** `dataviz/datacenter/`:
  - `store.py`: the managed store plus linked sources. Paths are checked against `storage.allowed_roots`; delete goes to trash.
  - `formats.py`: format detection, sampled and cached by fingerprint.
  - `tags.py`: user tags plus `format:*` auto tags, re-checked after every change.
  - Migration `0006_datacenter`; routes `/api/dc/*`.
- **LLM**: native tool calling for Anthropic, OpenAI-compatible and Gemini. The tool registry is in `dataviz/llm/tools.py` and the agent loop with a confirm step in `agent.py`. The skill `.claude/skills/datacenter/SKILL.md` gives the tool catalogue and the ask/refuse rules.
- **Frontend** `static/ts/datacenter/`: a `?data` screen with one module per concern, and `css/datacenter.css`. The "New session" forms gain a "From Data Center…" picker.
- `ARCHITECTURE.md`: a code map so every part is easy to find.

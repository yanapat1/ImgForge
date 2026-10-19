# `static/assets/` — icons and fonts

| File | What it is |
|---|---|
| `icons.svg` | The icon sprite. Every icon is a `<symbol id="i-name">` on a 24px grid. It is used as `<svg class="ico"><use href="static/assets/icons.svg#i-name"/></svg>` in HTML, or `icon('name')` in JS (`js/core/dom.js`). |
| `fonts/atkinson-next.woff2` | Atkinson Hyperlegible Next, a variable font (weights 200–800) used for all UI text. It is preloaded in `index.html`. |
| `fonts/atkinson-mono.woff2` | Atkinson Hyperlegible Mono, used for numbers, paths and keys (tabular figures). |

## Icon conventions

- **Duotone.** Strokes use `currentColor`, which the icon inherits from the text colour around it.
  Parts with `class="f"` get a 22% fill of the same colour, and parts with `class="s"` are solid.
- The page's stylesheet **cannot reach inside an external sprite**. So the `.f` and `.s` rules live in a
  `<style>` inside `icons.svg` itself, while stroke width and colour come in by inheritance from
  `.ico` in `css/base.css`.
- To add an icon, append a `<symbol id="i-…" viewBox="0 0 24 24">` using round 1.7px strokes (set by
  `.ico`) and keep it optically centred in the 24px box.

## Why an external sprite

The sprite used to be inline at the top of `index.html`, roughly 30 symbols of markup in front of the
page content. As a separate file it is downloaded once, cached and revalidated like everything else,
and the HTML now contains only the page structure.

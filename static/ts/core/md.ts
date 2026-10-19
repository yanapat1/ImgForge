// Tiny safe Markdown → HTML for the ◆ assistant's answers. Everything is escaped first (esc), then a small
// subset is turned back into tags: fenced code, inline code, headings (# … ###), bold, italic, ul / ol lists,
// simple pipe tables, paragraphs, and http(s) links. Raw HTML in the text always stays text.
// It is fast enough to re-render a whole growing answer once per animation frame while it streams, and an
// unfinished construct (an open ``` fence, a half table) renders as what it is so far.

import { esc } from './dom.js';

// inline: code spans first (their content is never formatted), then links, bold, italic
function inline(s0: string): string {
  let s = s0;
  const codes: string[] = [];
  s = s.replace(/`([^`\n]+)`/g, (_m, c: string) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  // [text](http…) — the url is already escaped (no quotes left: esc turns " into &quot;)
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_m, t: string, u: string) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`);
  s = s.replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>').replace(/__([^_\n]+?)__/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*\w])\*([^*\n]+?)\*(?!\w)/g, '$1<em>$2</em>').replace(/(^|[^_\w])_([^_\n]+?)_(?!\w)/g, '$1<em>$2</em>');
  return s.replace(/\u0000(\d+)\u0000/g, (_m, i: string) => `<code>${codes[+i] ?? ''}</code>`);
}

const cells = (line: string): string[] => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
const isSep = (line: string): boolean => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);

/** Render (a safe subset of) Markdown to HTML; raw HTML in `src` stays text. */
export function md(src?: string | null): string {
  const lines = esc(src || '').replace(/\r\n?/g, '\n').split('\n');
  const out: string[] = [];
  let para: string[] = [];
  const flush = () => { if (para.length) { out.push(`<p>${inline(para.join('\n')).replace(/\n/g, '<br>')}</p>`); para = []; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? '';
    const fence = line.match(/^\s*```\s*([\w+-]*)\s*$/);
    if (fence) {
      flush();
      const code: string[] = [];
      while (++i < lines.length && !/^\s*```\s*$/.test(lines[i] ?? '')) code.push(lines[i] ?? '');
      out.push(`<pre><code${fence[1] ? ` data-lang="${fence[1]}"` : ''}>${code.join('\n')}</code></pre>`);
      continue;
    }
    const h = line.match(/^\s*(#{1,6})\s+(.*)$/);
    if (h) { flush(); const n = Math.min(3, (h[1] ?? '').length) + 2; out.push(`<h${n}>${inline((h[2] ?? '').replace(/\s*#+\s*$/, ''))}</h${n}>`); continue; }
    if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) { flush(); out.push('<hr>'); continue; }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && isSep(lines[i + 1] ?? '')) {
      flush();
      const head = cells(line);
      i++;
      const rows: string[][] = [];
      while (i + 1 < lines.length && /^\s*\|.*\|?\s*$/.test(lines[i + 1] ?? '') && (lines[i + 1] ?? '').includes('|')) rows.push(cells(lines[++i] ?? ''));
      out.push('<div class="md-table"><table><thead><tr>' + head.map((c) => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>' +
        rows.map((r) => '<tr>' + head.map((_, k) => `<td>${inline(r[k] || '')}</td>`).join('') + '</tr>').join('') + '</tbody></table></div>');
      continue;
    }
    const li = line.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
    if (li) {
      flush();
      const ordered = /\d/.test(li[1] ?? '');
      const items: string[] = [li[2] ?? ''];
      // following items of the same kind; an indented line continues the previous item
      while (i + 1 < lines.length) {
        const nx = (lines[i + 1] ?? '').match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
        if (nx && /\d/.test(nx[1] ?? '') === ordered) { items.push(nx[2] ?? ''); i++; } else if (!nx && /^\s{2,}\S/.test(lines[i + 1] ?? '')) { items[items.length - 1] += ' ' + (lines[++i] ?? '').trim(); } else break;
      }
      const tag = ordered ? 'ol' : 'ul';
      const start = ordered ? parseInt(li[1] ?? '1', 10) : 1;
      out.push(`<${tag}${ordered && start !== 1 ? ` start="${start}"` : ''}>` + items.map((t) => `<li>${inline(t)}</li>`).join('') + `</${tag}>`);
      continue;
    }
    if (!line.trim()) { flush(); continue; }
    para.push(line);
  }
  flush();
  return out.join('');
}

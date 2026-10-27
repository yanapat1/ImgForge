// Render each design's HTML shots to PNG. Usage: node render.mjs design1 [design2 ...]
// Every design HTML takes ?shot=imgN (and optional &theme=light) and shows that state.
import { createRequire } from 'module';
const { chromium } = createRequire(import.meta.url)('playwright');
import { fileURLToPath } from 'url';
import path from 'path';
import fs from 'fs';
const here = path.dirname(fileURLToPath(import.meta.url));
const designs = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
for (const d of designs) {
  const out = path.join(here, '..', d); fs.mkdirSync(out, { recursive: true });
  const shots = JSON.parse(fs.readFileSync(path.join(here, d + '.shots.json'), 'utf8'));
  for (const s of shots) {   // [{name:'img1', q:'shot=img1'}, {name:'img6', q:'shot=img1&theme=light'}]
    await page.goto('file://' + path.join(here, d + '.html') + '?' + s.q);
    await page.waitForTimeout(250);
    await page.screenshot({ path: path.join(out, s.name + '.png') });
    console.log(d, s.name);
  }
}
await browser.close();

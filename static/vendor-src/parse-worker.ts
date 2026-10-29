// Parses a big JSON / YAML file off the page's main thread (datacenter/editor.ts uses it above 512 KB), so opening a
// multi-MB config never freezes the page. Message in: {id, lang, text}; out: {id, ...ParseResult}.

import { structParse } from './editor.js';

self.onmessage = (e: MessageEvent<{ id: number; lang: 'json' | 'yaml'; text: string }>): void => {
  const { id, lang, text } = e.data;
  self.postMessage({ id, ...structParse(lang, text) });
};

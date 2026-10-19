// The ◆ AI assistant's switchboard on the page: what the server says is usable (GET api/llm/state: connected,
// the active provider, effective feature switches). Every LLM control carries data-llm="<feature>" (or several,
// space-separated: shown when any is on) and stays hidden unless that feature is on, so nothing LLM-related is
// ever visible before an LLM is connected and the feature is switched on in Settings › AI assistant.
// Settings fires the window event `llm-changed` after a save / test / switch: everything re-applies live.

import type { LLMState, LLMStatePayload } from '../types.js';
import { getJSON } from './api.js';

/** Live LLM state: mutated in place by setLLMState (import it, never copy it). */
export const LLM: LLMState = { connected: false, provider: null, features: {}, switches: {}, budget: { max_input_tokens: 100000 }, loaded: false };

export const llmOn = (f: string): boolean => !!LLM.features[f];
export const llmAny = (...fs: string[]): boolean => fs.some(llmOn);

// show / hide every [data-llm] element in `root` (default: the document)
export function applyLLM(root: ParentNode = document): void {
  for (const el of root.querySelectorAll<HTMLElement>('[data-llm]')) el.classList.toggle('hidden', !llmAny(...(el.dataset.llm ?? '').split(/\s+/)));
  document.documentElement.toggleAttribute('data-llm-on', llmAny(...Object.keys(LLM.features)));
}

export function setLLMState(st: LLMStatePayload | null | undefined): void {
  if (!st) return;
  Object.assign(LLM, st, { loaded: true });
  applyLLM();
  window.dispatchEvent(new CustomEvent<LLMState>('llm-state', { detail: LLM }));
}

export async function refreshLLM(): Promise<LLMState> {
  try { setLLMState(await getJSON<LLMStatePayload>('api/llm/state')); } catch (e) { /* no LLM support on this server: keep everything hidden */ }
  return LLM;
}

let bound = false;
export function initLLM(st?: LLMStatePayload | null): void {
  if (st) setLLMState(st); else void refreshLLM();
  if (bound) return;
  bound = true;
  window.addEventListener('llm-changed', () => { void refreshLLM(); });
}

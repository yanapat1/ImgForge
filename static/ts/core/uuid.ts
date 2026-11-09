// A random UUID (v4) on every page. crypto.randomUUID exists only in a secure context (HTTPS or localhost), so a server
// opened over plain http at its address has none; crypto.getRandomValues works everywhere and gives the same format.

export function uuid(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = ((b[6] ?? 0) & 0x0f) | 0x40;              // version 4
  b[8] = ((b[8] ?? 0) & 0x3f) | 0x80;              // RFC 4122 variant
  const h = [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

// Hold-to-talk for the ◆ assistant: microphone → MediaRecorder → decoded (AudioContext) → 16 kHz mono 16-bit
// PCM WAV → base64, the format every provider accepts. At most 30 s; the microphone is released after each take.

import { errMessage } from './api.js';

const MAX_MS = 30000, RATE = 16000;

interface Recording { mr: MediaRecorder; stream: MediaStream; chunks: Blob[]; done: Promise<Blob>; timer: ReturnType<typeof setTimeout> }
let rec: Recording | null = null;

// Safari still ships the prefixed constructor
const audioCtxCtor = (): typeof AudioContext | undefined =>
  window.AudioContext || (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;

export const canRecord = (): boolean => !!(typeof navigator.mediaDevices?.getUserMedia === 'function' && 'MediaRecorder' in window &&
  audioCtxCtor() && 'OfflineAudioContext' in window);

export const isRecording = (): boolean => !!rec;

function micError(e: unknown): string {
  const n = typeof e === 'object' && e !== null && 'name' in e ? e.name : undefined;   // DOMException or Error
  if (n === 'NotAllowedError' || n === 'SecurityError') return 'Microphone access was blocked. Allow it in the browser’s site settings (it needs https or localhost).';
  if (n === 'NotFoundError' || n === 'OverconstrainedError') return 'No microphone was found.';
  if (n === 'NotReadableError') return 'The microphone is busy in another app.';
  return `Could not record: ${errMessage(e)}`;
}

// resolves once recording really started (after the permission prompt); rejects with a readable message
/** Start recording; `onLimit` fires when the 30 s cap is reached (the caller then calls stopRecording). */
export async function startRecording({ onLimit }: { onLimit?: () => void } = {}): Promise<void> {
  if (rec) return;
  if (!canRecord()) throw new Error('Voice input is not supported in this browser.');
  let stream: MediaStream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } }); } catch (e) { throw new Error(micError(e)); }
  const mr = new MediaRecorder(stream);
  const chunks: Blob[] = [];
  mr.ondataavailable = (e: BlobEvent) => { if (e.data && e.data.size) chunks.push(e.data); };
  const done = new Promise<Blob>((resolve) => { mr.onstop = () => resolve(new Blob(chunks, { type: mr.mimeType || 'audio/webm' })); });
  rec = { mr, stream, chunks, done, timer: setTimeout(() => { if (onLimit) onLimit(); }, MAX_MS) };
  mr.start(250);
}

// stops, converts, releases the microphone; resolves to the base64 WAV ('' when nothing was recorded)
export async function stopRecording(): Promise<string> {
  const r = rec;
  if (!r) return '';
  rec = null;
  clearTimeout(r.timer);
  if (r.mr.state !== 'inactive') r.mr.stop();
  const blob = await r.done;
  r.stream.getTracks().forEach((t: MediaStreamTrack) => t.stop());
  if (!blob.size) return '';
  return toWavB64(await blob.arrayBuffer());
}

export function cancelRecording(): void {
  const r = rec;
  if (!r) return;
  rec = null;
  clearTimeout(r.timer);
  try { if (r.mr.state !== 'inactive') r.mr.stop(); } catch (e) { /* already stopped */ }
  r.stream.getTracks().forEach((t: MediaStreamTrack) => t.stop());
}

async function toWavB64(buf: ArrayBuffer): Promise<string> {
  const AC = audioCtxCtor();
  if (!AC) throw new Error('Voice input is not supported in this browser.');
  const ac = new AC();
  let audio: AudioBuffer;
  try { audio = await ac.decodeAudioData(buf); } finally { ac.close().catch(() => {}); }
  const secs = Math.min(audio.duration, MAX_MS / 1000);
  const n = Math.max(1, Math.ceil(secs * RATE));
  const off = new OfflineAudioContext(1, n, RATE);          // mono: the channels are mixed down
  const src = off.createBufferSource();
  src.buffer = audio;
  src.connect(off.destination);
  src.start();
  const pcm = (await off.startRendering()).getChannelData(0);
  return b64(wav(pcm));
}

function wav(pcm: Float32Array): Uint8Array {
  const v = new DataView(new ArrayBuffer(44 + pcm.length * 2));
  const str = (o: number, s: string): void => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); str(8, 'WAVE');
  str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, RATE, true); v.setUint32(28, RATE * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, 'data'); v.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) { const s = Math.max(-1, Math.min(1, pcm[i] ?? 0)); v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true); }
  return new Uint8Array(v.buffer);
}

function b64(bytes: Uint8Array): string {
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

// The page's side of the vendored editor module (static/vendor-src/editor.ts, built to static/js/vendor/editor.js and
// loaded at runtime by bucket/editor.ts). Kept in step with that file by hand: it is loaded by URL, not imported.

export type Lang = 'json' | 'yaml' | 'env' | 'ini' | 'toml' | 'xml' | 'html' | 'css' | 'js' | 'ts' | 'python' | 'markdown' | 'sql'
  | 'shell' | 'dockerfile' | 'csv' | 'text';
export type Path = Array<string | number>;
export type StructOp =
  | { op: 'set'; path: Path; value: unknown }
  | { op: 'delete'; path: Path }
  | { op: 'rename'; path: Path; key: string }
  | { op: 'add'; path: Path; key?: string; value: unknown };
export interface EditorOpts { text: string; lang: Lang; readOnly?: boolean; onChange?: (text: string) => void; onSave?: () => void }
export interface EditorHandle {
  getText(): string; setText(text: string): void; setReadOnly(ro: boolean): void; focus(): void; destroy(): void;
}
export type ParseResult = { ok: true; value: unknown } | { ok: false; error: { msg: string; line: number; col: number } };
export interface EditorModule {
  createEditor(host: HTMLElement, o: EditorOpts): Promise<EditorHandle>;
  detectLang(name: string, text: string): Lang;
  structParse(lang: 'json' | 'yaml', text: string): ParseResult;
  structEdit(lang: 'json' | 'yaml', text: string, op: StructOp): string;
  formatText(lang: Lang, text: string): string;
}

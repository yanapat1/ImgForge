import { build, context } from 'esbuild';
import { readdirSync } from 'node:fs';

const globTs = (dir) =>
  readdirSync(dir, { recursive: true, withFileTypes: true })
    .filter((d) => d.isFile() && d.name.endsWith('.ts') && !d.name.endsWith('.d.ts') && !/^types(-[a-z-]+)?\.ts$/.test(d.name))
    .map((d) => `${d.parentPath ?? d.path}/${d.name}`);

const common = {
  entryPoints: globTs('static/ts'),
  outdir: 'static/js',
  outbase: 'static/ts',
  target: 'es2022',
  sourcemap: true,
  logLevel: 'info',
};
const bootOnly = (p) => p.endsWith('theme-boot.ts');
const modules = { ...common, entryPoints: common.entryPoints.filter((p) => !bootOnly(p)), format: 'esm' };
const boot = { ...common, entryPoints: common.entryPoints.filter(bootOnly), format: 'iife' };
// the Data Center's text editor and its libraries (CodeMirror, yaml), loaded only when a file is opened: bundled,
// one chunk per language (splitting), so a JSON file never downloads the Python grammar
const vendor = {
  entryPoints: ['static/vendor-src/editor.ts', 'static/vendor-src/parse-worker.ts'],
  outdir: 'static/js/vendor',
  bundle: true, splitting: true, minify: true, format: 'esm', target: 'es2022', sourcemap: true, logLevel: 'info',
  chunkNames: 'chunks/[name]-[hash]',
};

if (process.argv.includes('--watch')) {
  for (const o of [modules, boot, vendor]) await (await context(o)).watch();
} else {
  await build(modules);
  await build(boot);
  await build(vendor);
}

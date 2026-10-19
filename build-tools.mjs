// Compiles the TypeScript skill scripts to .cjs files next to their sources.
import { build } from "esbuild";

const entryPoints = [
  ".claude/skills/brand/scripts/extract-colors.ts",
  ".claude/skills/brand/scripts/inject-brand-context.ts",
  ".claude/skills/brand/scripts/sync-brand-to-tokens.ts",
  ".claude/skills/brand/scripts/validate-asset.ts",
  ".claude/skills/design-system/scripts/embed-tokens.ts",
  ".claude/skills/design-system/scripts/generate-tokens.ts",
  ".claude/skills/design-system/scripts/validate-tokens.ts",
];

await build({
  entryPoints,
  outdir: ".claude/skills",
  outbase: ".claude/skills",
  outExtension: { ".js": ".cjs" },
  format: "cjs",
  platform: "node",
  target: "node18",
  bundle: false,
  logLevel: "info",
});

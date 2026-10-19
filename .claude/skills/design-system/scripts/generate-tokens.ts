#!/usr/bin/env node
/**
 * Generate CSS variables from design tokens JSON
 *
 * Usage:
 *   node generate-tokens.cjs --config tokens.json -o tokens.css
 *   node generate-tokens.cjs --config tokens.json --format tailwind
 */

import * as fs from 'node:fs';
import * as path from 'node:path';

interface CliOptions {
  config: string | null;
  output: string | null;
  format: string;
}

/** A leaf design token in W3C-style format. */
interface TokenLeaf {
  $value: string | number;
  $type?: string;
  $description?: string;
}

interface TokenGroup {
  [key: string]: TokenNode;
}

type TokenNode = TokenLeaf | TokenGroup | string | number | null | undefined;

interface DesignTokens {
  primitive?: TokenGroup;
  semantic?: TokenGroup;
  component?: TokenGroup;
  dark?: { semantic?: TokenGroup };
  [key: string]: unknown;
}

type CssVars = Record<string, string | number>;

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null;
}

/**
 * Parse command line arguments
 */
function parseArgs(): CliOptions {
  const args = process.argv.slice(2);
  const options: CliOptions = {
    config: null,
    output: null,
    format: 'css' // css | tailwind
  };

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--config' || args[i] === '-c') {
      options.config = args[++i] ?? null;
    } else if (args[i] === '--output' || args[i] === '-o') {
      options.output = args[++i] ?? null;
    } else if (args[i] === '--format' || args[i] === '-f') {
      options.format = args[++i] ?? options.format;
    } else if (args[i] === '--help' || args[i] === '-h') {
      console.log(`
Usage: node generate-tokens.cjs [options]

Options:
  -c, --config <file>   Input JSON token file (required)
  -o, --output <file>   Output file (default: stdout)
  -f, --format <type>   Output format: css | tailwind (default: css)
  -h, --help            Show this help
      `);
      process.exit(0);
    }
  }

  return options;
}

/**
 * Resolve token references like {primitive.color.blue.600}
 */
function resolveReference(value: unknown, tokens: DesignTokens): string | number {
  if (typeof value !== 'string' || !value.startsWith('{')) {
    return value as string | number;
  }

  const keys = value.slice(1, -1).split('.');
  let result: unknown = tokens;

  for (const key of keys) {
    result = isObject(result) ? result[key] : undefined;
  }

  if (isObject(result) && result['$value']) {
    return resolveReference(result['$value'], tokens);
  }

  return (result as string | number | undefined) || value;
}

/**
 * Convert token name to CSS variable name
 */
function toCssVarName(path: string[]): string {
  return '--' + path.join('-').replace(/\./g, '-');
}

/**
 * Flatten tokens into CSS variables
 */
function flattenTokens(
  obj: TokenGroup,
  tokens: DesignTokens,
  prefix: string[] = [],
  result: CssVars = {}
): CssVars {
  for (const [key, value] of Object.entries(obj)) {
    const currentPath = [...prefix, key];

    if (value && typeof value === 'object') {
      if ('$value' in value && value.$value !== undefined) {
        // This is a token
        const cssVar = toCssVarName(currentPath);
        const resolvedValue = resolveReference(value.$value, tokens);
        result[cssVar] = resolvedValue;
      } else {
        // Recurse into nested object
        flattenTokens(value as TokenGroup, tokens, currentPath, result);
      }
    }
  }

  return result;
}

/**
 * Generate CSS output
 */
function generateCSS(tokens: DesignTokens): string {
  const primitive = flattenTokens(tokens.primitive || {}, tokens, ['primitive']);
  const semantic = flattenTokens(tokens.semantic || {}, tokens, []);
  const component = flattenTokens(tokens.component || {}, tokens, []);
  const darkSemantic = flattenTokens(tokens.dark?.semantic || {}, tokens, []);

  let css = `/* Design Tokens - Auto-generated */
/* Do not edit directly - modify tokens.json instead */

/* === PRIMITIVES === */
:root {
${Object.entries(primitive).map(([k, v]) => `  ${k}: ${v};`).join('\n')}
}

/* === SEMANTIC === */
:root {
${Object.entries(semantic).map(([k, v]) => `  ${k}: ${v};`).join('\n')}
}

/* === COMPONENTS === */
:root {
${Object.entries(component).map(([k, v]) => `  ${k}: ${v};`).join('\n')}
}
`;

  if (Object.keys(darkSemantic).length > 0) {
    css += `
/* === DARK MODE === */
.dark {
${Object.entries(darkSemantic).map(([k, v]) => `  ${k}: ${v};`).join('\n')}
}
`;
  }

  return css;
}

/**
 * Generate Tailwind config output
 */
function generateTailwind(tokens: DesignTokens): string {
  const semantic = flattenTokens(tokens.semantic || {}, tokens, []);

  // Extract colors for Tailwind
  const colors: Record<string, string> = {};
  for (const [key] of Object.entries(semantic)) {
    if (key.includes('color')) {
      const name = key.replace('--color-', '').replace(/-/g, '.');
      colors[name] = `var(${key})`;
    }
  }

  return `// Tailwind color config - Auto-generated
// Add to tailwind.config.ts theme.extend.colors

module.exports = {
  colors: ${JSON.stringify(colors, null, 2).replace(/"/g, "'")}
};
`;
}

/**
 * Main
 */
function main(): void {
  const options = parseArgs();

  if (!options.config) {
    console.error('Error: --config is required');
    process.exit(1);
  }

  // Resolve config path
  const configPath = path.resolve(process.cwd(), options.config);

  if (!fs.existsSync(configPath)) {
    console.error(`Error: Config file not found: ${configPath}`);
    process.exit(1);
  }

  // Read and parse tokens
  const tokens = JSON.parse(fs.readFileSync(configPath, 'utf-8')) as DesignTokens;

  // Generate output
  let output: string;
  if (options.format === 'tailwind') {
    output = generateTailwind(tokens);
  } else {
    output = generateCSS(tokens);
  }

  // Write output
  if (options.output) {
    const outputPath = path.resolve(process.cwd(), options.output);
    fs.mkdirSync(path.dirname(outputPath), { recursive: true });
    fs.writeFileSync(outputPath, output);
    console.log(`Generated: ${outputPath}`);
  } else {
    console.log(output);
  }
}

main();

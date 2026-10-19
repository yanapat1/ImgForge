#!/usr/bin/env node
"use strict";
var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(
  // If the importer is in node compatibility mode or this is not an ESM
  // file that has been converted to a CommonJS file using a Babel-
  // compatible transform (i.e. "__esModule" has not been set), then set
  // "default" to the CommonJS "module.exports" for node compatibility.
  isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target,
  mod
));
var fs = __toESM(require("node:fs"), 1);
var path = __toESM(require("node:path"), 1);
function isObject(v) {
  return typeof v === "object" && v !== null;
}
function parseArgs() {
  const args = process.argv.slice(2);
  const options = {
    config: null,
    output: null,
    format: "css"
    // css | tailwind
  };
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--config" || args[i] === "-c") {
      options.config = args[++i] ?? null;
    } else if (args[i] === "--output" || args[i] === "-o") {
      options.output = args[++i] ?? null;
    } else if (args[i] === "--format" || args[i] === "-f") {
      options.format = args[++i] ?? options.format;
    } else if (args[i] === "--help" || args[i] === "-h") {
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
function resolveReference(value, tokens) {
  if (typeof value !== "string" || !value.startsWith("{")) {
    return value;
  }
  const keys = value.slice(1, -1).split(".");
  let result = tokens;
  for (const key of keys) {
    result = isObject(result) ? result[key] : void 0;
  }
  if (isObject(result) && result["$value"]) {
    return resolveReference(result["$value"], tokens);
  }
  return result || value;
}
function toCssVarName(path2) {
  return "--" + path2.join("-").replace(/\./g, "-");
}
function flattenTokens(obj, tokens, prefix = [], result = {}) {
  for (const [key, value] of Object.entries(obj)) {
    const currentPath = [...prefix, key];
    if (value && typeof value === "object") {
      if ("$value" in value && value.$value !== void 0) {
        const cssVar = toCssVarName(currentPath);
        const resolvedValue = resolveReference(value.$value, tokens);
        result[cssVar] = resolvedValue;
      } else {
        flattenTokens(value, tokens, currentPath, result);
      }
    }
  }
  return result;
}
function generateCSS(tokens) {
  const primitive = flattenTokens(tokens.primitive || {}, tokens, ["primitive"]);
  const semantic = flattenTokens(tokens.semantic || {}, tokens, []);
  const component = flattenTokens(tokens.component || {}, tokens, []);
  const darkSemantic = flattenTokens(tokens.dark?.semantic || {}, tokens, []);
  let css = `/* Design Tokens - Auto-generated */
/* Do not edit directly - modify tokens.json instead */

/* === PRIMITIVES === */
:root {
${Object.entries(primitive).map(([k, v]) => `  ${k}: ${v};`).join("\n")}
}

/* === SEMANTIC === */
:root {
${Object.entries(semantic).map(([k, v]) => `  ${k}: ${v};`).join("\n")}
}

/* === COMPONENTS === */
:root {
${Object.entries(component).map(([k, v]) => `  ${k}: ${v};`).join("\n")}
}
`;
  if (Object.keys(darkSemantic).length > 0) {
    css += `
/* === DARK MODE === */
.dark {
${Object.entries(darkSemantic).map(([k, v]) => `  ${k}: ${v};`).join("\n")}
}
`;
  }
  return css;
}
function generateTailwind(tokens) {
  const semantic = flattenTokens(tokens.semantic || {}, tokens, []);
  const colors = {};
  for (const [key] of Object.entries(semantic)) {
    if (key.includes("color")) {
      const name = key.replace("--color-", "").replace(/-/g, ".");
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
function main() {
  const options = parseArgs();
  if (!options.config) {
    console.error("Error: --config is required");
    process.exit(1);
  }
  const configPath = path.resolve(process.cwd(), options.config);
  if (!fs.existsSync(configPath)) {
    console.error(`Error: Config file not found: ${configPath}`);
    process.exit(1);
  }
  const tokens = JSON.parse(fs.readFileSync(configPath, "utf-8"));
  let output;
  if (options.format === "tailwind") {
    output = generateTailwind(tokens);
  } else {
    output = generateCSS(tokens);
  }
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

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
function findProjectRoot(startDir) {
  let dir = startDir;
  while (dir !== "/") {
    if (fs.existsSync(path.join(dir, "assets", "design-tokens.css"))) {
      return dir;
    }
    dir = path.dirname(dir);
  }
  return null;
}
const projectRoot = findProjectRoot(process.cwd());
if (!projectRoot) {
  console.error("Error: Could not find assets/design-tokens.css");
  process.exit(1);
}
const tokensPath = path.join(projectRoot, "assets", "design-tokens.css");
const MINIMAL_TOKENS = [
  "--primitive-spacing-",
  "--primitive-fontSize-",
  "--primitive-fontWeight-",
  "--primitive-lineHeight-",
  "--primitive-radius-",
  "--primitive-shadow-glow-",
  "--primitive-gradient-",
  "--primitive-duration-",
  "--color-primary",
  "--color-secondary",
  "--color-accent",
  "--color-background",
  "--color-surface",
  "--color-foreground",
  "--color-border",
  "--typography-font-",
  "--card-"
];
function extractTokens(css, minimal2 = false) {
  const rootMatch = css.match(/:root\s*\{([^}]+)\}/g);
  if (!rootMatch) return "";
  let allVars = [];
  for (const block of rootMatch) {
    const vars = block.match(/--[\w-]+:\s*[^;]+;/g) || [];
    allVars = allVars.concat(vars);
  }
  if (minimal2) {
    allVars = allVars.filter(
      (v) => MINIMAL_TOKENS.some((token) => v.includes(token))
    );
  }
  allVars = [...new Set(allVars)];
  return `:root {
  ${allVars.join("\n  ")}
}`;
}
const args = process.argv.slice(2);
const minimal = args.includes("--minimal");
const wrapStyle = args.includes("--style");
try {
  const css = fs.readFileSync(tokensPath, "utf-8");
  let output = extractTokens(css, minimal);
  if (wrapStyle) {
    output = `<style>
/* Design Tokens (embedded for standalone HTML) */
${output}
</style>`;
  } else {
    output = `/* Design Tokens (embedded for standalone HTML) */
${output}`;
  }
  console.log(output);
} catch (err) {
  console.error(`Error reading tokens: ${err instanceof Error ? err.message : String(err)}`);
  process.exit(1);
}

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
var import_node_child_process = require("node:child_process");
const BRAND_GUIDELINES = "docs/brand-guidelines.md";
const DESIGN_TOKENS_JSON = "assets/design-tokens.json";
const DESIGN_TOKENS_CSS = "assets/design-tokens.css";
const GENERATE_TOKENS_SCRIPT = ".claude/skills/design-system/scripts/generate-tokens.cjs";
function extractColorsFromMarkdown(content) {
  const colors = {
    primary: { name: "primary", shades: {} },
    secondary: { name: "secondary", shades: {} },
    accent: { name: "accent", shades: {} }
  };
  const rowRe = /\|\s*\*{0,2}([^*|]+?)\*{0,2}\s*\|\s*#([A-Fa-f0-9]{6})\b/g;
  const quickRef = {
    primary: /Primary Color\s*\|\s*#([A-Fa-f0-9]{6})/i,
    secondary: /Secondary Color\s*\|\s*#([A-Fa-f0-9]{6})/i,
    accent: /Accent Color\s*\|\s*#([A-Fa-f0-9]{6})/i
  };
  for (const key of Object.keys(quickRef)) {
    const m = content.match(quickRef[key]);
    if (m) colors[key].base = `#${m[1]}`;
  }
  const assignFromSection = (heading, target) => {
    const section = content.match(new RegExp(`### ${heading}[\\s\\S]*?(?=\\n###|$)`, "i"));
    if (!section) return;
    for (const m of section[0].matchAll(rowRe)) {
      const label = (m[1] ?? "").trim().toLowerCase();
      const hex = `#${m[2]}`;
      if (label.includes("dark")) target.dark = hex;
      else if (label.includes("light")) target.light = hex;
      else if (!target.base) target.base = hex;
    }
  };
  assignFromSection("Primary Colors", colors.primary);
  assignFromSection("Secondary Colors", colors.secondary);
  assignFromSection("Accent Colors", colors.accent);
  if (!colors.accent.base) {
    for (const m of content.matchAll(rowRe)) {
      if ((m[1] ?? "").trim().toLowerCase().includes("accent")) {
        colors.accent.base = `#${m[2]}`;
        break;
      }
    }
  }
  return colors;
}
function generateColorScale(baseHex, darkHex, lightHex) {
  return {
    "50": { "$value": lightHex || adjustBrightness(baseHex, 0.9), "$type": "color" },
    "100": { "$value": lightHex || adjustBrightness(baseHex, 0.8), "$type": "color" },
    "200": { "$value": adjustBrightness(baseHex, 0.6), "$type": "color" },
    "300": { "$value": adjustBrightness(baseHex, 0.4), "$type": "color" },
    "400": { "$value": adjustBrightness(baseHex, 0.2), "$type": "color" },
    "500": { "$value": baseHex, "$type": "color" },
    "600": { "$value": darkHex || adjustBrightness(baseHex, -0.15), "$type": "color" },
    "700": { "$value": adjustBrightness(baseHex, -0.3), "$type": "color" },
    "800": { "$value": adjustBrightness(baseHex, -0.45), "$type": "color" },
    "900": { "$value": adjustBrightness(baseHex, -0.6), "$type": "color" }
  };
}
function adjustBrightness(hex, percent) {
  if (typeof hex !== "string") return "#000000";
  const num = parseInt(hex.replace("#", ""), 16);
  const r = Math.min(255, Math.max(0, (num >> 16) + Math.round(255 * percent)));
  const g = Math.min(255, Math.max(0, (num >> 8 & 255) + Math.round(255 * percent)));
  const b = Math.min(255, Math.max(0, (num & 255) + Math.round(255 * percent)));
  return `#${(r << 16 | g << 8 | b).toString(16).padStart(6, "0").toUpperCase()}`;
}
function updateDesignTokens(tokens, colors) {
  const brandName = `ClaudeKit Marketing - ${colors.primary.name.split("-").map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ")}`;
  tokens.brand = brandName;
  tokens.primitive = tokens.primitive || {};
  const primitiveColors = tokens.primitive.color || {};
  delete primitiveColors.coral;
  delete primitiveColors.purple;
  delete primitiveColors.mint;
  for (const role of ["primary", "secondary", "accent"]) {
    const c = colors[role];
    if (!c.base) {
      console.warn(`\u26A0\uFE0F  No base hex found for ${role} color \u2014 skipping its token scale.`);
      continue;
    }
    primitiveColors[c.name] = generateColorScale(c.base, c.dark, c.light);
  }
  tokens.primitive.color = primitiveColors;
  if (tokens.semantic?.color) {
    const sem = tokens.semantic.color;
    const p = colors.primary.name;
    const s = colors.secondary.name;
    const a = colors.accent.name;
    sem.primary = { "$value": `{primitive.color.${p}.500}`, "$type": "color" };
    sem["primary-hover"] = { "$value": `{primitive.color.${p}.600}`, "$type": "color" };
    sem["primary-active"] = { "$value": `{primitive.color.${p}.700}`, "$type": "color" };
    sem["primary-light"] = { "$value": `{primitive.color.${p}.400}`, "$type": "color" };
    sem["primary-lighter"] = { "$value": `{primitive.color.${p}.100}`, "$type": "color" };
    sem["primary-dark"] = { "$value": `{primitive.color.${p}.600}`, "$type": "color" };
    sem.secondary = { "$value": `{primitive.color.${s}.500}`, "$type": "color" };
    sem["secondary-hover"] = { "$value": `{primitive.color.${s}.600}`, "$type": "color" };
    sem["secondary-light"] = { "$value": `{primitive.color.${s}.300}`, "$type": "color" };
    sem["secondary-dark"] = { "$value": `{primitive.color.${s}.600}`, "$type": "color" };
    sem.accent = { "$value": `{primitive.color.${a}.500}`, "$type": "color" };
    sem["accent-hover"] = { "$value": `{primitive.color.${a}.600}`, "$type": "color" };
    sem["accent-light"] = { "$value": `{primitive.color.${a}.300}`, "$type": "color" };
    sem.success = { "$value": `{primitive.color.${a}.500}`, "$type": "color" };
    sem["success-light"] = { "$value": `{primitive.color.${a}.300}`, "$type": "color" };
    sem.error = { "$value": `{primitive.color.${p}.500}`, "$type": "color" };
    sem["error-light"] = { "$value": `{primitive.color.${p}.300}`, "$type": "color" };
    sem.info = { "$value": `{primitive.color.${s}.500}`, "$type": "color" };
    sem["info-light"] = { "$value": `{primitive.color.${s}.300}`, "$type": "color" };
  }
  if (tokens.component?.button?.secondary && colors.primary.base) {
    const primaryBase = colors.primary.base;
    tokens.component.button.secondary["bg-hover"] = {
      "$value": `${primaryBase}1A`,
      "$type": "color"
    };
  }
  return tokens;
}
function main() {
  const dryRun = process.argv.includes("--dry-run");
  console.log("\u{1F504} Syncing brand guidelines \u2192 design tokens\n");
  const guidelinesPath = path.resolve(process.cwd(), BRAND_GUIDELINES);
  if (!fs.existsSync(guidelinesPath)) {
    console.error(`\u274C Brand guidelines not found: ${guidelinesPath}`);
    process.exit(1);
  }
  const guidelinesContent = fs.readFileSync(guidelinesPath, "utf-8");
  const colors = extractColorsFromMarkdown(guidelinesContent);
  console.log("\u{1F4CA} Extracted colors:");
  console.log(`   Primary: ${colors.primary.name} (${colors.primary.base})`);
  console.log(`   Secondary: ${colors.secondary.name} (${colors.secondary.base})`);
  console.log(`   Accent: ${colors.accent.name} (${colors.accent.base})
`);
  const tokensPath = path.resolve(process.cwd(), DESIGN_TOKENS_JSON);
  let tokens = {};
  if (fs.existsSync(tokensPath)) {
    tokens = JSON.parse(fs.readFileSync(tokensPath, "utf-8"));
  }
  tokens = updateDesignTokens(tokens, colors);
  if (dryRun) {
    console.log("\u{1F4CB} Would update design-tokens.json:");
    console.log(JSON.stringify(tokens.primitive?.color, null, 2).slice(0, 500) + "...");
    console.log("\n\u23ED\uFE0F  Dry run - no files changed");
    return;
  }
  fs.writeFileSync(tokensPath, JSON.stringify(tokens, null, 2));
  console.log(`\u2705 Updated: ${DESIGN_TOKENS_JSON}`);
  const generateScript = path.resolve(process.cwd(), GENERATE_TOKENS_SCRIPT);
  if (fs.existsSync(generateScript)) {
    try {
      (0, import_node_child_process.execFileSync)("node", [generateScript, "--config", DESIGN_TOKENS_JSON, "-o", DESIGN_TOKENS_CSS], {
        cwd: process.cwd(),
        stdio: "inherit"
      });
      console.log(`\u2705 Regenerated: ${DESIGN_TOKENS_CSS}`);
    } catch (e) {
      console.error("\u26A0\uFE0F  Failed to regenerate CSS:", e instanceof Error ? e.message : String(e));
    }
  }
  console.log("\n\u2728 Brand sync complete!");
}
main();

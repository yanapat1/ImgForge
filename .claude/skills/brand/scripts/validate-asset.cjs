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
const RULES = {
  naming: {
    pattern: /^[a-z]+_[a-z0-9-]+_[a-z0-9-]+_\d{8}(_[a-z0-9-]+)?\.[a-z]+$/,
    description: "{type}_{campaign}_{description}_{timestamp}_{variant}.{ext}",
    examples: [
      "banner_claude-launch_hero-image_20251209.png",
      "logo_brand-refresh_horizontal_20251209_dark.svg"
    ]
  },
  dimensions: {
    banner: { minWidth: 600, minHeight: 300 },
    logo: { minWidth: 100, minHeight: 100 },
    design: { minWidth: 800, minHeight: 600 },
    video: { minWidth: 640, minHeight: 480 },
    default: { minWidth: 100, minHeight: 100 }
  },
  fileSize: {
    image: { max: 5 * 1024 * 1024, recommended: 1 * 1024 * 1024 },
    video: { max: 100 * 1024 * 1024, recommended: 50 * 1024 * 1024 },
    svg: { max: 500 * 1024, recommended: 100 * 1024 }
  },
  formats: {
    image: ["png", "jpg", "jpeg", "webp", "gif"],
    vector: ["svg"],
    video: ["mp4", "mov", "webm"],
    document: ["pdf", "psd", "ai", "fig"]
  }
};
function parseFilename(filename) {
  const parts = filename.replace(/\.[^.]+$/, "").split("_");
  if (parts.length < 4) {
    return null;
  }
  return {
    type: parts[0] ?? "",
    campaign: parts[1] ?? "",
    description: parts[2] ?? "",
    timestamp: parts[3] ?? "",
    variant: parts.length > 4 ? parts[4] ?? null : null,
    extension: path.extname(filename).slice(1).toLowerCase()
  };
}
function validateFilename(filename) {
  const issues = [];
  const suggestions = [];
  if (!RULES.naming.pattern.test(filename)) {
    issues.push("Filename does not match naming convention");
    suggestions.push(`Expected format: ${RULES.naming.description}`);
    suggestions.push(`Examples: ${RULES.naming.examples.join(", ")}`);
  }
  const parsed = parseFilename(filename);
  if (parsed) {
    if (!/^\d{8}$/.test(parsed.timestamp)) {
      issues.push("Timestamp should be YYYYMMDD format");
    }
    if (parsed.campaign && !/^[a-z0-9-]+$/.test(parsed.campaign)) {
      issues.push("Campaign name should be kebab-case");
    }
    if (parsed.description && !/^[a-z0-9-]+$/.test(parsed.description)) {
      issues.push("Description should be kebab-case");
    }
    const validTypes = [
      "banner",
      "logo",
      "design",
      "video",
      "infographic",
      "icon",
      "photo"
    ];
    if (!validTypes.includes(parsed.type)) {
      suggestions.push(`Consider using type: ${validTypes.join(", ")}`);
    }
  }
  return { valid: issues.length === 0, issues, suggestions, parsed };
}
function validateFileSize(filepath, extension) {
  const issues = [];
  const warnings = [];
  const stats = fs.statSync(filepath);
  const size = stats.size;
  let limits;
  if (RULES.formats.video.includes(extension)) {
    limits = RULES.fileSize.video;
  } else if (extension === "svg") {
    limits = RULES.fileSize.svg;
  } else {
    limits = RULES.fileSize.image;
  }
  if (size > limits.max) {
    issues.push(
      `File size (${formatBytes(size)}) exceeds maximum (${formatBytes(
        limits.max
      )})`
    );
  } else if (size > limits.recommended) {
    warnings.push(
      `File size (${formatBytes(size)}) exceeds recommended (${formatBytes(
        limits.recommended
      )})`
    );
  }
  return { valid: issues.length === 0, issues, warnings, size };
}
function validateFormat(extension) {
  const issues = [];
  const info = { category: null };
  const allFormats = [
    ...RULES.formats.image,
    ...RULES.formats.vector,
    ...RULES.formats.video,
    ...RULES.formats.document
  ];
  if (!allFormats.includes(extension)) {
    issues.push(`Unsupported file format: .${extension}`);
    return { valid: false, issues, info };
  }
  if (RULES.formats.image.includes(extension)) info.category = "image";
  else if (RULES.formats.vector.includes(extension)) info.category = "vector";
  else if (RULES.formats.video.includes(extension)) info.category = "video";
  else if (RULES.formats.document.includes(extension))
    info.category = "document";
  return { valid: true, issues, info };
}
function checkManifest(filepath) {
  const manifestPath = path.join(process.cwd(), ".assets", "manifest.json");
  if (!fs.existsSync(manifestPath)) {
    return { registered: false, message: "Manifest not found" };
  }
  try {
    const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf-8"));
    const relativePath = path.relative(process.cwd(), filepath);
    const found = manifest.assets?.find(
      (a) => a.path === relativePath || a.path === filepath
    );
    return {
      registered: !!found,
      message: found ? "Asset registered in manifest" : "Asset not in manifest",
      asset: found
    };
  } catch {
    return { registered: false, message: "Error reading manifest" };
  }
}
function suggestFilename(_original, parsed) {
  if (!parsed) return null;
  const today = (/* @__PURE__ */ new Date()).toISOString().slice(0, 10).replace(/-/g, "");
  const type = parsed.type || "asset";
  const campaign = parsed.campaign || "general";
  const description = parsed.description || "untitled";
  const ext = parsed.extension || "png";
  return `${type}_${campaign}_${description}_${today}.${ext}`;
}
function formatBytes(bytes) {
  if (bytes === 0) return "0 Bytes";
  const k = 1024;
  const sizes = ["Bytes", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + (sizes[i] ?? "");
}
function validateAsset(assetPath) {
  const results = {
    path: assetPath,
    filename: path.basename(assetPath),
    valid: true,
    issues: [],
    warnings: [],
    suggestions: [],
    checks: {}
  };
  if (!fs.existsSync(assetPath)) {
    results.valid = false;
    results.issues.push(`File not found: ${assetPath}`);
    return results;
  }
  const filename = path.basename(assetPath);
  const extension = path.extname(filename).slice(1).toLowerCase();
  const filenameResult = validateFilename(filename);
  results.checks.filename = filenameResult;
  if (!filenameResult.valid) {
    results.issues.push(...filenameResult.issues);
    results.suggestions.push(...filenameResult.suggestions);
  }
  const formatResult = validateFormat(extension);
  results.checks.format = formatResult;
  if (!formatResult.valid) {
    results.issues.push(...formatResult.issues);
  }
  const sizeResult = validateFileSize(assetPath, extension);
  results.checks.fileSize = sizeResult;
  if (!sizeResult.valid) {
    results.issues.push(...sizeResult.issues);
  }
  results.warnings.push(...sizeResult.warnings);
  const manifestResult = checkManifest(assetPath);
  results.checks.manifest = manifestResult;
  if (!manifestResult.registered) {
    results.warnings.push("Asset not registered in manifest.json");
    results.suggestions.push(
      "Register asset in .assets/manifest.json for tracking"
    );
  }
  if (!filenameResult.valid && filenameResult.parsed) {
    const suggested = suggestFilename(filename, filenameResult.parsed);
    if (suggested) {
      results.suggestions.push(`Suggested filename: ${suggested}`);
    }
  }
  results.valid = results.issues.length === 0;
  return results;
}
function formatOutput(results) {
  const lines = [];
  lines.push("\n" + "=".repeat(60));
  lines.push(`ASSET VALIDATION: ${results.filename}`);
  lines.push("=".repeat(60));
  lines.push(`
Status: ${results.valid ? "PASS" : "FAIL"}`);
  lines.push(`Path: ${results.path}`);
  if (results.issues.length > 0) {
    lines.push("\nISSUES:");
    results.issues.forEach((issue) => lines.push(`  - ${issue}`));
  }
  if (results.warnings.length > 0) {
    lines.push("\nWARNINGS:");
    results.warnings.forEach((warning) => lines.push(`  - ${warning}`));
  }
  if (results.suggestions.length > 0) {
    lines.push("\nSUGGESTIONS:");
    results.suggestions.forEach(
      (suggestion) => lines.push(`  - ${suggestion}`)
    );
  }
  if (results.checks.fileSize?.size) {
    lines.push(`
File Size: ${formatBytes(results.checks.fileSize.size)}`);
  }
  lines.push("\n" + "=".repeat(60));
  return lines.join("\n");
}
function main() {
  const args = process.argv.slice(2);
  const jsonOutput = args.includes("--json");
  const assetPath = args.find((a) => !a.startsWith("--"));
  if (!assetPath) {
    console.error("Usage: node validate-asset.cjs <asset-path> [--json]");
    console.error("\nExamples:");
    console.error(
      "  node validate-asset.cjs assets/banners/social-media/banner_launch_hero_20251209.png"
    );
    console.error(
      "  node validate-asset.cjs assets/logos/icon-only/logo-icon.svg --json"
    );
    process.exit(1);
  }
  const resolvedPath = path.isAbsolute(assetPath) ? assetPath : path.join(process.cwd(), assetPath);
  const results = validateAsset(resolvedPath);
  if (jsonOutput) {
    console.log(JSON.stringify(results, null, 2));
  } else {
    console.log(formatOutput(results));
  }
  process.exit(results.valid ? 0 : 1);
}
main();

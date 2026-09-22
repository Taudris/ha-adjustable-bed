// Build script for the Adjustable Bed Lovelace card.
// Emits two ESM files into custom_components/adjustable_bed/frontend/dist: the
// gated loader Home Assistant injects, and the card chunk the loader imports
// once a root element is defined.
//
// Usage:
//   bun run build          # one-shot production build
//   bun run build.mjs --watch
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import * as esbuild from "esbuild";

const here = dirname(fileURLToPath(import.meta.url));
const manifest = JSON.parse(
  readFileSync(join(here, "..", "manifest.json"), "utf8"),
);

export const ENTRY_FILENAME = "adjustable-bed-card.js";
export const CHUNK_FILENAME = "adjustable-bed-card-chunk.js";

const DIST_DIR = join(here, "dist");
const CHUNK_SPECIFIER = `./${CHUNK_FILENAME}`;

/** @type {import('esbuild').BuildOptions} */
const shared = {
  bundle: true,
  format: "esm",
  target: "es2021",
  sourcemap: false,
  legalComments: "none",
  define: {
    __CARD_VERSION__: JSON.stringify(manifest.version),
  },
  banner: {
    js: `/* adjustable-bed-card ${manifest.version} — ships with the Adjustable Bed integration. Do not edit; build from frontend/src. */`,
  },
};

const chunkOptions = (outDir, minify) => ({
  ...shared,
  minify,
  entryPoints: [join(here, "src", "adjustable-bed-card-chunk.ts")],
  outfile: join(outDir, CHUNK_FILENAME),
});

const entryOptions = (outDir, minify) => ({
  ...shared,
  minify,
  entryPoints: [join(here, "src", "loader.ts")],
  outfile: join(outDir, ENTRY_FILENAME),
  external: [CHUNK_SPECIFIER],
});

/** Builds the card chunk. */
export async function buildChunk(outDir = DIST_DIR, { minify = true } = {}) {
  await esbuild.build(chunkOptions(outDir, minify));
  // Lit's generated template literals contain literal tabs before newlines.
  // Preserve their semantics using escapes so generated bundles pass git's
  // trailing-whitespace check.
  const chunk = join(outDir, CHUNK_FILENAME);
  writeFileSync(chunk, readFileSync(chunk, "utf8").replaceAll("\t\n", "\\t\n"));
}

/** Builds the gated loader, which imports the chunk by its relative name. */
export async function buildEntry(outDir = DIST_DIR, { minify = true } = {}) {
  await esbuild.build(entryOptions(outDir, minify));
}

if (import.meta.main) {
  const watch = process.argv.includes("--watch");
  if (watch) {
    const entry = await esbuild.context(entryOptions(DIST_DIR, false));
    const chunk = await esbuild.context(chunkOptions(DIST_DIR, false));
    await chunk.watch();
    await entry.watch();
    console.log("watching frontend/src for changes…");
  } else {
    await buildChunk();
    await buildEntry();
    console.log(
      `built dist/${ENTRY_FILENAME} and dist/${CHUNK_FILENAME} (v${manifest.version})`,
    );
  }
}

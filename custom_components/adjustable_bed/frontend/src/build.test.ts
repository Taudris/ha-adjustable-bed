import { afterAll, describe, expect, test } from "bun:test";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  CHUNK_FILENAME,
  ENTRY_FILENAME,
  buildChunk,
  buildEntry,
} from "../build.mjs";

const scratchDirs: string[] = [];

function scratch(): string {
  const dir = mkdtempSync(join(tmpdir(), "adjustable-bed-build-"));
  scratchDirs.push(dir);
  return dir;
}

afterAll(() => {
  for (const dir of scratchDirs) rmSync(dir, { recursive: true, force: true });
});

describe("build output", () => {
  test("repeats byte for byte from unchanged sources (content-keyed-chunk-path)", async () => {
    const first = scratch();
    const second = scratch();

    for (const dir of [first, second]) {
      await buildChunk(dir);
      await buildEntry(dir);
    }

    expect(readdirSync(second).sort()).toEqual(
      [CHUNK_FILENAME, ENTRY_FILENAME].sort(),
    );
    for (const filename of [ENTRY_FILENAME, CHUNK_FILENAME]) {
      expect(readFileSync(join(second, filename))).toEqual(
        readFileSync(join(first, filename)),
      );
    }
  });
});

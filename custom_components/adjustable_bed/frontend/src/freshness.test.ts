// Unit tests for the open-page freshness check. No DOM: the registry, the
// connection, and the reload are all handed in.
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { type RootLookup, bundleKey, reloadWhenStale } from "./freshness";
import type { HassConnection } from "./types";

/** A page defining `root` and nothing else: the gate's winner on that host. */
function fakePage(root: string): RootLookup {
  const stub = class {} as unknown as CustomElementConstructor;
  return { get: (name) => (name === root ? stub : undefined) };
}

const appPage = fakePage("home-assistant");
const castReceiver = fakePage("hc-main");

/** A connection answering with `key`, recording asks and `ready` listeners. */
function fakeConnection(key: string) {
  const asked: { type: string }[] = [];
  const ready: (() => unknown)[] = [];
  const connection: HassConnection = {
    sendMessagePromise: async (message) => {
      asked.push(message);
      return { cache_key: key };
    },
    addEventListener: (_event, handler) => {
      ready.push(handler);
    },
  };
  return { connection, asked, ready };
}

describe("freshness", () => {
  test("asks at load and on every reconnect (freshness-checked-on-connect)", async () => {
    const { connection, asked, ready } = fakeConnection("3.7.1-current");

    await reloadWhenStale(appPage, connection, "3.7.1-current", () => {
      throw new Error("a matching key must not reload");
    });

    expect(asked).toEqual([{ type: "adjustable_bed/card_freshness" }]);
    expect(ready).toHaveLength(1);

    await ready[0]();
    await ready[0]();

    expect(asked).toHaveLength(3);
  });

  test("reloads once on a key the server no longer serves (stale-page-reloads-at-once)", async () => {
    const { connection } = fakeConnection("3.7.1-current");
    let reloads = 0;

    await reloadWhenStale(appPage, connection, "3.7.1-loaded", () => {
      reloads += 1;
    });

    expect(reloads).toBe(1);
  });

  test("leaves a current page alone (stale-page-reloads-at-once)", async () => {
    const { connection, ready } = fakeConnection("3.7.1-current");
    let reloads = 0;

    await reloadWhenStale(appPage, connection, "3.7.1-current", () => {
      reloads += 1;
    });
    await ready[0]();

    expect(reloads).toBe(0);
  });

  test("leaves a Cast receiver alone (receiver-skipped-until-mapped)", async () => {
    const { connection, asked, ready } = fakeConnection("3.7.1-current");
    let reloads = 0;

    await reloadWhenStale(castReceiver, connection, "3.7.1-loaded", () => {
      reloads += 1;
    });

    expect(reloads).toBe(0);
    expect(asked).toEqual([]);
    expect(ready).toEqual([]);
  });

  test("reads the loaded key from the chunk's own URL (freshness-checked-on-connect)", () => {
    expect(
      bundleKey(
        "http://homeassistant.local:8123/adjustable_bed_frontend/3.7.1-abc123def456/adjustable-bed-card-chunk.js",
      ),
    ).toBe("3.7.1-abc123def456");
  });

  test("reloads on the answer alone, never on a timer (stale-page-reloads-at-once)", () => {
    const source = readFileSync(new URL("./freshness.ts", import.meta.url), "utf8");

    expect(source).not.toContain("setTimeout");
    expect(source).not.toContain("setInterval");
  });
});

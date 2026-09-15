import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { type RootRegistry, gate } from "./gate";

function read(path: string): string {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const entry = read("../dist/adjustable-bed-card.js");
const chunk = read("../dist/adjustable-bed-card-chunk.js");

/** A registry whose `whenDefined` promises settle only when `define` is called. */
function deferredRegistry() {
  const resolvers = new Map<string, () => void>();
  const asked: string[] = [];
  const settled = new Set<string>();
  const registry: RootRegistry = {
    whenDefined: (name) => {
      asked.push(name);
      return new Promise((resolve) => {
        resolvers.set(name, () => {
          settled.add(name);
          resolve(class {} as CustomElementConstructor);
        });
      });
    },
  };
  return { registry, asked, settled, define: (name: string) => resolvers.get(name)!() };
}

describe("gate", () => {
  test("waits while neither root element is defined (define-after-app-element)", async () => {
    const { registry, asked } = deferredRegistry();
    let resolved = false;

    void gate(registry).then(() => {
      resolved = true;
    });
    await new Promise<void>(queueMicrotask);

    expect(asked).toEqual(["home-assistant", "hc-main"]);
    expect(resolved).toBe(false);
  });

  test("resolves when the frontend defines home-assistant (define-after-app-element)", async () => {
    const { registry, settled, define } = deferredRegistry();
    const gated = gate(registry);

    define("home-assistant");
    await gated;

    expect(settled).toEqual(new Set(["home-assistant"]));
  });

  test("resolves when a Cast receiver defines hc-main (define-after-app-element)", async () => {
    const { registry, settled, define } = deferredRegistry();
    const gated = gate(registry);

    define("hc-main");
    await gated;

    expect(settled).toEqual(new Set(["hc-main"]));
  });
});

describe("built entry", () => {
  test("races both root elements, then imports once (define-after-app-element)", () => {
    expect(entry).toContain('"home-assistant"');
    expect(entry).toContain('"hc-main"');
    expect(entry.match(/import\(/g)).toHaveLength(1);
  });

  test("registers nothing itself (entry-defines-nothing)", () => {
    expect(entry).not.toContain("customElements.define");
    expect(entry).not.toContain("HTMLElement");
  });

  test("names the chunk by a literal relative specifier (chunk-evaluates-once)", () => {
    expect(entry).toMatch(
      /import\(\s*"\.\/adjustable-bed-card-chunk\.js"\s*\)/,
    );
  });

  test("waits on promises only, never on a timer (gate-is-a-promise)", () => {
    for (const text of [entry, read("./loader.ts"), read("./gate.ts")]) {
      expect(text).not.toContain("setTimeout");
      expect(text).not.toContain("setInterval");
    }
  });
});

describe("built chunk", () => {
  test.each(["adjustable-bed-card", "adjustable-bed-card-editor"])(
    "guards the %s definition (defines-are-guarded)",
    (name) => {
      const guard = chunk.indexOf(`customElements.get("${name}")`);
      const define = chunk.indexOf(`customElements.define("${name}"`);

      expect(guard).toBeGreaterThan(-1);
      expect(define).toBeGreaterThan(guard);
    },
  );
});

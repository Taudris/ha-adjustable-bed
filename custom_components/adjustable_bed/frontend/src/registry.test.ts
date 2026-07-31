// Unit tests for the custom element registry repair.
// Run with: bun test
import { expect, test } from "bun:test";
import { type ElementRegistry, keepDefined } from "./registry";

const CARD = "adjustable-bed-card";
const EDITOR = "adjustable-bed-card-editor";

const CardClass = class {} as unknown as CustomElementConstructor;
const EditorClass = class {} as unknown as CustomElementConstructor;
const OtherCardClass = class {} as unknown as CustomElementConstructor;

// Rejects a duplicate define like the real registry does, so a repair that
// defines over a live definition fails the test instead of passing quietly.
class FakeRegistry implements ElementRegistry {
  public defineCount = 0;
  private readonly _defined = new Map<string, CustomElementConstructor>();

  constructor(defined: Record<string, CustomElementConstructor> = {}) {
    for (const [tag, elementClass] of Object.entries(defined)) {
      this._defined.set(tag, elementClass);
    }
  }

  get(tag: string): CustomElementConstructor | undefined {
    return this._defined.get(tag);
  }

  define(tag: string, elementClass: CustomElementConstructor): void {
    if (this._defined.has(tag)) {
      throw new Error(`already defined: ${tag}`);
    }
    this.defineCount += 1;
    this._defined.set(tag, elementClass);
  }
}

// Collects the scheduled checks so a test drives time explicitly.
function testScheduler(): {
  schedule: (check: () => void, delayMs: number) => void;
  delays: number[];
  runNext: () => void;
  runAll: () => void;
} {
  const checks: Array<() => void> = [];
  const delays: number[] = [];
  return {
    schedule: (check, delayMs) => {
      checks.push(check);
      delays.push(delayMs);
    },
    delays,
    runNext: () => checks.shift()?.(),
    runAll: () => {
      while (checks.length) checks.shift()?.();
    },
  };
}

test("re-defines the elements when the registry is replaced", () => {
  const native = new FakeRegistry({ [CARD]: CardClass, [EDITOR]: EditorClass });
  const polyfilled = new FakeRegistry();
  let current: FakeRegistry = native;
  const scheduler = testScheduler();

  keepDefined(
    { [CARD]: CardClass, [EDITOR]: EditorClass },
    { registry: () => current, schedule: scheduler.schedule },
  );
  // Checks before the swap must not disturb the definitions already in place.
  scheduler.runNext();
  current = polyfilled;
  scheduler.runAll();

  expect(native.defineCount).toBe(0);
  expect(polyfilled.get(CARD)).toBe(CardClass);
  expect(polyfilled.get(EDITOR)).toBe(EditorClass);
  expect(polyfilled.defineCount).toBe(2);
});

test("leaves an intact registry untouched", () => {
  const registry = new FakeRegistry({
    [CARD]: CardClass,
    [EDITOR]: EditorClass,
  });
  const scheduler = testScheduler();

  keepDefined(
    { [CARD]: CardClass, [EDITOR]: EditorClass },
    { registry: () => registry, schedule: scheduler.schedule },
  );
  scheduler.runAll();

  expect(registry.defineCount).toBe(0);
});

test("keeps a definition another copy of the bundle already made", () => {
  const polyfilled = new FakeRegistry({ [CARD]: OtherCardClass });
  const scheduler = testScheduler();

  keepDefined(
    { [CARD]: CardClass, [EDITOR]: EditorClass },
    { registry: () => polyfilled, schedule: scheduler.schedule },
  );
  scheduler.runAll();

  expect(polyfilled.get(CARD)).toBe(OtherCardClass);
  expect(polyfilled.get(EDITOR)).toBe(EditorClass);
});

test("checks repeatedly across the first ten seconds", () => {
  const registry = new FakeRegistry({ [CARD]: CardClass });
  const scheduler = testScheduler();

  keepDefined({ [CARD]: CardClass }, {
    registry: () => registry,
    schedule: scheduler.schedule,
  });

  expect(scheduler.delays.length).toBeGreaterThanOrEqual(4);
  expect(scheduler.delays[0]).toBe(0);
  expect(scheduler.delays.at(-1)).toBe(10000);
  // Strictly increasing, so every check adds coverage rather than repeating.
  expect(scheduler.delays).toEqual([...scheduler.delays].sort((a, b) => a - b));
  expect(new Set(scheduler.delays).size).toBe(scheduler.delays.length);
});

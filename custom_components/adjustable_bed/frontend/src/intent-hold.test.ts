// Unit tests for the sample hold gesture. Run with: bun test
//
// The sender under it is the real one with its dependencies injected, so what a
// gesture puts on the wire is what these assert.
import { expect, test } from "bun:test";
import { IntentHold } from "./intent-hold";
import { type IntentSample, IntentSender } from "./intents";
import type { HoldControl } from "./types";

// One sample as the log names it. The card emits holds only, so the activate
// arm is the union's other half rather than a shape these tests ever see.
const logged = (s: IntentSample): string =>
  `${s.intent_id}:${s.control}@${
    s.action.kind === "hold" ? s.action.ttl_ms : "activate"
  }`;

interface Harness {
  hold: IntentHold;
  // Sends and bed stops in the order they happened.
  log: string[];
  advance: (ms: number) => void;
}

function harness(opts: { withoutSender?: boolean } = {}): Harness {
  const log: string[] = [];
  const timers: Array<{ at: number; fn: () => void; live: boolean }> = [];
  let now = 1000;
  let minted = 0;
  let hold!: IntentHold;
  const sender = new IntentSender({
    send: (set) => {
      log.push(`send ${set.samples.map(logged).join(" ")}`);
      return undefined;
    },
    report: (error) => log.push(`report ${String(error)}`),
    stranded: (handle) => hold.noteStranded(handle),
    schedule: (ms, fn) => {
      const timer = { at: now + ms, fn, live: true };
      timers.push(timer);
      return () => {
        timer.live = false;
      };
    },
    mintId: () => `id-${minted++}`,
    now: () => now,
  });
  hold = new IntentHold(
    () => (opts.withoutSender ? null : sender),
    (stopEntityId) => log.push(`stop-bed ${stopEntityId ?? "none"}`),
  );
  return {
    log,
    hold,
    advance: (ms: number) => {
      const until = now + ms;
      for (;;) {
        const due = timers
          .filter((t) => t.live && t.at <= until)
          .sort((a, b) => a.at - b.at)[0];
        if (!due) break;
        due.live = false;
        now = due.at;
        due.fn();
      }
      now = until;
    },
  };
}

const CAP_MS = 30000;

// One control as an entity publishes it: the roster's name and its cap.
const ctl = (control: string): HoldControl => ({ control, ttlMaxMs: CAP_MS });

// Every gesture a test starts is live unless the test is about liveness itself.
const LIVE = () => true;

// The sender's figures, restated rather than imported, as in intents.test.ts.
const TTL_MS = 750;
const MIN_PRESS_MS = 200;
const REFRESH_MS = 250;

test("presets-hold-only: a preset gesture is a hold on the tile's control", () => {
  const h = harness();

  h.hold.start(ctl("preset-1"), "button.bed_preset_memory_1", 1, LIVE);

  expect(h.log).toEqual(["send id-1:preset-1@750"]);
  expect(h.hold.heldKey).toBe("button.bed_preset_memory_1");
});

test("press-state-fidelity: one gesture is one intent id from finger down to release", () => {
  const h = harness();

  h.hold.start(ctl("motor-head-up"), "head", 1, LIVE);
  h.advance(MIN_PRESS_MS * 2);
  h.hold.endFromPointer("head", 1, true);

  const ids = new Set(
    h.log.map((line) => line.replace("send ", "").split(":")[0]),
  );
  expect([...ids]).toEqual(["id-1"]);
  expect(h.log[h.log.length - 1]).toBe("send id-1:motor-head-up@0");
});

test("press-state-fidelity: a re-render mid-gesture does not split the press", () => {
  const h = harness();

  h.hold.start(ctl("motor-head-up"), "head", 1, LIVE);
  h.advance(MIN_PRESS_MS);
  // render() rebuilds every MotorEntity, so the release handler names the same
  // control through a fresh object. Ownership is by key, so it still matches.
  h.hold.endFromPointer("head", 1, true);

  expect(h.hold.heldKey).toBeNull();
  expect(h.log[h.log.length - 1]).toBe("send id-1:motor-head-up@0");
});

test("press-state-fidelity: a second pointer's release does not end the gesture", () => {
  const h = harness();

  h.hold.start(ctl("motor-head-up"), "head", 1, LIVE);
  h.advance(MIN_PRESS_MS);
  h.hold.endFromPointer("head", 2, true);

  expect(h.hold.heldKey).toBe("head");
  // Still one live intent behind it: ending the gesture properly ends that id.
  h.hold.end("head");
  expect(h.log[h.log.length - 1]).toBe("send id-1:motor-head-up@0");
});

test("a non-primary button release does not end the gesture", () => {
  const h = harness();

  h.hold.start(ctl("motor-head-up"), "head", 1, LIVE);
  h.advance(MIN_PRESS_MS);
  h.hold.endFromPointer("head", 1, false);

  expect(h.hold.heldKey).toBe("head");
  expect(h.log).toEqual([`send id-1:motor-head-up@${TTL_MS}`]);
});

test("releasing one control does not end another's gesture", () => {
  const h = harness();

  h.hold.start(ctl("motor-head-up"), "head", 1, LIVE);
  h.advance(MIN_PRESS_MS);
  h.hold.end("legs");

  expect(h.hold.heldKey).toBe("head");
  // The head's intent is untouched: its own release is what ends it.
  h.hold.end("head");
  expect(h.log[h.log.length - 1]).toBe("send id-1:motor-head-up@0");
});

test("a second start while a control is held is ignored", () => {
  const h = harness();

  h.hold.start(ctl("motor-head-up"), "head", 1, LIVE);
  h.hold.start(ctl("preset-1"), "button.bed_preset_memory_1", 2, LIVE);

  expect(h.hold.heldKey).toBe("head");
  expect(h.log).toEqual(["send id-1:motor-head-up@750"]);
});

test("stopAll drops the live intent before it presses the bed's stop", () => {
  const h = harness();

  h.hold.start(ctl("motor-head-up"), "head", 1, LIVE);
  h.hold.stopAll("button.bed_stop");

  expect(h.log).toEqual([
    "send id-1:motor-head-up@750",
    "send id-1:motor-head-up@0",
    "stop-bed button.bed_stop",
  ]);
  expect(h.hold.heldKey).toBeNull();
});

test("stopAll presses the stop of the side the button belongs to", () => {
  const h = harness();

  h.hold.start(ctl("motor-head-up"), "head", 1, LIVE);
  h.hold.stopAll("button.bed_stop_left");

  expect(h.log[h.log.length - 1]).toBe("stop-bed button.bed_stop_left");
});

test("stopAll with nothing held still presses the bed's stop", () => {
  const h = harness();

  h.hold.stopAll("button.bed_stop");

  expect(h.log).toEqual(["stop-bed button.bed_stop"]);
});

test("cancel ends the gesture at once, without the bed-wide stop", () => {
  const h = harness();

  h.hold.start(ctl("motor-legs-down"), "legs", 1, LIVE);
  expect(h.hold.cancel("legs")).toBe(true);

  expect(h.log).toEqual([
    "send id-1:motor-legs-down@750",
    "send id-1:motor-legs-down@0",
  ]);
});

test("cancel ignores a control that does not hold", () => {
  const h = harness();

  h.hold.start(ctl("motor-legs-down"), "legs", 1, LIVE);

  expect(h.hold.cancel("head")).toBe(false);
  expect(h.hold.heldKey).toBe("legs");
});

test("abandoning mid-gesture ends the intent at once, floor or not", () => {
  const h = harness();

  h.hold.start(ctl("motor-head-up"), "head", 1, LIVE);
  h.hold.abandon();

  expect(h.log[h.log.length - 1]).toBe("send id-1:motor-head-up@0");
  expect(h.hold.heldKey).toBeNull();
});

test("abandoning with nothing held touches nothing", () => {
  const h = harness();

  h.hold.abandon();

  expect(h.log).toEqual([]);
});

test("a start with no sender does nothing, and leaves nothing held", () => {
  const h = harness({ withoutSender: true });

  h.hold.start(ctl("motor-head-up"), "head", 1, LIVE);

  expect(h.log).toEqual([]);
  expect(h.hold.heldKey).toBeNull();
});

test("a keyboard gesture has no owning pointer, so any release ends it", () => {
  const h = harness();

  h.hold.start(ctl("motor-head-up"), "head", null, LIVE);
  h.advance(MIN_PRESS_MS);
  h.hold.endFromPointer("head", 99, true);

  expect(h.hold.heldKey).toBeNull();
  expect(h.log[h.log.length - 1]).toBe("send id-1:motor-head-up@0");
});

test("a tap's release waits out the press floor before it rides a message", () => {
  const h = harness();

  h.hold.start(ctl("preset-1"), "button.bed_preset_memory_1", 1, LIVE);
  h.hold.end("button.bed_preset_memory_1");

  expect(h.log).toEqual([`send id-1:preset-1@${TTL_MS}`]);

  h.advance(MIN_PRESS_MS);

  expect(h.log[h.log.length - 1]).toBe("send id-1:preset-1@0");
});

test("a gesture whose element goes away is released, and the card holds nothing", () => {
  const h = harness();
  let live = true;

  h.hold.start(ctl("motor-head-up"), "head", 1, () => live);
  expect(h.hold.heldKey).toBe("head");

  // A re-render swapped the tile, so the captured button is gone and no
  // pointerup, pointercancel or blur will ever reach it.
  live = false;
  h.advance(REFRESH_MS);

  expect(h.log[h.log.length - 1]).toBe("send id-1:motor-head-up@0");
  expect(h.hold.heldKey).toBeNull();
});

test("a press after a stranded gesture is taken rather than refused", () => {
  const h = harness();
  let live = true;

  h.hold.start(ctl("motor-head-up"), "head", 1, () => live);
  live = false;
  h.advance(REFRESH_MS);

  h.hold.start(ctl("motor-feet-up"), "feet", 2, LIVE);

  expect(h.hold.heldKey).toBe("feet");
  expect(h.log[h.log.length - 1]).toBe(`send id-2:motor-feet-up@${TTL_MS}`);
});

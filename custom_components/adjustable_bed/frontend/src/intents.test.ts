// Unit tests for the card's intent sender — the client half of the hold
// contract. Run with: bun test
//
// Every dependency is injected, so a test drives the clock rather than waiting
// on one: nothing here is timing-dependent.
import { expect, test } from "bun:test";
import {
  type IntentSample,
  type IntentSampleSet,
  type IntentHandle,
  type IntentSenderDeps,
  IntentSender,
  mintIntentId,
} from "./intents";

interface Harness {
  deps: IntentSenderDeps;
  sent: IntentSampleSet[];
  // Every failed send the sender reported.
  reported: unknown[];
  // Every intent the sender released because its gesture stopped being live.
  stranded: IntentHandle[];
  // Moves the clock, firing whatever falls due on the way.
  advance: (ms: number) => void;
}

function harness(opts: { reject?: boolean } = {}): Harness {
  const sent: IntentSampleSet[] = [];
  const reported: unknown[] = [];
  const stranded: IntentHandle[] = [];
  const timers: Array<{ at: number; fn: () => void; live: boolean }> = [];
  let now = 1000;
  let minted = 0;
  return {
    sent,
    reported,
    stranded,
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
    deps: {
      send: (set) => {
        sent.push(set);
        return opts.reject
          ? Promise.reject(new Error("websocket is down"))
          : Promise.resolve();
      },
      report: (error) => reported.push(error),
      stranded: (handle) => stranded.push(handle),
      schedule: (ms, fn) => {
        const timer = { at: now + ms, fn, live: true };
        timers.push(timer);
        return () => {
          timer.live = false;
        };
      },
      mintId: () => `id-${minted++}`,
      now: () => now,
    },
  };
}

const MOTOR_CAP_MS = 30000;

// Every hold a test starts is live unless the test is about liveness itself.
const LIVE = () => true;

// The sender's figures, restated here rather than imported: a test that reads
// the constant it checks pins nothing, and these are the contract the plan's
// derivations settled.
const REFRESH_MS = 250;
const TTL_MS = 750;
const MIN_PRESS_MS = 200;

// The ttl a sample's action carries, or null for an activate. The card emits
// holds only, so a null here is a test failure rather than a case to handle.
function ttlOf(sample: IntentSample): number | null {
  return sample.action.kind === "hold" ? sample.action.ttl_ms : null;
}

test("client-sample-sets: every message carries the complete active set under one seq", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);

  sender.hold({ control: "motor-head-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  sender.hold({ control: "motor-feet-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);

  expect(h.sent).toHaveLength(2);
  expect(h.sent[0].seq).toBe(0);
  expect(h.sent[1].seq).toBe(1);
  expect(h.sent[1].samples.map((s) => s.control)).toEqual([
    "motor-head-up",
    "motor-feet-up",
  ]);
  // One sender id for the life of the sender, on every message.
  expect(h.sent[0].sender_id).toBe(h.sent[1].sender_id);
});

test("client-sample-sets: a ttl-0 sample rides one message beside the live one, then leaves the set", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);

  const head = sender.hold({ control: "motor-head-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  sender.hold({ control: "motor-feet-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  h.advance(MIN_PRESS_MS);
  sender.release(head);

  const ending = h.sent[h.sent.length - 1];
  expect(ending.samples).toEqual([
    {
      intent_id: "id-1",
      control: "motor-head-up",
      action: { kind: "hold", ttl_ms: 0 },
    },
    {
      intent_id: "id-2",
      control: "motor-feet-up",
      action: { kind: "hold", ttl_ms: TTL_MS },
    },
  ]);

  h.advance(REFRESH_MS);
  const next = h.sent[h.sent.length - 1];
  expect(next.samples.map((s) => s.intent_id)).toEqual(["id-2"]);
});

test("client-sample-sets: no empty set ever leaves, and the refresh stops with the last hold", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);

  const head = sender.hold({ control: "motor-head-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  h.advance(MIN_PRESS_MS);
  sender.release(head);
  const afterRelease = h.sent.length;

  h.advance(REFRESH_MS * 10);

  expect(h.sent).toHaveLength(afterRelease);
  expect(h.sent.every((set) => set.samples.length > 0)).toBe(true);
});

test("client-sample-sets: the set is re-sent every T while anything is held", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);

  sender.hold({ control: "motor-head-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  h.advance(REFRESH_MS * 3);

  expect(h.sent).toHaveLength(4);
  expect(h.sent.map((set) => set.seq)).toEqual([0, 1, 2, 3]);
  for (const set of h.sent) {
    expect(set.samples).toEqual([
      {
        intent_id: "id-1",
        control: "motor-head-up",
        action: { kind: "hold", ttl_ms: TTL_MS },
      },
    ]);
  }
});

test("client-sample-sets: a lost message costs nothing — the next refresh re-asserts inside the ttl", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);

  sender.hold({ control: "motor-head-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  h.advance(REFRESH_MS * 2);

  // Pretend the middle message never arrived: the ttl covers the gap to the
  // next one, which asserts the same intent again.
  const survived = [h.sent[0], h.sent[2]];
  expect(survived[1].seq - survived[0].seq).toBe(2);
  expect(REFRESH_MS * 2).toBeLessThan(TTL_MS);
  expect(survived[1].samples[0].intent_id).toBe(survived[0].samples[0].intent_id);
});

test("client-sample-sets: the card sends no activate, so the one-message clause is vacuous here", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);

  sender.hold({ control: "preset-1", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  h.advance(REFRESH_MS);

  // An Activate rides exactly one message and leaves the set. This card emits
  // holds only — a light or massage tap keeps its button.press — so the action's
  // kind is "hold" on every sample there is.
  expect(
    h.sent.every((set) => set.samples.every((s) => s.action.kind === "hold")),
  ).toBe(true);
});

test("intents-are-parameterized: a sample carries a control, an action and a ttl, and nothing else", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);

  sender.hold({ control: "preset-1", ttlMaxMs: MOTOR_CAP_MS }, LIVE);

  const [sample] = h.sent[0].samples;
  expect(Object.keys(sample).sort()).toEqual(["action", "control", "intent_id"]);
  // The ttl is the hold variant's own parameter, so it is inside the action
  // rather than a sibling an activate could carry too.
  expect(Object.keys(sample.action).sort()).toEqual(["kind", "ttl_ms"]);
  expect(Object.keys(h.sent[0]).sort()).toEqual(["samples", "sender_id", "seq"]);
});

test("intents-are-time-bounded: every sample on every path carries a ttl", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);

  const head = sender.hold({ control: "motor-head-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  h.advance(REFRESH_MS);
  const feet = sender.hold({ control: "motor-feet-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  h.advance(MIN_PRESS_MS);
  sender.release(head);
  sender.drop(feet);

  expect(h.sent.length).toBeGreaterThan(3);
  for (const set of h.sent) {
    for (const sample of set.samples) {
      expect(typeof ttlOf(sample)).toBe("number");
    }
  }
});

test("key-registration: a release inside the press floor waits for it", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);

  const head = sender.hold({ control: "motor-head-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  h.advance(30);
  sender.release(head);

  // The press is still live: a release the streamer sees before it can have
  // expressed the press would express nothing at all.
  expect(ttlOf(h.sent[h.sent.length - 1].samples[0])).toBe(TTL_MS);

  h.advance(MIN_PRESS_MS - 30);

  expect(ttlOf(h.sent[h.sent.length - 1].samples[0])).toBe(0);

  // The intent left the set with that send, so nothing refreshes it after.
  const ended = h.sent.length;
  h.advance(REFRESH_MS * 3);
  expect(h.sent).toHaveLength(ended);
});

test("key-registration: drop bypasses the press floor", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);

  const head = sender.hold({ control: "motor-head-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  h.advance(30);
  sender.drop(head);

  expect(ttlOf(h.sent[h.sent.length - 1].samples[0])).toBe(0);

  // The intent left the set with that send, so nothing refreshes it after.
  const ended = h.sent.length;
  h.advance(REFRESH_MS * 3);
  expect(h.sent).toHaveLength(ended);
});

test("key-registration: a release after the floor rides a message at once", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);

  const head = sender.hold({ control: "motor-head-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  h.advance(MIN_PRESS_MS + 1);
  const before = h.sent.length;
  sender.release(head);

  expect(h.sent).toHaveLength(before + 1);
  expect(ttlOf(h.sent[before].samples[0])).toBe(0);
});

test("stop-fences-the-control: one press keeps one intent id past the lifetime cap", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);

  sender.hold({ control: "motor-head-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  const pressId = h.sent[0].samples[0].intent_id;
  h.advance(MOTOR_CAP_MS + REFRESH_MS * 4);

  // Every message the press ever sends names the id it was born under. A
  // successor id would be a press the server never fenced, so a stop taken
  // during this hold would stop the bed and the next refresh would start it
  // again; the cap ends the hold at the server and the user re-presses.
  for (const message of h.sent) {
    expect(message.samples.map((s) => s.intent_id)).toEqual([pressId]);
    expect(ttlOf(message.samples[0])).toBe(TTL_MS);
  }
});

test("a refresh releases a gesture that is no longer live, and stops refreshing", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);
  let live = true;

  const head = sender.hold(
    { control: "motor-head-up", ttlMaxMs: MOTOR_CAP_MS },
    () => live,
  );
  h.advance(REFRESH_MS);
  expect(ttlOf(h.sent[h.sent.length - 1].samples[0])).toBe(TTL_MS);

  // The card re-rendered the tile away, so no pointerup, pointercancel or blur
  // will ever reach the button this gesture started on.
  live = false;
  h.advance(REFRESH_MS);

  expect(ttlOf(h.sent[h.sent.length - 1].samples[0])).toBe(0);
  expect(h.stranded).toEqual([head]);

  const ended = h.sent.length;
  h.advance(REFRESH_MS * 4);
  expect(h.sent).toHaveLength(ended);
});

test("a refresh keeps the live gestures while it releases the stranded one", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);
  let headIsLive = true;

  sender.hold({ control: "motor-head-up", ttlMaxMs: MOTOR_CAP_MS }, () => headIsLive);
  sender.hold({ control: "motor-feet-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);

  headIsLive = false;
  h.advance(REFRESH_MS);

  const message = h.sent[h.sent.length - 1];
  expect(message.samples.map((s) => [s.control, ttlOf(s)])).toEqual([
    ["motor-head-up", 0],
    ["motor-feet-up", TTL_MS],
  ]);

  h.advance(REFRESH_MS);
  expect(h.sent[h.sent.length - 1].samples.map((s) => s.control)).toEqual([
    "motor-feet-up",
  ]);
});

test("a rejected send neither stops the refresh nor retries out of band", async () => {
  const h = harness({ reject: true });
  const sender = new IntentSender(h.deps);

  sender.hold({ control: "motor-head-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  h.advance(REFRESH_MS * 3);

  // One message per tick and not one more: the refresh is the cadence, never a
  // retry of what failed.
  expect(h.sent).toHaveLength(4);

  // Each failure is reported once, by the sender, and reaches the injected
  // reporter rather than an unhandled rejection.
  await Promise.resolve();
  expect(h.reported).toHaveLength(4);
});

test("an abandoned sender ends every live intent at once, floor or not", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);

  sender.hold({ control: "motor-head-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  sender.hold({ control: "motor-feet-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  sender.abandon();

  const last = h.sent[h.sent.length - 1];
  expect(last.samples.map(ttlOf)).toEqual([0, 0]);

  h.advance(REFRESH_MS * 4);
  expect(h.sent[h.sent.length - 1]).toBe(last);
});

test("abandoning with nothing held sends no message", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);

  sender.abandon();

  expect(h.sent).toHaveLength(0);
});

test("abandoning cancels a release still waiting on the press floor", () => {
  const h = harness();
  const sender = new IntentSender(h.deps);

  const head = sender.hold({ control: "motor-head-up", ttlMaxMs: MOTOR_CAP_MS }, LIVE);
  sender.release(head);
  sender.abandon();
  const afterAbandon = h.sent.length;

  h.advance(MIN_PRESS_MS * 2);

  expect(h.sent).toHaveLength(afterAbandon);
});

test("an intent id is 128 bits of randomness, hex-encoded", () => {
  const id = mintIntentId();

  expect(id).toMatch(/^[0-9a-f]{32}$/);
  expect(mintIntentId()).not.toBe(id);
});

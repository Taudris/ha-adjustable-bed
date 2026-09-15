// The card's wire protocol for hold intents: one sender per bed, sampling what
// the user is asking for and re-sending the complete active set every T while
// anything is held.
//
// Free of DOM, Home Assistant and real timers — every dependency is injected —
// so the rules below are unit tested rather than watched in a browser.

// A control's roster name, as the bed publishes it. Opaque here: the card never
// parses one and never composes one.
export type ControlId = string;
// One press, as the sender mints it. Opaque to the server too.
export type IntentId = string;

// One control a bed publishes on the entity that renders it: the roster's name
// for it and its ttl cap. Both come from the same publisher, so an entity
// declares a control with its cap or declares nothing at all — which is what a
// hold gesture needs, and what routes a bed to one strategy or the other. It is
// one fact, so it travels as one value.
export interface HoldControl {
  control: ControlId;
  ttlMaxMs: number;
}

// What a sample asks of its control, tagged by kind. A hold carries the ttl it
// is bounded by and an activate carries nothing, so neither an activate with a
// ttl nor a hold without one is a shape this type can express.
export type IntentAction =
  | { kind: "hold"; ttl_ms: number }
  | { kind: "activate" };

export interface IntentSample {
  intent_id: IntentId;
  control: ControlId;
  // The card samples holds only. A light or massage tap keeps its button.press,
  // which reaches the reconstructor as an Activate-born intent, so no card
  // gesture ever emits the activate variant.
  action: IntentAction;
}

export interface IntentSampleSet {
  sender_id: string;
  seq: number;
  samples: IntentSample[];
}

// Whether the gesture an intent came from is still the one that started it.
// Checked before every refresh, because the events that would otherwise end a
// hold - pointerup, pointercancel, blur - never reach an element a re-render
// has already discarded.
export type Liveness = () => boolean;

export interface IntentSenderDeps {
  // Sends one message and hands back whatever the transport gives, unguarded:
  // the sender is the one place a failure is reported, so a caller wrapping
  // this in its own catch would report the same failure twice.
  send: (set: IntentSampleSet) => Promise<unknown> | undefined;
  // Reports a send that failed. The sender keeps its cadence either way and
  // never retries: the ttl bounds what a lost message can do.
  report: (error: unknown) => void;
  // Reports an intent the sender released because its gesture stopped being
  // live. The holder's own bookkeeping outlives the sender's otherwise, and a
  // holder that still believes a control is held refuses every later press.
  stranded: (handle: IntentHandle) => void;
  // Runs fn once after ms, returning the cancel for it.
  schedule: (ms: number, fn: () => void) => () => void;
  mintId: () => string;
  now: () => number;
}

// The four figures below are the sender's own: nothing outside this module
// reads one, so none is exported. Each carries its derivation.
//
// The refresh interval T. TTL_MS / 3: a lost message delays a refresh by one
// send, so a ttl of three refreshes rides out two consecutive losses.
const REFRESH_MS = 250;

// The ttl every sample carries. Motion after a client stops refreshing is
// bounded by ttl + one sustain window = 750 + 218 ~= 968 ms, no worse than the
// single 1000 ms press the pulse path already issues per pulse, and far under
// the 30 s roster cap, so the server's clamp never binds.
const TTL_MS = 750;

// The client press floor: how long a press lives before its release can ride a
// message. Above the streamer's F = 100 ms emission tick, so a press outlives
// the next pump wake and is expressed at all; below the streamer's own 223 ms
// press floor, which the box is draining anyway, so it adds no latency the bed
// was not already imposing.
const MIN_PRESS_MS = 200;

// Sixteen random bytes, hex-encoded. Not crypto.randomUUID(), which is gated on
// a secure context: a Home Assistant reached over plain HTTP at a LAN name has
// none, so that member is undefined for a large share of this integration's
// users. getRandomValues carries no such gate.
export function mintIntentId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

// One live intent: one physical press, under one id for the whole of its life.
// The server's lifetime cap ends the hold where a press outlasts it, and the
// user releases and presses again; minting a successor id would re-assert a
// control the server may have fenced in the meantime.
class LiveIntent {
  cancelPendingRelease: (() => void) | null = null;

  constructor(
    readonly control: ControlId,
    // When the release may first ride a message, fixed at the press.
    readonly floorAt: number,
    readonly intentId: IntentId,
    readonly isLive: Liveness,
  ) {}
}

// What the caller holds for one gesture. Opaque by construction: this is the
// whole of what a holder may read, and the mutable state a roll moves is the
// sender's own.
export interface IntentHandle {
  readonly control: ControlId;
}

export class IntentSender {
  private readonly _senderId: string;
  private _seq = 0;
  private readonly _active = new Set<LiveIntent>();
  private _cancelRefresh: (() => void) | null = null;

  constructor(private readonly deps: IntentSenderDeps) {
    this._senderId = deps.mintId();
  }

  // Assert a control until the handle is released, or until ``isLive`` stops
  // answering for the gesture that began it.
  hold(held: HoldControl, isLive: Liveness): IntentHandle {
    const intent = new LiveIntent(
      held.control,
      this.deps.now() + MIN_PRESS_MS,
      this.deps.mintId(),
      isLive,
    );
    this._active.add(intent);
    this._send();
    this._armRefresh();
    return intent;
  }

  // End a press the user ended. Held to the press floor: a release the streamer
  // receives before it can have expressed the press would express nothing at
  // all, so a tap would move nothing.
  release(handle: IntentHandle): void {
    const intent = this._live(handle);
    const wait = intent.floorAt - this.deps.now();
    if (wait <= 0) {
      this._end(intent);
      return;
    }
    intent.cancelPendingRelease = this.deps.schedule(wait, () => {
      intent.cancelPendingRelease = null;
      this._end(intent);
    });
  }

  // End a press at once, floor or not — the card's stop, which the server
  // fences anyway. The local end is what stops the card re-asserting it.
  drop(handle: IntentHandle): void {
    const intent = this._live(handle);
    intent.cancelPendingRelease?.();
    intent.cancelPendingRelease = null;
    this._end(intent);
  }

  // Every handle this sender hands out is one of its own LiveIntents; the
  // narrow type is what stops a caller reading or writing its innards.
  private _live(handle: IntentHandle): LiveIntent {
    return handle as LiveIntent;
  }

  // End every live intent at once, for the card leaving the DOM. The floor
  // bounds a release the user asked for; it never delays an end the card is
  // forced into.
  abandon(): void {
    if (this._active.size === 0) return;
    this._cancelRefreshTimer();
    for (const intent of this._active) {
      intent.cancelPendingRelease?.();
      intent.cancelPendingRelease = null;
    }
    this._send(this._active);
    this._active.clear();
  }

  // Ends one intent: its ttl-0 sample rides one message beside whatever else is
  // held, and the intent leaves the set after that send.
  private _end(handle: LiveIntent): void {
    // Cancelling first is what keeps a refresh from following a ttl-0 sample
    // for the same id; both run on the browser's single thread, so nothing can
    // interleave between here and the send.
    this._cancelRefreshTimer();
    this._send(new Set([handle]));
    this._active.delete(handle);
    if (this._active.size > 0) this._armRefresh();
  }

  // One refresh: whatever is still live is re-asserted, and whatever is not is
  // released in the same message. A gesture whose element a re-render replaced
  // or whose pointer went elsewhere gets no pointer event at all, so this is
  // the only thing that ends it.
  private _refresh(): void {
    this._cancelRefresh = null;
    const stranded = new Set<LiveIntent>();
    for (const intent of this._active) {
      if (!intent.isLive()) stranded.add(intent);
    }
    this._send(stranded);
    for (const intent of stranded) {
      intent.cancelPendingRelease?.();
      intent.cancelPendingRelease = null;
      this._active.delete(intent);
      this.deps.stranded(intent);
    }
    if (this._active.size > 0) this._armRefresh();
  }

  private _armRefresh(): void {
    this._cancelRefreshTimer();
    this._cancelRefresh = this.deps.schedule(REFRESH_MS, () => this._refresh());
  }

  private _cancelRefreshTimer(): void {
    this._cancelRefresh?.();
    this._cancelRefresh = null;
  }

  // Sends the complete active set under the next seq. The set is never empty
  // here: a hold adds before sending, an end sends before removing, and the
  // refresh timer only runs while something is held.
  private _send(ending: ReadonlySet<LiveIntent> = new Set()): void {
    const samples: IntentSample[] = [];
    for (const intent of this._active) {
      samples.push({
        intent_id: intent.intentId,
        control: intent.control,
        action: { kind: "hold", ttl_ms: ending.has(intent) ? 0 : TTL_MS },
      });
    }
    void this.deps
      .send({ sender_id: this._senderId, seq: this._seq++, samples })
      ?.catch((error: unknown) => this.deps.report(error));
  }
}

// The sample hold strategy: the gesture the card runs on a bed whose entities
// publish hold controls. MotorHold's counterpart, with the same member names
// and the same ownership rules, so the card's event wiring is one branch rather
// than two shapes.
//
// It owns one gesture — which control the finger holds, and the sender handle
// for it — and borrows the sender through a getter, because the card rebuilds
// the sender when its device_id changes and a held reference would go stale.
import type { IntentHandle, IntentSender, Liveness } from "./intents";
import type { HoldControl } from "./types";

export class IntentHold {
  // Ownership is tracked by key rather than object identity, for the reason
  // MotorHold tracks it that way: a state change re-renders the card and
  // rebuilds every entity object, so a release handler would otherwise hold a
  // different object for the same control and never end the gesture.
  private _key: string | null = null;
  // The pointer that owns the gesture, null for keyboard activation. A second
  // touch's release must not end the primary finger's hold.
  private _pointerId: number | null = null;
  private _live: IntentHandle | null = null;

  constructor(
    private readonly sender: () => IntentSender | null,
    // The stop that covers the held control, which on a paired bed is the
    // side's own rather than the parent's — the same argument MotorHold's stop
    // takes, from the same call sites.
    private readonly stopBed: (stopEntityId?: string) => void,
  ) {}

  get heldKey(): string | null {
    return this._key;
  }

  // Begin a hold on `hold`, the control the entity published with its lifetime
  // cap. `key` is what the card's release handlers name it by — a motor's key,
  // a tile's entity id — and `pointerId` is null for keyboard activation.
  // `isLive` answers whether the gesture is still the one that began, which the
  // sender asks before each refresh. Ignored when another control already holds.
  start(
    hold: HoldControl,
    key: string,
    pointerId: number | null,
    isLive: Liveness,
  ): void {
    if (this._key !== null) return;
    const sender = this.sender();
    if (sender === null) return;
    this._key = key;
    this._pointerId = pointerId;
    this._live = sender.hold(hold, isLive);
  }

  // The sender released a hold whose gesture stopped being live, so this one's
  // bookkeeping has to go with it: a key left set refuses every later press.
  noteStranded(handle: IntentHandle): void {
    if (handle === this._live) this._reset();
  }

  // A pointer-driven release. Only the pointer that started the gesture may end
  // it, and a non-primary button release must not.
  endFromPointer(
    key: string,
    pointerId: number,
    isPrimaryButtonRelease: boolean,
  ): void {
    if (this._pointerId !== null && pointerId !== this._pointerId) return;
    if (!isPrimaryButtonRelease) return;
    this.end(key);
  }

  // End the gesture the user ended. The sender holds the release to its press
  // floor, so a tap still reaches the bed as a press. Releasing one control
  // must not end another's gesture: a blur on some other control's button would
  // otherwise stop the bed that is moving.
  end(key: string): void {
    const handle = this._live;
    if (handle === null || this._key !== key) return;
    this._reset();
    this.sender()?.release(handle);
  }

  // End the gesture at once, for a control whose stop the server is already
  // fencing. Returns whether there was a matching hold.
  cancel(key: string): boolean {
    const handle = this._live;
    if (handle === null || this._key !== key) return false;
    this._reset();
    this.sender()?.drop(handle);
    return true;
  }

  // A stop button. It stops whatever is moving, so it invalidates any gesture
  // rather than one control's. Dropping first is what stops the card
  // re-asserting a press the stop has already fenced. `stopEntityId` is the
  // pressed button's own stop.
  stopAll(stopEntityId?: string): void {
    const handle = this._live;
    this._reset();
    if (handle !== null) this.sender()?.drop(handle);
    this.stopBed(stopEntityId);
  }

  // The card left the DOM mid-gesture, so no pointer release will ever arrive.
  // Every live intent ends immediately: the press floor bounds a release the
  // user asked for, never an end the card is forced into. No stop is pressed —
  // the ttl-0 message is the release, and it reaches the bed either way.
  abandon(): void {
    if (this._live === null) return;
    this._reset();
    this.sender()?.abandon();
  }

  private _reset(): void {
    this._key = null;
    this._pointerId = null;
    this._live = null;
  }
}

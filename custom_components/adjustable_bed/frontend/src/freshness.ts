// The freshness check that recovers a dashboard left open across a deploy.
//
// Nothing tells a loaded document that the integration's bundle changed: a
// websocket reconnect re-reads no resources, and a document evaluates a module
// URL once, so an open page keeps running the card it loaded until something
// reloads it. This module makes the page ask instead, comparing the cache key
// in its own URL against the key the server serves.
//
// It asks at two moments, and needs both. The connection's first `ready` fires
// before any listener a card can attach, so a page that loads after a deploy
// would never hear one; every later `ready` is a genuine reconnect, which is
// what a restarted server produces. The frontend's own update check works
// around the same gap the same way.
//
// A mismatch reloads at once, with no idle test and no toast. A page goes
// stale only at the reconnect that follows a restart, when the bed's link is
// already down and no hold survives on the server, so there is nothing on the
// page to protect, and a card whose contract changed cannot work until it
// reloads anyway.
//
// Only the app page runs any of it. A Cast receiver renders the same card from
// a page of its own, and what a reload does to a running cast session is
// unread: the sender may re-send its connect message and the card come back, or
// the receiver may go dark until someone casts again. Until that is mapped a
// receiver keeps the behavior it had before this check existed: a stale card
// until it is re-cast.
import type { HassConnection } from "./types";

const FRESHNESS_COMMAND = "adjustable_bed/card_freshness";

/** Root element of the app page. A Cast receiver defines `hc-main` instead. */
const APP_ROOT = "home-assistant";

interface CardFreshness {
  cache_key: string;
}

/** The one registry capability the host check needs. */
export type RootLookup = Pick<CustomElementRegistry, "get">;

/** The cache key a served bundle URL carries: the segment before the filename. */
export function bundleKey(moduleUrl: string): string {
  const segments = new URL(moduleUrl).pathname.split("/");
  return segments[segments.length - 2];
}

/**
 * Reloads the document as soon as the server stops serving the cache key this
 * document loaded, checking now and on every reconnect after it.
 *
 * On the app page only. The gate that let this chunk evaluate raced
 * `home-assistant` against a Cast receiver's `hc-main`, so which of the two
 * `registry` holds is which host this is, and a receiver is left alone.
 */
export async function reloadWhenStale(
  registry: RootLookup,
  connection: HassConnection,
  loadedKey: string,
  reload: () => void,
): Promise<void> {
  if (!registry.get(APP_ROOT)) return;

  const check = async (): Promise<void> => {
    const answer = (await connection.sendMessagePromise({
      type: FRESHNESS_COMMAND,
    })) as CardFreshness;
    if (answer.cache_key !== loadedKey) reload();
  };

  connection.addEventListener("ready", check);
  await check();
}

let watching = false;

/** Starts this document's one watch, on the first `hass` a card receives. */
export function watchCardFreshness(connection: HassConnection): void {
  if (watching) return;
  watching = true;
  void reloadWhenStale(
    customElements,
    connection,
    bundleKey(import.meta.url),
    () => location.reload(),
  );
}

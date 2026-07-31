// Keeps this bundle's elements visible to Home Assistant's custom element
// registry.
//
// app.js imports @webcomponents/scoped-custom-element-registry, which replaces
// window.customElements outright with a registry that knows only what was
// defined through it: get() and whenDefined() never consult the native
// registry, and nothing carries earlier definitions across. When the
// integration falls back to add_extra_js_url (YAML resource mode), this bundle
// is imported from index.html alongside app.js and can finish first, and its
// elements are then invisible to Lovelace for the life of the document. The
// editor is where that shows: its element lookup rejects after 2 s with
// "Custom element not found" (home-assistant/frontend#52960).
//
// Defining again after the swap repairs it. The polyfill's define() reuses the
// class already registered natively rather than failing, and resolves the
// whenDefined promises Lovelace is waiting on.

/** The part of CustomElementRegistry the repair uses. */
export interface ElementRegistry {
  get(tag: string): CustomElementConstructor | undefined;
  define(tag: string, elementClass: CustomElementConstructor): void;
}

export interface KeepDefinedOptions {
  /** Read afresh per check: the object under window.customElements changes. */
  registry?: () => ElementRegistry;
  schedule?: (check: () => void, delayMs: number) => void;
  /** Spans the window in which app.js can still evaluate after this bundle. */
  delaysMs?: readonly number[];
}

const DEFAULT_DELAYS_MS: readonly number[] = [0, 250, 1000, 3000, 6000, 10000];

/**
 * Re-define `definitions` whenever the current registry has lost them.
 *
 * Call once, after the elements are defined; the checks run on timers and cost
 * one lookup per tag each.
 */
export function keepDefined(
  definitions: Readonly<Record<string, CustomElementConstructor>>,
  options: KeepDefinedOptions = {},
): void {
  const registryOf = options.registry ?? (() => window.customElements);
  const schedule =
    options.schedule ?? ((check, delayMs) => void setTimeout(check, delayMs));
  const entries = Object.entries(definitions);

  const redefineMissing = (): void => {
    const registry = registryOf();
    for (const [tag, elementClass] of entries) {
      if (!registry.get(tag)) {
        registry.define(tag, elementClass);
      }
    }
  };

  for (const delayMs of options.delaysMs ?? DEFAULT_DELAYS_MS) {
    schedule(redefineMissing, delayMs);
  }
}

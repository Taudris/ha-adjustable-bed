// The load gate the entry awaits before it imports the card chunk, so every
// definition the chunk makes lands after the host application has defined its
// root element.
//
// Registering any earlier is what breaks the card. On the main frontend,
// app.js replaces window.customElements wholesale before it defines its own
// elements, and a definition made against the registry it replaced is invisible
// to both the Lovelace view and the card editor. Waiting for `home-assistant`
// puts every definition after that replacement. Home Assistant's Cast receiver
// never defines `home-assistant`, so the gate races that name against the
// receiver's own root, `hc-main`; whichever loses stays pending and costs
// nothing.
//
// There is no timeout. `whenDefined` is the only signal either host offers, and
// a page that defines neither root renders no dashboard to put a card in.

/** The one registry capability the gate needs. */
export type RootRegistry = Pick<CustomElementRegistry, "whenDefined">;

/** Root element of each host application that renders Lovelace views. */
const ROOT_ELEMENTS = ["home-assistant", "hc-main"];

/** Resolves once the host application defines one of its root elements. */
export async function gate(registry: RootRegistry): Promise<void> {
  await Promise.race(ROOT_ELEMENTS.map((name) => registry.whenDefined(name)));
}

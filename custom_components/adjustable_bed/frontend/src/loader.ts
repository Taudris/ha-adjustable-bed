// The emitted entry: the module Home Assistant serves and injects as
// adjustable-bed-card.js.
import { gate } from "./gate";

void gate(customElements).then(() => import("./adjustable-bed-card-chunk.js"));

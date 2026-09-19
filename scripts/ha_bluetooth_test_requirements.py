"""Print Bluetooth test dependencies pinned by the installed HA release.

HA installs integration requirements on demand; installing the core wheel and
its pytest helper alone can otherwise select a newer Bluetooth stack.
"""

import json
from importlib.resources import files

for domain in ("bluetooth", "esphome"):
    manifest = files("homeassistant").joinpath("components", domain, "manifest.json")
    print("\n".join(json.loads(manifest.read_text())["requirements"]))

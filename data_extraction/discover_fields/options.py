from __future__ import annotations

from ..shared.contracts import StrictContract


class FieldDiscoveryOptions(StrictContract):
    """Feature-owned configuration contract.

    Discovery patterns remain deterministic in v0.24; no behavior setting is
    exposed yet. The explicit contract keeps feature composition consistent.
    """

    pass

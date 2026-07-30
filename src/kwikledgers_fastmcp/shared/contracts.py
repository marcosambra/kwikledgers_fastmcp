from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityStatus:
    capability_name: str
    implementation_status: str
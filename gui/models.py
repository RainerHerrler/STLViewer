from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RenderProgress:
    total: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0


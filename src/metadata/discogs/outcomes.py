"""Explicit outcomes for speculative Discogs collection refreshes."""

from dataclasses import dataclass
from enum import Enum, auto
from types import MappingProxyType
from typing import Any, Mapping, Optional


class CollectionRefreshState(Enum):
    OWNED = auto()
    CLEAN_NO_MATCH = auto()
    COOLDOWN_SKIPPED = auto()


@dataclass(frozen=True)
class CollectionRefreshResult:
    state: CollectionRefreshState
    result: Optional[Mapping[str, Any]] = None
    cooldown_follows_successful_rebuild: bool = False

    def __post_init__(self):
        if (self.state is CollectionRefreshState.OWNED) != (self.result is not None):
            raise ValueError("OWNED requires a result; other states forbid one")
        if (self.state is not CollectionRefreshState.COOLDOWN_SKIPPED
                and self.cooldown_follows_successful_rebuild):
            raise ValueError("cooldown provenance is valid only for a skipped refresh")
        if self.result is not None:
            object.__setattr__(self, "result", MappingProxyType(dict(self.result)))

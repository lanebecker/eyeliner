"""Explicit outcomes for speculative Discogs collection refreshes."""

from dataclasses import dataclass
from enum import Enum, auto
from types import MappingProxyType
from typing import Any, Mapping, Optional


class CollectionRefreshState(Enum):
    OWNED = auto()
    CLEAN_NO_MATCH = auto()
    COOLDOWN_SKIPPED = auto()


class PlayCountReadState(Enum):
    """The safety classification of a Play Count read."""

    READY = auto()
    DEFINITIVE_INSTANCE_MISSING = auto()
    ABORT = auto()


@dataclass(frozen=True)
class PlayCountReadResult:
    """A validated Play Count read, or a safe reason not to write."""

    state: PlayCountReadState
    field_id: Optional[int] = None
    current_count: Optional[int] = None
    observed_instance_ids: tuple[int, ...] = ()

    def __post_init__(self):
        ready = self.state is PlayCountReadState.READY
        missing = self.state is PlayCountReadState.DEFINITIVE_INSTANCE_MISSING
        if ready:
            if type(self.field_id) is not int or self.field_id <= 0:
                raise ValueError("READY requires a positive integer field ID")
            if type(self.current_count) is not int or self.current_count < 0:
                raise ValueError("READY requires a nonnegative integer count")
            if self.observed_instance_ids:
                raise ValueError("READY cannot carry replacement instances")
        elif self.field_id is not None or self.current_count is not None:
            raise ValueError("non-READY results cannot carry field/count data")
        if missing:
            if (len(set(self.observed_instance_ids)) != len(self.observed_instance_ids)
                    or any(type(value) is not int or value <= 0
                           for value in self.observed_instance_ids)):
                raise ValueError("MISSING instances must be unique positive integers")
        elif self.observed_instance_ids:
            raise ValueError("only MISSING can carry replacement instances")


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

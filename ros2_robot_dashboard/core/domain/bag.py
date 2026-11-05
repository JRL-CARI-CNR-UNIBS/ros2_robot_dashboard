"""Bag recording and playback types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .base import Serializable


@dataclass(frozen=True)
class BagRecordConfig(Serializable):
    output_dir: str
    name: str = ''
    add_timestamp: bool = True
    topics: tuple[str, ...] = ()
    all_topics: bool = False
    exclude_regex: str = ''
    include_regex: str = ''
    storage: str = ''                       # '' = distro default
    max_duration_s: int = 0                 # split files (0 = off)
    max_size_bytes: int = 0                 # split files (0 = off)
    compression: str = ''                   # '' | zstd
    compression_mode: str = 'file'          # file | message
    qos_overrides_path: str = ''
    include_hidden: bool = False
    include_unpublished: bool = False
    stop_after_s: float = 0.0               # stop recording automatically (0 = manual)


@dataclass(frozen=True)
class BagPlayConfig(Serializable):
    path: str
    rate: float = 1.0
    loop: bool = False
    start_offset_s: float = 0.0
    topics: tuple[str, ...] = ()
    clock: bool = False
    clock_hz: float = 100.0
    start_paused: bool = False
    storage: str = ''


@dataclass(frozen=True)
class BagTopic(Serializable):
    name: str
    type: str
    count: int
    serialization: str = 'cdr'


@dataclass(frozen=True)
class BagInfo(Serializable):
    path: str
    storage: str
    duration_s: float
    start_s: float
    message_count: int
    size_bytes: int
    topics: tuple[BagTopic, ...] = ()
    files: tuple[str, ...] = ()
    compression: str = ''


@dataclass(frozen=True)
class RecordingStatus(Serializable):
    process_id: str
    output: str
    elapsed_s: float
    size_bytes: int
    recording: bool
    message: str = ''
    topic_counts: dict = field(default_factory=dict)
    info: Optional[BagInfo] = None

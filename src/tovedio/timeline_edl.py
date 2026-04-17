"""时间轴 / EDL 中间表示（FR10）：供 ffmpeg 合成阶段与调试落盘。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass
class TimelineClip:
    """单镜在成片中的一段视觉轨。"""

    shot_id: str
    order: int
    media: Literal["image", "video"]
    source_path: str
    duration_sec: float
    i2v_task_id: str | None = None
    i2v_fallback: bool = False


@dataclass
class TimelineAudio:
    """MVP：单轨旁白/对白混音描述。"""

    path: str
    strategy: str = "single_track_mux"
    notes: str = ""


@dataclass
class ProjectTimeline:
    """统一 EDL：镜序列 + 音轨元数据。"""

    schema_version: str = "1.0.0"
    width: int = 1280
    height: int = 720
    fps_hint: float = 25.0
    clips: list[TimelineClip] = field(default_factory=list)
    audio: TimelineAudio | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def save_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

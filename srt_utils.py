"""SRT 字幕文件生成工具。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Segment:
    start: float  # 秒
    end: float
    text: str
    translation: str = ""


def format_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments: list[Segment], path: str, bilingual: bool = False) -> None:
    """写出 SRT 文件。bilingual=True 时每条字幕为 原文+译文 两行。"""
    blocks = []
    for i, seg in enumerate(segments, 1):
        if bilingual and seg.translation:
            text = f"{seg.text.strip()}\n{seg.translation.strip()}"
        else:
            text = (seg.translation or seg.text).strip()
        blocks.append(
            f"{i}\n{format_timestamp(seg.start)} --> {format_timestamp(seg.end)}\n{text}"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(blocks) + "\n")

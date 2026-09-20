"""视频 -> 音频提取 -> 语音转录 -> LLM 翻译 -> SRT 字幕 流水线。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

from srt_utils import Segment, write_srt
from llm_translator import translate_segments


class PipelineError(Exception):
    pass


def app_dir() -> str:
    """exe 或脚本所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_ffmpeg() -> str:
    """优先使用与程序同目录的 ffmpeg，否则用 PATH 中的。

    macOS 从 Finder 启动的 .app 继承不到 shell 的 PATH（没有
    /opt/homebrew/bin），因此额外检查 Homebrew 的常见安装位置。
    """
    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    candidates = [os.path.join(app_dir(), exe_name)]
    if sys.platform == "darwin":
        candidates += [
            "/opt/homebrew/bin/ffmpeg",  # Apple Silicon Homebrew
            "/usr/local/bin/ffmpeg",     # Intel Homebrew / 手动安装
        ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise PipelineError(
        f"未找到 ffmpeg。请将 {exe_name} 放到程序目录，或加入系统 PATH。"
        + ("" if os.name == "nt" else "macOS 可用 brew install ffmpeg 安装。")
    )


def extract_audio(video_path: str, wav_path: str) -> None:
    """用 ffmpeg 从视频中提取 16kHz 单声道 WAV 音频。"""
    cmd = [
        find_ffmpeg(), "-y", "-i", video_path,
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", wav_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not os.path.exists(wav_path):
        raise PipelineError(f"音频提取失败:\n{proc.stderr[-800:]}")


def transcribe(audio_path: str, model_size: str, language: str | None,
               progress_cb=None, cancel_event=None) -> tuple[list[Segment], str, float]:
    """faster-whisper 转录。返回 (segments, 检测到的语言代码, 音频时长秒)。"""
    from faster_whisper import WhisperModel

    # 默认使用 CPU(int8)，无需 NVIDIA 运行库即可运行
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    seg_iter, info = model.transcribe(
        audio_path,
        language=language,  # None 表示自动检测
        vad_filter=True,
        beam_size=5,
    )
    duration = float(info.duration) if info.duration else 0.0
    segments: list[Segment] = []
    for s in seg_iter:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("已取消")
        segments.append(Segment(start=s.start, end=s.end, text=s.text.strip()))
        if progress_cb and duration > 0:
            progress_cb(min(s.end / duration, 1.0))
    if progress_cb:
        progress_cb(1.0)
    return segments, info.language or "unknown", duration


def run_pipeline(video_path: str, source_lang: str, target_lang: str,
                 whisper_model: str, llm_config: dict,
                 bilingual: bool, transcribe_only: bool,
                 output_path: str | None = None,
                 progress_cb=None, log_cb=None, cancel_event=None) -> str:
    """完整流程，返回生成的 SRT 路径。

    progress_cb(stage, fraction): stage ∈ {"extract", "transcribe", "translate"}
    """
    def log(msg: str):
        if log_cb:
            log_cb(msg)

    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("已取消")

    video_path = os.path.abspath(video_path)
    if not os.path.isfile(video_path):
        raise PipelineError(f"视频文件不存在: {video_path}")

    if output_path is None:
        stem, _ = os.path.splitext(video_path)
        suffix = "transcript" if transcribe_only else target_lang
        output_path = f"{stem}.{suffix}.srt"

    tmpdir = tempfile.mkdtemp(prefix="vidtrans_")
    wav_path = os.path.join(tmpdir, "audio.wav")
    try:
        log("正在提取音频 ...")
        if progress_cb:
            progress_cb("extract", 0.0)
        extract_audio(video_path, wav_path)
        check_cancel()

        lang_code = None if source_lang == "auto" else source_lang
        log(f"正在加载 Whisper 模型 ({whisper_model}) 并转录 ...")
        segments, detected_lang, duration = transcribe(
            wav_path, whisper_model, lang_code,
            progress_cb=lambda f: progress_cb and progress_cb("transcribe", f),
            cancel_event=cancel_event,
        )
        if not segments:
            raise PipelineError("未识别到任何语音内容。")
        log(f"转录完成：{len(segments)} 条字幕，检测语言: {detected_lang}，"
            f"时长 {duration:.1f}s")
        check_cancel()

        if not transcribe_only:
            src = detected_lang if source_lang == "auto" else source_lang
            log(f"正在调用 LLM 翻译 ({src} -> {target_lang}) ...")
            translations = translate_segments(
                [s.text for s in segments], src, target_lang,
                base_url=llm_config["base_url"],
                api_key=llm_config["api_key"],
                model=llm_config["model"],
                progress_cb=lambda d, t: progress_cb and progress_cb(
                    "translate", d / t if t else 1.0),
                cancel_event=cancel_event,
            )
            for seg, tr in zip(segments, translations):
                seg.translation = tr
            log("翻译完成。")

        write_srt(segments, output_path, bilingual=bilingual and not transcribe_only)
        log(f"字幕已保存: {output_path}")
        return output_path
    finally:
        try:
            os.remove(wav_path)
            os.rmdir(tmpdir)
        except OSError:
            pass

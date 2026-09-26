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


def is_url(path: str) -> bool:
    """是否为 http(s) 链接（网盘直链等）。"""
    return path.lower().startswith(("http://", "https://"))


def default_remote_output_dir() -> str:
    """远程链接视频的字幕默认输出目录（本地文件保存在视频同目录）。"""
    d = os.path.join(os.path.expanduser("~"), "Movies", "VideoSubtitleTranslator")
    os.makedirs(d, exist_ok=True)
    return d


def _url_basename(url: str) -> str:
    """从 URL 中提取可读的文件名（去掉扩展名），用于命名字幕文件。"""
    from urllib.parse import urlparse, unquote
    name = os.path.basename(unquote(urlparse(url).path))
    stem, _ = os.path.splitext(name)
    return stem or "remote_video"


_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0.0.0 Safari/537.36")


def extract_audio(video_path: str, wav_path: str, headers: str = "") -> None:
    """用 ffmpeg 从视频中提取 16kHz 单声道 WAV 音频。

    video_path 可以是本地路径或 http(s) 直链；headers 为可选的自定义
    请求头（如 "Cookie: ..."），用于需要登录态的网盘直链。
    """
    cmd = [find_ffmpeg(), "-y"]
    if is_url(video_path):
        cmd += ["-user_agent", _UA]
        if headers.strip():
            # ffmpeg 要求请求头以 \r\n 结尾
            cmd += ["-headers", headers.strip() + "\r\n"]
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5"]
    cmd += [
        "-i", video_path,
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", wav_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not os.path.exists(wav_path):
        raise PipelineError(f"音频提取失败:\n{proc.stderr[-800:]}")


def throttled_download(url: str, dest: str, rate_bps: float,
                       headers: str = "",
                       progress_cb=None, cancel_event=None) -> int:
    """限速下载 URL 到本地文件，返回下载字节数。

    单连接顺序读取 + 限速，使下载行为接近在线播放，降低网盘风控特征。
    rate_bps: 字节/秒；<= 0 表示不限速。progress_cb(downloaded, total)，
    total 未知时为 0。
    """
    import time
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    for line in headers.strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            req.add_header(k.strip(), v.strip())
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        raise PipelineError(f"下载视频失败 HTTP {e.code}（链接可能已过期或需要更新 Cookie）") from e
    except urllib.error.URLError as e:
        raise PipelineError(f"无法连接视频链接: {e.reason}") from e

    total = int(resp.headers.get("Content-Length") or 0)
    downloaded = 0
    start = time.monotonic()
    chunk_size = 256 * 1024
    try:
        with resp, open(dest, "wb") as f:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("已取消")
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if rate_bps > 0:
                    expected = downloaded / rate_bps
                    delay = expected - (time.monotonic() - start)
                    if delay > 0:
                        time.sleep(min(delay, 1.0))
                if progress_cb:
                    progress_cb(downloaded, total)
    except InterruptedError:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    return downloaded


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
                 headers: str = "",
                 speed_limit_mbs: float = 0.0,
                 progress_cb=None, log_cb=None, cancel_event=None) -> str:
    """完整流程，返回生成的 SRT 路径。

    video_path 可以是本地路径或 http(s) 直链（网盘直链等）；
    headers 为访问直链时的可选自定义请求头（如 Cookie）；
    speed_limit_mbs 为直链下载限速（MB/s），<= 0 表示不限速直读。
    progress_cb(stage, fraction): stage ∈ {"extract", "transcribe", "translate"}
    """
    def log(msg: str):
        if log_cb:
            log_cb(msg)

    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("已取消")

    remote = is_url(video_path)
    if remote:
        pass  # 远程链接无法预先校验，交给下载/提取环节处理
    else:
        video_path = os.path.abspath(video_path)
        if not os.path.isfile(video_path):
            raise PipelineError(f"视频文件不存在: {video_path}")

    if output_path is None:
        suffix = "transcript" if transcribe_only else target_lang
        if remote:
            output_path = os.path.join(
                default_remote_output_dir(), f"{_url_basename(video_path)}.{suffix}.srt")
        else:
            stem, _ = os.path.splitext(video_path)
            output_path = f"{stem}.{suffix}.srt"

    tmpdir = tempfile.mkdtemp(prefix="vidtrans_")
    wav_path = os.path.join(tmpdir, "audio.wav")
    local_video: str | None = None
    try:
        if progress_cb:
            progress_cb("extract", 0.0)
        if remote and speed_limit_mbs > 0:
            # 限速模式：先单连接节流下载到临时文件，再从本地提取音频。
            # 下载行为接近在线播放，降低网盘风控特征。
            from urllib.parse import urlparse, unquote
            ext = os.path.splitext(unquote(urlparse(video_path).path))[1]
            local_video = os.path.join(tmpdir, f"video{ext or '.mp4'}")
            log(f"正在限速下载视频 ({speed_limit_mbs:g} MB/s) ...")
            size = throttled_download(
                video_path, local_video, speed_limit_mbs * 1024 * 1024,
                headers=headers,
                progress_cb=lambda d, t: progress_cb and progress_cb(
                    "extract", (d / t * 0.9) if t else 0.0),
                cancel_event=cancel_event,
            )
            log(f"下载完成 ({size / 1024 / 1024:.1f} MB)，正在提取音频 ...")
            extract_audio(local_video, wav_path)
        else:
            log("正在从链接下载并提取音频 ..." if remote else "正在提取音频 ...")
            extract_audio(video_path, wav_path, headers=headers)
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
        except OSError:
            pass
        if local_video:
            try:
                os.remove(local_video)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

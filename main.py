"""视频语音翻译字幕工具 —— tkinter GUI 入口。"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from pipeline import run_pipeline, app_dir
from llm_translator import test_connection, LLMError

def user_data_dir() -> str:
    """可写的用户数据目录。macOS 打包成 .app 后 exe 在 bundle 内（只读且
    可能被 Gatekeeper 隔离），配置和日志需放到 Application Support。"""
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        d = os.path.expanduser("~/Library/Application Support/VideoSubtitleTranslator")
        os.makedirs(d, exist_ok=True)
        return d
    return app_dir()


CONFIG_PATH = os.path.join(user_data_dir(), "config.json")

# (显示名, whisper 语言代码)
SOURCE_LANGUAGES = [
    ("自动检测", "auto"),
    ("中文", "zh"),
    ("英语", "en"),
    ("日语", "ja"),
    ("韩语", "ko"),
    ("法语", "fr"),
    ("德语", "de"),
    ("西班牙语", "es"),
    ("俄语", "ru"),
    ("意大利语", "it"),
    ("葡萄牙语", "pt"),
    ("阿拉伯语", "ar"),
    ("印地语", "hi"),
    ("泰语", "th"),
    ("越南语", "vi"),
]

TARGET_LANGUAGES = [
    "中文（简体）", "中文（繁体）", "英语", "日语", "韩语",
    "法语", "德语", "西班牙语", "俄语",
]

WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3"]

VIDEO_FILETYPES = [
    ("视频文件", "*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.ts *.m4v"),
    ("所有文件", "*.*"),
]

DEFAULT_CONFIG = {
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o-mini",
    "whisper_model": "small",
    "bilingual": True,
    "source_lang": "auto",
    "target_lang": "中文（简体）",
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("视频语音翻译字幕工具")
        self.geometry("760x640")
        self.minsize(700, 600)

        self.config_data = self._load_config()
        self.msg_queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()

        self._build_ui()
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 配置 ----------
    def _load_config(self) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass
        return cfg

    def _save_config(self):
        self.config_data.update({
            "base_url": self.base_url_var.get().strip(),
            "api_key": self.api_key_var.get().strip(),
            "model": self.model_var.get().strip(),
            "whisper_model": self.whisper_var.get(),
            "bilingual": self.bilingual_var.get(),
            "source_lang": self._source_lang_code(),
            "target_lang": self.target_var.get().strip(),
        })
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config_data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---------- 界面 ----------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        main = ttk.Frame(self)
        main.pack(fill="both", expand=True, padx=6, pady=6)

        # 视频文件
        file_frame = ttk.LabelFrame(main, text="视频文件")
        file_frame.pack(fill="x", **pad)
        self.video_var = tk.StringVar()
        ttk.Entry(file_frame, textvariable=self.video_var).pack(
            side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(file_frame, text="浏览...", command=self._browse).pack(
            side="left", padx=6, pady=6)

        # 语言选项
        lang_frame = ttk.LabelFrame(main, text="语言设置")
        lang_frame.pack(fill="x", **pad)
        ttk.Label(lang_frame, text="原视频语言:").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.source_var = tk.StringVar()
        src_names = [n for n, _ in SOURCE_LANGUAGES]
        self.source_combo = ttk.Combobox(lang_frame, textvariable=self.source_var,
                                         values=src_names, state="readonly", width=14)
        saved_code = self.config_data.get("source_lang", "auto")
        self.source_combo.set(next((n for n, c in SOURCE_LANGUAGES if c == saved_code), "自动检测"))
        self.source_combo.grid(row=0, column=1, sticky="w", padx=6, pady=6)

        ttk.Label(lang_frame, text="翻译为:").grid(row=0, column=2, sticky="w", padx=6, pady=6)
        self.target_var = tk.StringVar(value=self.config_data.get("target_lang", "中文（简体）"))
        ttk.Combobox(lang_frame, textvariable=self.target_var,
                     values=TARGET_LANGUAGES, width=14).grid(
            row=0, column=3, sticky="w", padx=6, pady=6)

        self.bilingual_var = tk.BooleanVar(value=self.config_data.get("bilingual", True))
        ttk.Checkbutton(lang_frame, text="双语字幕（原文+译文）",
                        variable=self.bilingual_var).grid(
            row=0, column=4, sticky="w", padx=12, pady=6)

        # 模型设置
        model_frame = ttk.LabelFrame(main, text="识别与翻译设置")
        model_frame.pack(fill="x", **pad)
        ttk.Label(model_frame, text="Whisper 模型:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.whisper_var = tk.StringVar(value=self.config_data.get("whisper_model", "small"))
        ttk.Combobox(model_frame, textvariable=self.whisper_var,
                     values=WHISPER_MODELS, state="readonly", width=10).grid(
            row=0, column=1, sticky="w", padx=6, pady=4)
        ttk.Label(model_frame, text="（越大越准，首次使用会自动下载）",
                  foreground="gray").grid(row=0, column=2, columnspan=3, sticky="w", padx=6)

        ttk.Label(model_frame, text="LLM API 地址:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.base_url_var = tk.StringVar(value=self.config_data.get("base_url", ""))
        ttk.Entry(model_frame, textvariable=self.base_url_var, width=42).grid(
            row=1, column=1, columnspan=3, sticky="w", padx=6, pady=4)

        ttk.Label(model_frame, text="API Key:").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.api_key_var = tk.StringVar(value=self.config_data.get("api_key", ""))
        ttk.Entry(model_frame, textvariable=self.api_key_var, width=42, show="*").grid(
            row=2, column=1, columnspan=3, sticky="w", padx=6, pady=4)

        ttk.Label(model_frame, text="模型名称:").grid(row=3, column=0, sticky="w", padx=6, pady=4)
        self.model_var = tk.StringVar(value=self.config_data.get("model", ""))
        ttk.Entry(model_frame, textvariable=self.model_var, width=25).grid(
            row=3, column=1, sticky="w", padx=6, pady=4)
        ttk.Button(model_frame, text="测试连接", command=self._test_llm).grid(
            row=3, column=2, sticky="w", padx=6, pady=4)

        self.transcribe_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(model_frame, text="仅转录（不翻译）",
                        variable=self.transcribe_only_var).grid(
            row=3, column=3, sticky="w", padx=12, pady=4)

        # 操作区
        op_frame = ttk.Frame(main)
        op_frame.pack(fill="x", **pad)
        self.start_btn = ttk.Button(op_frame, text="开始生成字幕", command=self._start)
        self.start_btn.pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(op_frame, text="取消", command=self._cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.stage_var = tk.StringVar(value="就绪")
        ttk.Label(op_frame, textvariable=self.stage_var).pack(side="left", padx=16)

        self.progress = ttk.Progressbar(main, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=14, pady=4)

        # 日志
        log_frame = ttk.LabelFrame(main, text="日志")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        scroll.pack(side="right", fill="y", pady=6, padx=(0, 6))

    # ---------- 事件 ----------
    def _browse(self):
        path = filedialog.askopenfilename(filetypes=VIDEO_FILETYPES)
        if path:
            self.video_var.set(path)

    def _source_lang_code(self) -> str:
        name = self.source_combo.get()
        return next((c for n, c in SOURCE_LANGUAGES if n == name), "auto")

    def _log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _test_llm(self):
        base_url = self.base_url_var.get().strip()
        api_key = self.api_key_var.get().strip()
        model = self.model_var.get().strip()
        if not base_url or not model:
            messagebox.showwarning("提示", "请填写 API 地址和模型名称。")
            return
        self._log("正在测试 LLM 连接 ...")

        def worker():
            try:
                reply = test_connection(base_url, api_key, model)
                self.msg_queue.put(("log", f"连接成功，模型回复: {reply.strip()[:100]}"))
            except LLMError as e:
                self.msg_queue.put(("log", f"连接失败: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _start(self):
        video = self.video_var.get().strip()
        if not video or not os.path.isfile(video):
            messagebox.showwarning("提示", "请选择有效的视频文件。")
            return
        transcribe_only = self.transcribe_only_var.get()
        llm_config = {
            "base_url": self.base_url_var.get().strip(),
            "api_key": self.api_key_var.get().strip(),
            "model": self.model_var.get().strip(),
        }
        if not transcribe_only and (not llm_config["base_url"] or not llm_config["model"]):
            messagebox.showwarning("提示", "翻译需要填写 LLM API 地址和模型名称。")
            return

        self._save_config()
        self.cancel_event.clear()
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress["value"] = 0
        self.stage_var.set("运行中 ...")

        args = dict(
            video_path=video,
            source_lang=self._source_lang_code(),
            target_lang=self.target_var.get().strip() or "中文",
            whisper_model=self.whisper_var.get(),
            llm_config=llm_config,
            bilingual=self.bilingual_var.get(),
            transcribe_only=transcribe_only,
            progress_cb=lambda stage, frac: self.msg_queue.put(("progress", (stage, frac))),
            log_cb=lambda msg: self.msg_queue.put(("log", msg)),
            cancel_event=self.cancel_event,
        )
        self.worker = threading.Thread(target=self._run, kwargs=args, daemon=True)
        self.worker.start()

    def _run(self, **kwargs):
        try:
            out = run_pipeline(**kwargs)
            self.msg_queue.put(("done", out))
        except InterruptedError:
            self.msg_queue.put(("cancelled", None))
        except Exception as e:
            self.msg_queue.put(("error", str(e)))

    def _cancel(self):
        self.cancel_event.set()
        self.stage_var.set("正在取消 ...")

    def _poll_queue(self):
        stage_names = {"extract": "提取音频", "transcribe": "语音转录", "translate": "LLM 翻译"}
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    stage, frac = payload
                    self.progress["value"] = frac * 100
                    self.stage_var.set(f"{stage_names.get(stage, stage)} {frac * 100:.0f}%")
                elif kind == "done":
                    self._finish()
                    self._log(f"全部完成！字幕文件: {payload}")
                    messagebox.showinfo("完成", f"字幕已生成:\n{payload}")
                elif kind == "cancelled":
                    self._finish()
                    self._log("任务已取消。")
                elif kind == "error":
                    self._finish()
                    self._log(f"出错: {payload}")
                    messagebox.showerror("错误", payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _finish(self):
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.stage_var.set("就绪")

    def _on_close(self):
        self.cancel_event.set()
        self._save_config()
        self.destroy()


def _preload_vc_runtime():
    """抢在 TortoiseSVN 等 shell 扩展之前，按完整路径加载新版 VC++ 运行库。

    文件对话框会把 shell 扩展（如 TortoiseSVN）加载进本进程，带入其自带的
    旧版 MSVCP140.dll；Windows 按模块名去重后，ctranslate2 会拿到旧版运行库，
    导致 0xc0000005 闪退。启动时先按全路径加载新版即可避免。
    """
    import ctypes
    if os.name != "nt":
        return
    base = app_dir()
    candidates = [base, os.path.join(base, "_internal"),
                  os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")]
    for dll in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll"):
        for d in candidates:
            p = os.path.join(d, dll)
            if os.path.isfile(p):
                try:
                    ctypes.WinDLL(p)
                except OSError:
                    pass
                break


def _enable_crash_log():
    """把原生崩溃（段错误等）堆栈写入用户数据目录的 crash.log，便于排查闪退。"""
    import faulthandler
    try:
        f = open(os.path.join(user_data_dir(), "crash.log"), "a", encoding="utf-8")
        faulthandler.enable(f)
    except OSError:
        faulthandler.enable()


def _run_cli(argv):
    """无界面调试模式: main.py --cli <视频路径> [--model small] [--src auto] [--dst 语言] [--no-translate]"""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("--model", default=None)
    parser.add_argument("--src", default=None)
    parser.add_argument("--dst", default=None)
    parser.add_argument("--no-translate", action="store_true")
    args = parser.parse_args(argv)

    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, json.JSONDecodeError):
        pass

    log_path = os.path.join(user_data_dir(), "cli_run.log")
    log_file = open(log_path, "w", encoding="utf-8")

    def log(msg):
        line = str(msg)
        log_file.write(line + "\n")
        log_file.flush()
        try:
            print(line, flush=True)
        except Exception:
            pass

    try:
        out = run_pipeline(
            video_path=args.video,
            source_lang=args.src or cfg.get("source_lang", "auto"),
            target_lang=args.dst or cfg.get("target_lang", "中文（简体）"),
            whisper_model=args.model or cfg.get("whisper_model", "small"),
            llm_config={
                "base_url": cfg.get("base_url", ""),
                "api_key": cfg.get("api_key", ""),
                "model": cfg.get("model", ""),
            },
            bilingual=cfg.get("bilingual", True),
            transcribe_only=args.no_translate,
            log_cb=log,
            progress_cb=lambda s, f: log(f"[{s}] {f * 100:.0f}%"),
        )
        log(f"DONE: {out}")
        return 0
    except Exception as e:
        import traceback
        log(f"ERROR: {e}")
        log(traceback.format_exc())
        return 1
    finally:
        log_file.close()


if __name__ == "__main__":
    _preload_vc_runtime()
    _enable_crash_log()
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        sys.exit(_run_cli(sys.argv[2:]))
    App().mainloop()

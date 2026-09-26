# 视频语音翻译字幕工具

读取视频文件，使用 Whisper 识别语音，调用 LLM（任意 OpenAI 兼容接口）进行翻译，生成 SRT 字幕文件。提供图形界面，可选择视频文件、指定原视频语言和目标翻译语言。

## 功能

- 选择常见格式的视频文件（mp4 / mkv / avi / mov 等）
- 指定原视频语言（或自动检测）与目标翻译语言
- 本地 faster-whisper 语音转录（无需联网识别，模型首次使用自动下载）
- LLM 批量翻译，支持任何 OpenAI 兼容 API（OpenAI、DeepSeek、通义、本地 Ollama 等）
- 生成单语或双语（原文+译文）SRT 字幕，保存在视频同目录下
- 支持仅转录不翻译、任务取消、实时进度与日志

## 环境要求

- Python 3.10+
- ffmpeg（Windows：需在 PATH 中，或将 `ffmpeg.exe` 放到程序同目录；macOS：`brew install ffmpeg`）

## 安装与运行

Windows:

```bash
pip install -r requirements.txt
python main.py
```

macOS:

```bash
brew install ffmpeg python-tk
pip3 install -r requirements.txt
python3 main.py
```

## 打包为 exe

```bash
pip install pyinstaller
python -m PyInstaller --noconfirm --clean --windowed --onedir --name VideoSubtitleTranslator \
  --collect-all faster_whisper --collect-all ctranslate2 --collect-all tokenizers \
  --collect-all av --collect-all onnxruntime \
  main.py
```

打包产物在 `dist\VideoSubtitleTranslator\`，把 `ffmpeg.exe` 复制进该目录后即可整体分发给没有 Python 环境的用户使用，入口为 `VideoSubtitleTranslator.exe`。

## 打包为 macOS App

PyInstaller 不能跨平台编译，需要在 Mac 上执行（命令与 Windows 相同）：

```bash
pip3 install pyinstaller
python3 -m PyInstaller --noconfirm --clean --windowed --onedir --name VideoSubtitleTranslator \
  --collect-all faster_whisper --collect-all ctranslate2 --collect-all tokenizers \
  --collect-all av --collect-all onnxruntime \
  main.py
```

产物为 `dist/VideoSubtitleTranslator.app`。macOS 上 ffmpeg 建议用 `brew install ffmpeg` 单独安装（也可以把 `ffmpeg` 二进制放进 `.app/Contents/MacOS/` 目录随包分发）。说明：

- 未签名的 App 首次打开需在 Finder 中右键 →「打开」
- 配置与日志保存在 `~/Library/Application Support/VideoSubtitleTranslator/`
- 转录在 macOS 上同样使用 CPU 运行（ctranslate2 不支持 Metal），Apple Silicon 可正常使用

## 使用说明

1. 点击「浏览...」选择视频文件，或直接把 **http(s) 视频直链**粘贴到输入框（网盘直链见下文「网盘视频」）。
2. 在「语言设置」中选择原视频语言（不确定可留「自动检测」）和目标语言；勾选「双语字幕」可同时保留原文。
3. 在「识别与翻译设置」中：
   - 选择 Whisper 模型（`small` 是速度与准确率的平衡；`tiny/base` 快但粗糙，`medium/large-v3` 更准更慢）。
   - 填写 LLM 的 API 地址、API Key 和模型名称，可点「测试连接」验证。
   - 配置可点「保存为预设」存为命名预设，之后在「LLM 预设」下拉框中一键切换。
     - OpenAI: `https://api.openai.com/v1`
     - DeepSeek: `https://api.deepseek.com/v1`
     - Ollama 本地: `http://localhost:11434/v1`
4. 点击「开始生成字幕」。完成后 SRT 文件保存在视频同目录，文件名形如 `视频名.中文（简体）.srt`；使用直链时保存在 `~/Movies/VideoSubtitleTranslator/` 下。

## 网盘视频（115 等）

115 网盘没有官方本地挂载和开放 API，可选两种方式：

- **本地挂载**：用 CloudDrive2 等工具把网盘挂载为本地文件夹后，直接「浏览...」选中挂载目录里的视频即可，与本地文件用法完全相同。（第三方挂载属于网盘条款灰色地带，请自行权衡账号风控风险。）
- **视频直链**：把视频的 http(s) 直链（如 AList 生成的链接）粘贴到视频文件输入框；如果链接需要登录态（如 115 官方下载链接），在「直链请求头」一栏填入 `Cookie: 你的cookie`。程序边下边转录，字幕输出到 `~/Movies/VideoSubtitleTranslator/`。
  - 「限速(MB/s)」填大于 0 的值时，改为先单连接节流下载到临时文件再提取音频（下载行为接近在线播放，降低网盘风控特征，视频处理完后自动删除）；填 0 则不限速、直接边下边转。

设置会自动保存到 `config.json`，下次启动自动恢复。

## 项目结构

| 文件 | 说明 |
|---|---|
| `main.py` | tkinter 图形界面与任务调度 |
| `pipeline.py` | 音频提取 → 转录 → 翻译 → 写字幕 流水线 |
| `llm_translator.py` | OpenAI 兼容接口的批量翻译（仅标准库） |
| `srt_utils.py` | SRT 字幕生成 |

## 说明

- 语音转录默认使用 CPU（int8）运行，无需 NVIDIA 运行库。
- 翻译按每批 20 条字幕调用 LLM，要求模型返回 JSON；解析失败会自动重试一次。

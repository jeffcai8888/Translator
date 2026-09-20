"""调用 OpenAI 兼容接口的 LLM 进行批量字幕翻译（仅依赖标准库）。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


def _chat_completion(base_url: str, api_key: str, model: str,
                     messages: list[dict], timeout: int = 120) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise LLMError(f"LLM 请求失败 HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"无法连接 LLM 服务: {e.reason}") from e
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"LLM 返回格式异常: {json.dumps(data)[:500]}") from e


def _extract_json_array(text: str) -> list:
    """从模型输出中提取 JSON 数组，容忍 ```json 代码块包裹。"""
    text = text.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("输出中未找到 JSON 数组")
    return json.loads(text[start:end + 1])


def translate_batch(texts: list[str], source_lang: str, target_lang: str,
                    base_url: str, api_key: str, model: str) -> list[str]:
    """翻译一批字幕文本，返回与输入等长的译文列表。失败时抛出 LLMError。"""
    if not texts:
        return []
    numbered = [{"id": i, "text": t} for i, t in enumerate(texts)]
    src_desc = source_lang if source_lang != "auto" else "检测到的原始语言"
    system = (
        "你是专业的视频字幕翻译器。用户会给你一个字幕文本的 JSON 数组，"
        "请把每个 text 字段翻译成目标语言，保持口语化、简洁，适合字幕显示。"
        "只输出一个 JSON 数组，每个元素为 {\"id\": 数字, \"text\": \"译文\"}，"
        "id 必须与输入一一对应，不要遗漏、不要合并、不要输出任何其他内容。"
    )
    user = (
        f"源语言: {src_desc}\n目标语言: {target_lang}\n"
        f"请翻译以下字幕：\n{json.dumps(numbered, ensure_ascii=False)}"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_err: Exception | None = None
    for _ in range(2):  # 解析失败重试一次
        content = _chat_completion(base_url, api_key, model, messages)
        try:
            arr = _extract_json_array(content)
            result = {}
            for item in arr:
                result[int(item["id"])] = str(item["text"])
            translations = [result.get(i, "").strip() for i in range(len(texts))]
            if all(translations):
                return translations
            last_err = ValueError("部分字幕缺少译文")
        except (ValueError, KeyError, TypeError) as e:
            last_err = e
    raise LLMError(f"LLM 译文解析失败: {last_err}")


def translate_segments(texts: list[str], source_lang: str, target_lang: str,
                       base_url: str, api_key: str, model: str,
                       batch_size: int = 20,
                       progress_cb=None,
                       cancel_event=None) -> list[str]:
    """分批翻译全部字幕。progress_cb(done, total)；cancel_event.set() 可中止。"""
    total = len(texts)
    out: list[str] = [""] * total
    for start in range(0, total, batch_size):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("已取消")
        batch = texts[start:start + batch_size]
        translated = translate_batch(batch, source_lang, target_lang,
                                     base_url, api_key, model)
        out[start:start + len(batch)] = translated
        if progress_cb:
            progress_cb(min(start + len(batch), total), total)
    return out


def test_connection(base_url: str, api_key: str, model: str) -> str:
    """测试 LLM 连通性，返回模型回复。"""
    return _chat_completion(base_url, api_key, model, [
        {"role": "user", "content": "回复\"ok\"即可。"}
    ], timeout=30)

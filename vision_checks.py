"""Optional visual verification using an OpenAI-compatible multimodal endpoint.

Unset configuration means ``unknown`` and never blocks the existing DOM checks.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import urllib.request
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class VisionResult:
    verdict: str
    reason: str
    available: bool
    x: Optional[int] = None
    y: Optional[int] = None
    confidence: float = 0.0


def _request(image_path: str, question: str, mode: str, opener=None) -> VisionResult:
    endpoint = os.environ.get("DP_VISION_ENDPOINT", "").strip()
    api_key = os.environ.get("DP_VISION_API_KEY", "").strip()
    model = os.environ.get("DP_VISION_MODEL", "").strip()
    if not endpoint or not api_key or not model:
        return VisionResult("unknown", "视觉服务未配置，沿用 DOM 判断", False)
    mime = mimetypes.guess_type(image_path)[0] or "image/png"
    with open(image_path, "rb") as image_file:
        data_url = "data:%s;base64,%s" % (
            mime, base64.b64encode(image_file.read()).decode("ascii"))
    schema = ("只返回JSON：{\"verdict\":\"yes|no\",\"reason\":\"简短原因\"}"
              if mode == "check" else
              "只返回JSON：{\"verdict\":\"yes|no\",\"x\":整数,\"y\":整数,"
              "\"confidence\":0到1,\"reason\":\"简短原因\"}。坐标基于整张截图左上角。")
    payload = {"model": model, "temperature": 0, "messages": [{"role": "user", "content": [
        {"type": "text", "text": question + "\n" + schema},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]}]}
    request = urllib.request.Request(endpoint, json.dumps(payload).encode("utf-8"), {
        "Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
    try:
        response = (opener or urllib.request.urlopen)(request, timeout=20)
        outer = json.loads(response.read().decode("utf-8"))
        content = outer["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
        content = str(content).strip().removeprefix("```json").removesuffix("```").strip()
        answer = json.loads(content)
        verdict = str(answer.get("verdict", "unknown")).lower()
        if verdict not in ("yes", "no"):
            raise ValueError("invalid verdict")
        return VisionResult(verdict, str(answer.get("reason", "")), True,
                            answer.get("x"), answer.get("y"),
                            float(answer.get("confidence", 0) or 0))
    except Exception as exc:
        return VisionResult("unknown", "视觉核验失败：%s" % exc, False)


def vision_check(screenshot: str, question: str, opener=None) -> VisionResult:
    return _request(screenshot, question, "check", opener)


def vision_locate(screenshot: str, question: str, opener=None) -> VisionResult:
    return _request(screenshot, question, "locate", opener)

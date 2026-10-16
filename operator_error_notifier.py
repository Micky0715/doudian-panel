#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Send one deduplicated Feishu alert for a completed panel task.

This module is deliberately separate from the DouDian workers.  Workers only
produce structured errors; the panel server owns task completion and invokes
this adapter after a task has reached a terminal state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


WORK_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = WORK_DIR / "config" / "operator_error_notifications.json"
DEFAULT_CLI_FALLBACK = "/Users/atrenew/.local/bin/lark-cli"
TERMINAL_STATES = {"done", "stopped", "error"}


class NotificationConfigError(ValueError):
    pass


def load_notification_config(path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config_path = Path(path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NotificationConfigError("无法读取运营报错通知配置：%s" % exc) from exc
    if not isinstance(data, dict):
        raise NotificationConfigError("运营报错通知配置必须是 JSON 对象")
    chat_id = str(data.get("chat_id") or "")
    if not chat_id.startswith("oc_"):
        raise NotificationConfigError("运营报错通知缺少有效 chat_id")
    if str(data.get("send_as") or "") not in {"user", "bot"}:
        raise NotificationConfigError("send_as 只能是 user 或 bot")
    shops = data.get("shops")
    if not isinstance(shops, dict):
        raise NotificationConfigError("shops 必须是店铺短 ID 到负责人的映射")
    for shop_id, row in shops.items():
        if not str(shop_id).isdigit() or not isinstance(row, dict):
            raise NotificationConfigError("店铺通知映射格式无效：%s" % shop_id)
        if not str(row.get("operator_open_id") or "").startswith("ou_"):
            raise NotificationConfigError("店铺 %s 缺少负责人 open_id" % shop_id)
    return data


def _safe_text(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    # Error text is untrusted input.  Do not let it create extra mentions.
    text = text.replace("<at", "＜at").replace("</at>", "＜/at＞")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _operator_reason(value: Any) -> str:
    """Turn worker diagnostics into a short, actionable Chinese group message.

    The original exception is kept in task logs; the chat alert is for the
    operator and must not expose Python paths or English exception classes.
    """
    raw = _safe_text(value, 500)
    lowered = raw.casefold()
    if "at least two white-background images are required" in lowered:
        return "白底图不足 2 张，请补图后重试"
    if "missing main image requires three distinct uploadable white images" in lowered:
        return "缺少主图，且可用白底图不足 3 张，请补图后重试"
    if "invalid_ip" in lowered or "请求来源ip不可信" in raw:
        return "接口 IP 未加入白名单，请联系管理员"
    if "goodsid" in lowered and ("未找到" in raw or "missing" in lowered):
        return "货品映射未找到，请核对商品 ID 和成色"
    # Keep a useful Chinese business error, but remove technical prefixes.
    if re.search(r"[\u4e00-\u9fff]", raw):
        cleaned = re.sub(r"^.*?(?:Error|Exception):\s*", "", raw, count=1)
        return _safe_text(cleaned, 100)
    return "处理失败，请在面板查看详细日志"


def should_notify(task: Mapping[str, Any], errors: Sequence[Mapping[str, Any]]) -> bool:
    state = str(task.get("state") or "")
    return state in TERMINAL_STATES and (bool(errors) or state == "error")


def build_task_error_message(
    task: Mapping[str, Any],
    errors: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    test_message: bool = False,
) -> str:
    shop_id = str(task.get("shop_id") or "")
    shop_map = config.get("shops") if isinstance(config.get("shops"), Mapping) else {}
    owner = shop_map.get(shop_id) if isinstance(shop_map, Mapping) else None
    owner = owner if isinstance(owner, Mapping) else {}
    shop_name = _safe_text(owner.get("shop_name") or task.get("shop_name") or shop_id)
    operator_name = _safe_text(owner.get("operator_name") or "未配置负责人")
    operator_open_id = str(owner.get("operator_open_id") or "")
    mention = (
        '<at user_id="%s">%s</at>' % (operator_open_id, operator_name)
        if operator_open_id.startswith("ou_") else operator_name
    )
    title = "【通知测试｜无需处理】" if test_message else "【上架异常】"
    state_labels = {"done": "已结束", "stopped": "已停止", "error": "已中断"}
    progress = task.get("progress") if isinstance(task.get("progress"), Mapping) else {}
    lines = [
        title,
        "店铺：%s（%s）" % (shop_name, shop_id or "无短 ID"),
        "负责人：%s" % mention,
        "本次%s：%s；成功 %s，失败 %s，跳过 %s" % (
            "存草稿" if task.get("save_only") else "上架",
            state_labels.get(str(task.get("state") or ""), "已结束"),
            progress.get("success", 0), progress.get("failed", 0),
            progress.get("skipped", 0),
        ),
    ]
    if test_message:
        lines.append("仅测试群通知和 @提醒，没有实际上架异常。")
    if errors:
        limit = max(1, min(int(config.get("max_error_details") or 10), 10))
        for index, row in enumerate(errors[:limit], start=1):
            label = _safe_text(row.get("name") or row.get("model") or row.get("product_id") or "未知商品", 70)
            reason = _operator_reason(row.get("reason") or task.get("message"))
            lines.append("%d. %s：%s" % (index, label, reason))
        if len(errors) > limit:
            lines.append("另有 %d 条，请在面板查看。" % (len(errors) - limit))
    elif str(task.get("state") or "") == "error":
        lines.append("原因：%s" % _operator_reason(task.get("message") or "任务进程异常退出"))
    if not owner:
        lines.append("该店铺未配置负责人，请联系管理员。")
    if not test_message:
        lines.append("详情请在面板查看本次任务。")
    return "\n".join(lines)


class LarkCliSender:
    def __init__(self, config: Mapping[str, Any]):
        self.config = config
        configured = str(os.environ.get("DP_LARK_CLI_BIN") or "").strip()
        self.cli_bin = configured or shutil.which("lark-cli") or DEFAULT_CLI_FALLBACK

    def send_text(self, text: str, *, idempotency_key: str) -> dict[str, Any]:
        command = [
            self.cli_bin, "im", "+messages-send",
            "--profile", str(self.config.get("cli_profile") or "default"),
            "--as", str(self.config.get("send_as") or "user"),
            "--chat-id", str(self.config["chat_id"]),
            "--msg-type", "text",
            "--content", json.dumps({"text": text}, ensure_ascii=False),
            "--idempotency-key", idempotency_key[:50],
        ]
        env = dict(os.environ)
        env.update({
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
            "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
        })
        completed = subprocess.run(
            command, cwd=str(WORK_DIR), env=env, text=True, encoding="utf-8",
            errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=False,
        )
        output = (completed.stdout or completed.stderr or "").strip()
        try:
            envelope = json.loads(output)
        except ValueError:
            envelope = {"ok": False, "error": {"message": _safe_text(output or "CLI 无输出")}}
        if completed.returncode != 0 or envelope.get("ok") is not True:
            error = envelope.get("error") if isinstance(envelope.get("error"), Mapping) else {}
            raise RuntimeError("飞书报错通知发送失败：%s" % _safe_text(error.get("message") or output))
        return dict(envelope)


def notify_task_errors(
    task: Mapping[str, Any],
    errors: Sequence[Mapping[str, Any]],
    *,
    config_path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH,
    sender: LarkCliSender | None = None,
    test_message: bool = False,
) -> dict[str, Any]:
    config = load_notification_config(config_path)
    if not bool(config.get("enabled")):
        return {"sent": False, "reason": "disabled"}
    if not test_message and not should_notify(task, errors):
        return {"sent": False, "reason": "no_operational_error"}
    text = build_task_error_message(task, errors, config, test_message=test_message)
    task_id = _safe_text(task.get("id") or "unknown", 30)
    key_prefix = "ops-test" if test_message else "ops-error"
    key = "%s-%s" % (key_prefix, task_id)
    result = (sender or LarkCliSender(config)).send_text(text, idempotency_key=key)
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    return {"sent": True, "message_id": data.get("message_id") or result.get("message_id"), "text": text}


def main() -> int:
    parser = argparse.ArgumentParser(description="运营报错飞书通知链路")
    parser.add_argument("--test", action="store_true", help="发送明确标注的链路测试消息")
    parser.add_argument("--shop-id", default="7970406")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = parser.parse_args()
    if not args.test:
        parser.error("命令行仅开放 --test；真实任务通知由 panel_server 调用")
    now = int(time.time())
    task = {
        "id": "test-%s" % now,
        "shop_id": str(args.shop_id),
        "shop_name": "",
        "engine": "api_media",
        "save_only": True,
        "state": "done",
        "progress": {"success": 0, "failed": 1, "skipped": 0},
    }
    errors = [{
        "product_id": "TEST",
        "name": "通知链路测试商品",
        "error_type": "链路测试",
        "reason": "这不是实际运营报错",
    }]
    result = notify_task_errors(
        task, errors, config_path=args.config, test_message=True,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

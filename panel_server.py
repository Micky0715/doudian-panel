#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
panel_server.py —— 抖店多店铺控制面板 后端服务

启动:  /usr/bin/python3 panel_server.py
访问:  http://127.0.0.1:8080 （局域网访问请把 HOST 设为 0.0.0.0）

特性:
  - API 素材草稿使用独立无浏览器执行器；Playwright 仅作显式备用链路
  - 最多 4 个任务并行，浏览器任务各有独立 Chrome 用户数据目录
  - 温和停止：写入停止标志文件，脚本在当前商品处理完毕后安全退出
  - SSE 实时推送日志与进度（比 WebSocket 更简单，浏览器自动重连）
  - 浏览器逻辑由 panel_runner.py 调用 test_doudian_batch.py
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import socket
import uuid
from collections import deque

from flask import Flask, Response, jsonify, request, send_from_directory

from operator_error_notifier import notify_task_errors

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WORK_DIR)


def load_local_environment():
    """Load an optional local secret file without overriding service/system variables."""
    configured = str(os.environ.get("DP_ENV_FILE") or "").strip()
    candidates = [configured] if configured else [
        os.path.join(WORK_DIR, ".env.local"), os.path.join(WORK_DIR, "env.local"),
    ]
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            from doudian_api import read_env_file
            values = read_env_file(path)
            for key, value in values.items():
                os.environ.setdefault(key, value)
            print("[CONFIG] loaded local environment: %s" % os.path.basename(path))
            return path
        except Exception as exc:
            print("[WARN] failed to load local environment: %s" % exc)
    return ""


LOCAL_ENV_FILE = load_local_environment()

# ========== 配置 ==========
MAX_PARALLEL = 4                      # 并行上限
PORT = 8080
HOST = os.environ.get("DP_HOST", "0.0.0.0")  # 0.0.0.0=开放局域网；127.0.0.1=仅本机（可用环境变量 DP_HOST 覆盖）
# 注意：macOS Monterey+ 的 AirPlay Receiver 占用 5000 端口，返回 403，不可用
PYTHON_BIN = sys.executable           # 用启动本服务的同一个解释器跑脚本
RUNNER = os.path.join(WORK_DIR, "panel_runner.py")
API_MEDIA_RUNNER = os.path.join(WORK_DIR, "panel_api_media_runner.py")
LOG_BUFFER_SIZE = 3000                # 每个任务在内存中保留的日志行数

PROFILE_ROOT = WORK_DIR
PROCESSED_DIR = os.path.join(WORK_DIR, "panel_data", "processed")
RUNLOG_DIR = os.path.join(WORK_DIR, "panel_data", "logs")
STOP_DIR = os.path.join(WORK_DIR, "panel_data", "stop")
for _d in (PROCESSED_DIR, RUNLOG_DIR, STOP_DIR):
    os.makedirs(_d, exist_ok=True)

LEGACY_PROCESSED = os.path.join(WORK_DIR, "processed_products.json")

# 并行槽位 → Chrome 用户数据目录。
# 槽位 1 复用原有目录（已有登录态，无需重新扫码）；槽位 2/3/4 首次使用需各扫码一次。
_PROFILE_SUFFIX = "_win" if os.name == "nt" else ""
PROFILE_SLOTS = [
    os.path.join(PROFILE_ROOT, "chrome_user_data%s" % _PROFILE_SUFFIX),
    os.path.join(PROFILE_ROOT, "chrome_user_data_2%s" % _PROFILE_SUFFIX),
    os.path.join(PROFILE_ROOT, "chrome_user_data_3%s" % _PROFILE_SUFFIX),
    os.path.join(PROFILE_ROOT, "chrome_user_data_4%s" % _PROFILE_SUFFIX),
][:MAX_PARALLEL]


# ========== 店铺映射（从原脚本读取，避免重复维护） ==========

def load_shop_mapping():
    try:
        import test_doudian_batch as dd
        return dict(dd.SHOP_MAPPING)
    except Exception as e:
        print("[WARN] 无法从 test_doudian_batch 读取 SHOP_MAPPING: %s" % e)
        # API 型号体检不依赖 Playwright；维护机未安装浏览器驱动时仍应能使用面板。
        return {
            "爱回收官方旗舰店": "129675602",
            "爱回收二手手机旗舰店": "636781",
            "爱回收严选二手数码": "7970406",
            "爱回收严选二手优品": "51165770",
            "拍拍二手悦玥手机专卖店": "25595926",
            "拍拍严选手机优品": "168976032",
            "拍拍严选二手手机": "5576543",
            "拍拍二手数码": "156450040",
            "返航新生二手手机旗舰店": "208501840",
            "悦玥潮品生活坊": "223523435",
            "爱回收优品二手手机数码": "221724193",
            "拍机堂Mall线上二号店": "221729127",
            "新生严选二手手手": "293816952",
        }


SHOP_MAPPING = load_shop_mapping()


# ========== 事件广播 ==========

class Broadcaster(object):
    def __init__(self):
        self._clients = []
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def publish(self, event):
        data = json.dumps(event, ensure_ascii=False)
        with self._lock:
            targets = list(self._clients)
        for q in targets:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass


bus = Broadcaster()


# ========== 任务 ==========

class Task(object):
    def __init__(self, shop_id, mode, test_n, slot, save_only=False, product_id="", cover_all_models=False, full_retest=False,
                 engine="browser", media_folder_id=""):
        self.id = uuid.uuid4().hex[:8]
        self.shop_id = shop_id
        self.shop_name = shop_name_of(shop_id)
        self.mode = mode
        self.test_n = test_n
        self.save_only = bool(save_only)
        self.product_id = str(product_id or "").strip()
        self.cover_all_models = bool(cover_all_models)
        self.full_retest = bool(full_retest)
        self.engine = engine
        self.media_folder_id = str(media_folder_id or "")
        self.slot = slot
        self.profile_dir = PROFILE_SLOTS[slot] if slot is not None else ""
        self.state = "starting" if slot is not None else "queued"
        self.message = "" if slot is not None else "等待执行槽位"
        self.created_at = time.time()
        self.started_at = self.created_at if slot is not None else None
        self.ended_at = None
        self.queue_position = None
        self.stop_requested = False
        self.progress = {"total": 0, "processed": 0, "success": 0,
                         "failed": 0, "skipped": 0, "current": ""}
        self.logs = deque(maxlen=LOG_BUFFER_SIZE)
        self.proc = None
        self.stop_file = os.path.join(STOP_DIR, "stop_%s" % self.id)
        self.processed_log = os.path.join(
            PROCESSED_DIR, "products_%s.json" % (shop_id or "current"))
        self.debug_log = os.path.join(
            RUNLOG_DIR, "run_%s_%s_%s.log" % (shop_id or "current", time.strftime("%Y%m%d_%H%M%S"), self.id))
        self.errors = []              # 图片异常明细（查看报错面板）
        self.error_notification_started = False

    def brief(self):
        return {
            "id": self.id,
            "shop_id": self.shop_id,
            "shop_name": self.shop_name,
            "mode": self.mode,
            "engine": self.engine,
            "test_n": self.test_n,
            "save_only": self.save_only,
            "product_id": self.product_id,
            "cover_all_models": self.cover_all_models,
            "full_retest": self.full_retest,
            "slot": self.slot + 1 if self.slot is not None else None,
            "queue_position": self.queue_position,
            "state": self.state,
            "message": self.message,
            "stop_requested": self.stop_requested,
            "progress": dict(self.progress),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "log_count": len(self.logs),
            "error_count": len(self.errors),
            "debug_log": os.path.basename(self.debug_log),
        }

    def is_active(self):
        return self.state in ("queued", "starting", "waiting_login", "running", "stopping")

    def consumes_slot(self):
        return self.state in ("starting", "waiting_login", "running", "stopping")


TASKS = {}
TASKS_LOCK = threading.Lock()
TASK_QUEUE = deque()
DISPATCH_LOCK = threading.Lock()


def shop_name_of(shop_id):
    for name, sid in SHOP_MAPPING.items():
        if sid == shop_id:
            return name
    return ""


def is_known_shop_id(shop_id):
    """Only configured numeric shop IDs may reach paths or worker processes."""
    return bool(shop_id) and shop_id in SHOP_MAPPING.values()


def api_media_folder_ids():
    """Return only explicit, valid shop-to-material-folder bindings."""
    try:
        raw = json.loads(os.environ.get("DP_DOUDIAN_MATERIAL_FOLDER_IDS_JSON") or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(shop_id): str(folder_id)
        for shop_id, folder_id in raw.items()
        if is_known_shop_id(str(shop_id)) and str(folder_id).isdigit()
    }


def active_tasks():
    """Tasks currently consuming one of the four execution slots."""
    return [t for t in TASKS.values() if t.consumes_slot()]


def free_slot():
    """返回一个未被占用的并行槽位下标，没有则 None"""
    used = {t.slot for t in active_tasks() if t.slot is not None}
    for i in range(MAX_PARALLEL):
        if i not in used:
            return i
    return None


def seed_processed_log(task):
    """首次为某店铺创建已处理日志时，从旧的 processed_products.json 继承历史记录"""
    if os.path.exists(task.processed_log):
        return
    if os.path.exists(LEGACY_PROCESSED):
        try:
            shutil.copyfile(LEGACY_PROCESSED, task.processed_log)
        except Exception:
            pass


# ========== 子进程读取线程 ==========

def push(task, event):
    event["task_id"] = task.id
    bus.publish(event)


def add_log(task, entry):
    task.logs.append(entry)
    push(task, {"type": "log", "log": entry})


def set_state(task, state, message=""):
    task.state = state
    if message:
        task.message = message
    if state in ("done", "stopped", "error"):
        task.ended_at = time.time()
    push(task, {"type": "state", "task": task.brief()})


def _send_task_error_notification(task):
    """Send one best-effort alert without delaying task/queue completion."""
    try:
        result = notify_task_errors(task.brief(), list(task.errors))
        if result.get("sent"):
            add_log(task, {"time": time.strftime("%H:%M:%S"), "tag": "FEISHU",
                           "msg": "运营报错已推送到“自动上架工具报错”群",
                           "level": "success"})
    except Exception as exc:
        add_log(task, {"time": time.strftime("%H:%M:%S"), "tag": "FEISHU",
                       "msg": "运营报错群推送失败：%s" % exc,
                       "level": "error"})


def schedule_task_error_notification(task):
    """At most one notification attempt for each terminal task."""
    if task.error_notification_started:
        return False
    if not task.errors and task.state != "error":
        return False
    task.error_notification_started = True
    threading.Thread(target=_send_task_error_notification, args=(task,), daemon=True).start()
    return True


def _refresh_queue_positions():
    """Drop stale queue entries and publish stable FIFO positions."""
    with TASKS_LOCK:
        valid = []
        while TASK_QUEUE:
            task_id = TASK_QUEUE.popleft()
            task = TASKS.get(task_id)
            if task is not None and task.state == "queued":
                valid.append(task_id)
        TASK_QUEUE.extend(valid)
        queued = []
        for position, task_id in enumerate(TASK_QUEUE, start=1):
            task = TASKS[task_id]
            task.queue_position = position
            task.message = "等待执行槽位 · 排队第 %d 位" % position
            queued.append(task)
    for task in queued:
        push(task, {"type": "state", "task": task.brief()})


def _launch_task(task):
    """Launch one task that already owns a slot. Queue selection lives elsewhere."""
    if task.slot is None:
        raise RuntimeError("任务尚未分配执行槽位")
    task.profile_dir = PROFILE_SLOTS[task.slot]
    task.started_at = time.time()
    task.queue_position = None
    if task.engine == "browser":
        seed_processed_log(task)
        os.makedirs(task.profile_dir, exist_ok=True)

    env = dict(os.environ)
    default_image_root = os.path.join(os.path.expanduser("~"), "商品图片")
    default_white_library = os.path.join(os.path.expanduser("~"), "白底图完整版")
    env.update({
        "DP_TASK_ID": task.id,
        "DP_SHOP_ID": task.shop_id or "",
        "DP_MODE": task.mode,
        "DP_MEDIA_FOLDER_ID": task.media_folder_id,
        "DP_MEDIA_LIMIT": str(task.test_n),
        "DP_MEDIA_PRODUCT_ID": task.product_id,
        "DP_MEDIA_ACTION": "save_draft" if task.save_only else "publish",
        "DP_MEDIA_SOURCE": env.get("DP_MEDIA_SOURCE", "feishu"),
        "DP_PREFLIGHT_WORKERS": env.get("DP_PREFLIGHT_WORKERS", "2"),
        "DP_TEST_N": str(task.test_n),
        "DP_SAVE_DRAFT_ONLY": "1" if task.save_only else "0",
        "DP_TARGET_PRODUCT_ID": task.product_id,
        "DP_COVER_ALL_MODELS": "1" if task.cover_all_models else "0",
        "DP_FULL_RETEST": "1" if task.full_retest else "0",
        "DP_CROSS_STORE_IMAGE_FALLBACK": "0",
        "DP_BROWSER_IMAGE_SOURCE": env.get(
            "DP_BROWSER_IMAGE_SOURCE", "feishu_local_fallback"
        ),
        "DP_BROWSER_CLOUD_CACHE_ROOT": env.get(
            "DP_BROWSER_CLOUD_CACHE_ROOT",
            os.path.join(WORK_DIR, "panel_data", "browser_cloud_material_cache"),
        ),
        "DP_PRODUCT_IMAGE_ROOT": env.get("DP_PRODUCT_IMAGE_ROOT", default_image_root),
        "DP_WHITE_IMAGE_LIBRARY": env.get("DP_WHITE_IMAGE_LIBRARY", default_white_library),
        "DP_PROFILE_DIR": task.profile_dir,
        "DP_PROCESSED_LOG": task.processed_log,
        "DP_DEBUG_LOG": task.debug_log,
        "DP_STOP_FILE": task.stop_file,
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    try:
        task.proc = subprocess.Popen(
            [PYTHON_BIN, "-u", API_MEDIA_RUNNER if task.engine == "api_media" else RUNNER],
            cwd=WORK_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except Exception as exc:
        set_state(task, "error", "启动失败: %s" % exc)
        return False

    add_log(task, {"time": time.strftime("%H:%M:%S"), "tag": "PANEL",
                   "msg": ("API 素材任务已启动 | 不启动浏览器 | 槽位 %d" % (task.slot + 1)
                           if task.engine == "api_media" else
                           "浏览器任务已启动 | 槽位 %d | 浏览器目录 %s" %
                           (task.slot + 1, os.path.basename(task.profile_dir))),
                   "level": "info"})
    set_state(task, "starting", "进程已拉起")
    threading.Thread(target=reader_thread, args=(task,), daemon=True).start()
    return True


def dispatch_queued_tasks():
    """Fill free slots from the global FIFO queue without exceeding the limit."""
    with DISPATCH_LOCK:
        while True:
            with TASKS_LOCK:
                slot = free_slot()
                task = None
                while slot is not None and TASK_QUEUE:
                    task_id = TASK_QUEUE.popleft()
                    candidate = TASKS.get(task_id)
                    if candidate is None or candidate.state != "queued":
                        continue
                    candidate.slot = slot
                    candidate.profile_dir = PROFILE_SLOTS[slot]
                    candidate.state = "starting"
                    candidate.message = "排队结束，正在启动"
                    candidate.queue_position = None
                    task = candidate
                    break
            _refresh_queue_positions()
            if task is None:
                return
            if not _launch_task(task):
                # A failed spawn frees the claimed slot immediately; continue
                # with the next queued shop rather than blocking the FIFO.
                continue


def reader_thread(task):
    proc = task.proc
    try:
        for raw in iter(proc.stdout.readline, ""):
            if raw == "":
                break
            line = raw.rstrip("\n")
            if line.startswith("##EVT"):
                try:
                    ev = json.loads(line[5:])
                except Exception:
                    continue
                t = ev.get("t")
                if t == "log":
                    add_log(task, {
                        "time": ev.get("time") or time.strftime("%H:%M:%S"),
                        "tag": ev.get("tag", ""),
                        "msg": ev.get("msg", ""),
                        "level": ev.get("level", "info"),
                    })
                elif t == "progress":
                    for k in ("total", "processed", "success", "failed", "skipped", "current"):
                        if k in ev:
                            task.progress[k] = ev[k]
                    push(task, {"type": "progress", "task": task.brief()})
                elif t == "shop":
                    if ev.get("name"):
                        task.shop_name = ev["name"]
                    if ev.get("id"):
                        task.shop_id = ev["id"]
                    push(task, {"type": "state", "task": task.brief()})
                elif t == "errors":
                    task.errors = ev.get("errors", [])
                    push(task, {"type": "errors", "task": task.brief(), "errors": task.errors})
                elif t == "state":
                    st = ev.get("state", "")
                    if st in ("starting", "waiting_login", "running", "stopping"):
                        set_state(task, st, ev.get("message", ""))
                    else:
                        task.message = ev.get("message", "")
            else:
                # 非结构化输出（原脚本的 print / 第三方库输出）也照样展示
                if line.strip():
                    add_log(task, {"time": time.strftime("%H:%M:%S"),
                                   "tag": "OUT", "msg": line, "level": "info"})
    except Exception as e:
        add_log(task, {"time": time.strftime("%H:%M:%S"), "tag": "PANEL",
                       "msg": "读取子进程输出异常: %s" % e, "level": "error"})
    finally:
        code = proc.wait()
        if task.stop_requested:
            final = "stopped"
            msg = "已按温和停止退出"
        elif code == 0:
            final = "done"
            msg = "任务正常结束"
        else:
            final = "error"
            msg = "进程退出码 %s" % code
        add_log(task, {"time": time.strftime("%H:%M:%S"), "tag": "PANEL",
                       "msg": msg, "level": "success" if final == "done" else
                       ("warning" if final == "stopped" else "error")})
        set_state(task, final, msg)
        schedule_task_error_notification(task)
        try:
            if os.path.exists(task.stop_file):
                os.remove(task.stop_file)
        except Exception:
            pass
        dispatch_queued_tasks()


def start_task(shop_id, mode, test_n, save_only=False, product_id="", cover_all_models=False, full_retest=False,
               engine="browser", media_folder_id=""):
    if not is_known_shop_id(shop_id):
        return None, "请选择有效店铺"
    if engine not in ("browser", "api_media"):
        return None, "未知执行链路"
    if engine == "api_media" and (not str(media_folder_id).isdigit() or not 1 <= test_n <= 999):
        return None, "API 素材任务需要有效素材中心文件夹，测试数量须为 1—999 件"
    with TASKS_LOCK:
        for t in TASKS.values():
            if not t.is_active():
                continue
            if shop_id and t.shop_id == shop_id:
                label = "排队" if t.state == "queued" else "运行"
                return None, "该店铺已有任务在%s（任务 %s）" % (label, t.id)
        task = Task(shop_id, mode, test_n, None, save_only=save_only, product_id=product_id,
                    cover_all_models=cover_all_models, full_retest=full_retest,
                    engine=engine, media_folder_id=media_folder_id)
        TASKS[task.id] = task
        TASK_QUEUE.append(task.id)
    _refresh_queue_positions()
    dispatch_queued_tasks()
    if task.state == "queued":
        return task, "并发槽位已满，任务已进入等待队列（第 %d 位）" % task.queue_position
    if task.state == "error":
        return None, task.message or "启动失败"
    return task, "任务已启动"


# ========== Flask ==========

app = Flask(__name__, static_folder=None)
from app.gaia_listing.api.routes import create_gaia_blueprint

app.register_blueprint(create_gaia_blueprint(WORK_DIR, SHOP_MAPPING.values()))


@app.route("/")
def index():
    return send_from_directory(WORK_DIR, "panel.html")


@app.route("/album")
def album():
    return send_from_directory(WORK_DIR, "album.html")


@app.route("/assets/<path:filename>")
def serve_assets(filename):
    return send_from_directory(os.path.join(WORK_DIR, "assets"), filename, max_age=0)


@app.route("/api/shops")
def api_shops():
    shops = [{"name": n, "id": i} for n, i in SHOP_MAPPING.items()]
    return jsonify({"shops": shops, "max_parallel": MAX_PARALLEL})


@app.route("/api/config-status")
def api_config_status():
    """Expose presence only; never return API keys or tokens to the browser."""
    required = ("DOUDIAN_APP_KEY", "DOUDIAN_APP_SECRET", "DOUDIAN_SHOP_TOKENS_JSON")
    api_ready = all(bool(os.environ.get(key)) for key in required)
    folder_count = len(api_media_folder_ids())
    publish_enabled = os.environ.get("DP_API_MEDIA_PUBLISH_ENABLED") == "1"
    return jsonify({
        "doudian_api_ready": api_ready,
        "local_env_loaded": bool(LOCAL_ENV_FILE),
        "cross_store_image_fallback": False,
        "feishu_material_ready": bool(os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_APP_SECRET")),
        "browser_image_source": os.environ.get(
            "DP_BROWSER_IMAGE_SOURCE", "feishu_local_fallback"
        ),
        "api_media_source": os.environ.get("DP_MEDIA_SOURCE", "feishu"),
        "api_media_configured_shop_count": folder_count,
        "api_media_save_draft_ready": api_ready and folder_count > 0,
        "api_media_publish_enabled": publish_enabled,
        "api_image_migration_mode": (
            "api_media_publish_with_readback" if api_ready and folder_count > 0 and publish_enabled
            else "api_media_draft_with_readback" if api_ready and folder_count > 0
            else "browser_fallback_only"
        ),
    })


@app.route("/api/model-catalogue")
def api_model_catalogue():
    """Search both Gaia and DouDian Excel-derived catalogues; never mutates data."""
    paths = [
        ("gaia", os.environ.get("DP_MODEL_CATALOGUE",
            os.path.join(WORK_DIR, "panel_data", "gaia_excel_phone_models.json"))),
        ("doudian", os.environ.get("DP_DOUDIAN_MODEL_CATALOGUE",
            os.path.join(WORK_DIR, "panel_data", "doudian_phone_models.json"))),
    ]
    try:
        from model_catalogue import ModelCatalogue, model_key
        query = str(request.args.get("q") or "").strip()
        query_fold = query.casefold()
        query_model = model_key(query)
        rows = []
        counts = {}
        for source, path in paths:
            catalogue = ModelCatalogue.from_json(path)
            counts[source] = len(catalogue.rows)
            if not query:
                continue
            for row in catalogue.rows:
                brand = str(row.get("brand") or "")
                model = str(row.get("model") or "")
                if (query_fold in (brand + " " + model).casefold() or
                        (query_model and query_model in model_key(model))):
                    rows.append({"brand": brand, "model": model,
                                 "rows": row.get("rows", 0), "source": source})
                    if len(rows) >= 30:
                        break
        return jsonify({"success": True, "count": sum(counts.values()),
                        "counts": counts, "results": rows})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 400


@app.route("/api/model-audit", methods=["POST"])
def api_model_audit():
    """Read-only audit of property 1493 against DouDian's current options."""
    data = request.get_json(silent=True) or {}
    shop_id = str(data.get("shop_id") or "").strip()
    product_id = str(data.get("product_id") or "").strip()
    if not shop_id or not product_id:
        return jsonify({"success": False, "message": "请选择店铺并填写商品 ID"}), 400
    if not is_known_shop_id(shop_id):
        return jsonify({"success": False, "message": "店铺 ID 无效"}), 400
    if not product_id.isdigit():
        return jsonify({"success": False, "message": "商品 ID 必须是数字"}), 400
    try:
        from doudian_api import (
            DoudianClient, DoudianConfig, MODEL_PROPERTY_ID,
            find_property, resolve_model_option,
        )
        config = DoudianConfig.from_mapping(os.environ)
        client = DoudianClient(config, shop_id)
        detail = client.get_product_detail(product_id)
        raw_format = detail.get("product_format_new") or {}
        product_format = json.loads(raw_format) if isinstance(raw_format, str) else raw_format
        current_rows = product_format.get(str(MODEL_PROPERTY_ID), []) if isinstance(product_format, dict) else []
        current = current_rows[0] if current_rows else {}
        current_name = str(current.get("Name", current.get("name", "")) or "").strip()
        current_value = current.get("Value", current.get("value"))
        diy_type = current.get("diy_type")

        properties = client.get_category_properties()
        model_property = find_property(properties, MODEL_PROPERTY_ID) or {}
        resolved = resolve_model_option(model_property, current_name)
        recommended = None
        if resolved:
            recommended = {"name": resolved.get("name"), "value": resolved.get("value")}
        is_official = bool(resolved) and str(current_value) == str(resolved.get("value")) and diy_type == 0
        return jsonify({
            "success": True,
            "product_id": product_id,
            "title": detail.get("name", ""),
            "current": {"name": current_name, "value": current_value, "diy_type": diy_type},
            "recommended": recommended,
            "status": "official" if is_official else ("repairable" if resolved else "unresolved"),
            "message": ("型号已是官方值" if is_official else
                        "已找到唯一官方型号，可修复" if resolved else
                        "没有找到唯一官方型号，需要人工确认"),
        })
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 400


@app.route("/api/tasks")
def api_tasks():
    with TASKS_LOCK:
        items = sorted(TASKS.values(), key=lambda t: t.created_at, reverse=True)
        data = [t.brief() for t in items]
    return jsonify({"tasks": data,
                    "active": len([d for d in data
                                   if d["state"] in ("starting", "waiting_login",
                                                     "running", "stopping")]),
                    "queued": len([d for d in data if d["state"] == "queued"]),
                    "max_parallel": MAX_PARALLEL})


@app.route("/api/task/<tid>/logs")
def api_task_logs(tid):
    task = TASKS.get(tid)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({"logs": list(task.logs), "task": task.brief()})


@app.route("/api/task/<tid>/errors")
def api_task_errors(tid):
    task = TASKS.get(tid)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({"errors": list(task.errors), "count": len(task.errors),
                    "task": task.brief()})


@app.route("/api/errors/all")
def api_errors_all():
    """汇总已完成 / 已停止 / 出错 任务的全部报错明细，便于一键查看。

    每条报错附带来源任务信息（task_id / shop_id / shop_name / state），
    ID 由前端按内容哈希生成，保证同一报错在不同刷新下稳定可定位。
    """
    with TASKS_LOCK:
        tasks = sorted(TASKS.values(), key=lambda t: t.created_at, reverse=True)
        finished = [t for t in tasks if t.state in ("done", "stopped", "error")]
        out = []
        for t in finished:
            for e in t.errors:
                out.append({
                    "name": e.get("name", ""),
                    "brand": e.get("brand", ""),
                    "model": e.get("model", ""),
                    "error_type": e.get("error_type", ""),
                    "reason": e.get("reason", ""),
                    "task_id": t.id,
                    "shop_id": t.shop_id,
                    "shop_name": t.shop_name,
                    "state": t.state,
                })
        task_sum = [{"id": t.id, "shop_name": t.shop_name,
                     "state": t.state, "error_count": len(t.errors)}
                    for t in finished]
    return jsonify({"errors": out, "count": len(out), "tasks": task_sum})


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(silent=True) or {}
    shop_id = (data.get("shop_id") or "").strip()
    if not is_known_shop_id(shop_id):
        return jsonify({"success": False, "message": "请选择有效店铺"}), 400
    mode = data.get("mode") or "test"
    if mode not in ("test", "batch"):
        mode = "test"
    try:
        test_n = int(data.get("test_n") or 1)
    except Exception:
        test_n = 1
    test_n = max(1, min(test_n, 999))
    # Only an explicitly selected staged rollout may override the quantity.
    # Older cached pages always sent their default "smoke" option, which made
    # a visible 1-item test silently become 10 items.
    release_stage = str(data.get("release_stage") or "").strip().lower()
    if bool(data.get("release_stage_explicit")):
        if release_stage == "smoke":
            mode, test_n = "test", 10
        elif release_stage == "pilot":
            mode, test_n = "test", 20
        elif release_stage == "full":
            mode = "batch"
    else:
        release_stage = ""
    save_only = bool(data.get("save_only", False))
    cover_all_models = bool(data.get("cover_all_models", False))
    full_retest = bool(data.get("full_retest", False))
    product_id = str(data.get("product_id") or "").strip()
    if product_id and not product_id.isdigit():
        return jsonify({"success": False, "message": "商品 ID 只能包含数字"}), 400

    engine = str(data.get("engine") or "browser").strip()
    if engine not in ("browser", "api_media"):
        return jsonify({"success": False, "message": "未知执行链路"}), 400
    media_folder_id = ""
    if engine == "api_media":
        if not save_only and os.environ.get("DP_API_MEDIA_PUBLISH_ENABLED") != "1":
            return jsonify({"success": False, "message":
                            "API 面板直发尚未放开：型号/品牌与颜色规格图全流程需先验收；目前请选择仅保存草稿"}), 400
        # 批量模式由 worker 在读取完整快照后使用实际草稿总数；这里的
        # 999 只是启动参数占位，不会把任务截断为 10 件。
        if mode == "batch":
            test_n = 999
        else:
            test_n = min(test_n, 999)
        media_folder_id = api_media_folder_ids().get(shop_id, "")
        if not media_folder_id.isdigit():
            return jsonify({"success": False, "message": "该店铺尚未配置抖店素材中心文件夹，API 任务不会自动回退浏览器"}), 400

    task, msg = start_task(shop_id, mode, test_n, save_only=save_only,
                           product_id=product_id, cover_all_models=cover_all_models,
                           full_retest=full_retest, engine=engine,
                           media_folder_id=media_folder_id)
    if not task:
        return jsonify({"success": False, "message": msg}), 400
    return jsonify({"success": True, "message": msg, "task": task.brief()})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    data = request.get_json(silent=True) or {}
    tid = (data.get("task_id") or "").strip()
    task = TASKS.get(tid)
    if not task:
        return jsonify({"success": False, "message": "任务不存在"}), 404
    if not task.is_active():
        return jsonify({"success": False, "message": "任务已结束"}), 400
    if task.state == "queued":
        with TASKS_LOCK:
            try:
                TASK_QUEUE.remove(task.id)
            except ValueError:
                pass
            task.stop_requested = True
        add_log(task, {"time": time.strftime("%H:%M:%S"), "tag": "PANEL",
                       "msg": "已取消等待中的任务，未启动子进程",
                       "level": "warning"})
        set_state(task, "stopped", "已从等待队列取消")
        _refresh_queue_positions()
        return jsonify({"success": True, "message": "已取消排队任务；未执行商品操作"})
    try:
        with open(task.stop_file, "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        return jsonify({"success": False, "message": "写入停止标志失败: %s" % e}), 500
    task.stop_requested = True
    add_log(task, {"time": time.strftime("%H:%M:%S"), "tag": "PANEL",
                   "msg": "⚠ 已发送停止信号，将在当前商品处理完毕后安全退出",
                   "level": "warning"})
    set_state(task, "stopping", "等待当前商品处理完毕")
    return jsonify({"success": True, "message": "停止信号已发送，当前商品完成后停止"})


@app.route("/api/task/<tid>", methods=["DELETE"])
def api_remove(tid):
    task = TASKS.get(tid)
    if not task:
        return jsonify({"success": False, "message": "任务不存在"}), 404
    if task.is_active():
        return jsonify({"success": False, "message": "任务运行中，请先停止"}), 400
    with TASKS_LOCK:
        TASKS.pop(tid, None)
    bus.publish({"type": "removed", "task_id": tid})
    return jsonify({"success": True})


@app.route("/api/clear-logs", methods=["POST"])
def api_clear_logs():
    """清除指定店铺的本地运行日志（含 debug_log 和 processed_log）"""
    data = request.get_json(silent=True) or {}
    shop_id = (data.get("shop_id") or "").strip()
    if not shop_id:
        return jsonify({"success": False, "message": "必须指定店铺 ID"}), 400
    if not is_known_shop_id(shop_id):
        return jsonify({"success": False, "message": "店铺 ID 无效"}), 400

    cleared = []
    # 1. 清除该店铺的 debug 日志文件
    # 文件名格式：run_{shop_id}_{timestamp}_{task_id}.log（新格式）
    # 同时覆盖内存中仍保留的该店铺任务 debug_log（兼容旧格式/运行中任务）
    to_remove = set()
    try:
        for f in os.listdir(RUNLOG_DIR):
            fp = os.path.join(RUNLOG_DIR, f)
            if not os.path.isfile(fp) or not f.endswith(".log"):
                continue
            # 新文件名格式按店铺过滤
            if f.startswith("run_%s_" % shop_id):
                to_remove.add(fp)
    except Exception:
        pass
    # 内存中该店铺非运行中任务的 debug_log（避免删除正在写入的文件）
    with TASKS_LOCK:
        for t in TASKS.values():
            if t.shop_id == shop_id and not t.is_active() and getattr(t, "debug_log", None):
                to_remove.add(t.debug_log)
    for fp in to_remove:
        try:
            os.remove(fp)
            cleared.append(os.path.basename(fp))
        except Exception:
            pass

    # 2. 清除该店铺的已处理商品记录（让商品可重新跑）
    proc_file = os.path.join(PROCESSED_DIR, "products_%s.json" % shop_id)
    if os.path.exists(proc_file):
        try:
            os.remove(proc_file)
            cleared.append("products_%s.json" % shop_id)
        except Exception as e:
            pass

    # 3. 清除旧版 processed_products.json（如果指定的是当前使用的）
    if shop_id in [v for v in SHOP_MAPPING.values()]:
        if os.path.exists(LEGACY_PROCESSED):
            try:
                os.remove(LEGACY_PROCESSED)
                cleared.append("processed_products.json (legacy)")
            except Exception:
                pass

    # 4. 清除内存中该店铺所有任务的日志
    with TASKS_LOCK:
        for t in TASKS.values():
            if t.shop_id == shop_id and not t.is_active():
                t.logs.clear()

    msg = "已清除 %d 个日志文件" % len(cleared) if cleared else "没有找到可清除的日志"
    bus.publish({"type": "logs_cleared", "shop_id": shop_id, "count": len(cleared)})
    return jsonify({"success": True, "message": msg, "cleared_count": len(cleared),
                    "files": cleared})


@app.route("/api/clear-task-logs", methods=["POST"])
def api_clear_task_logs():
    """清除单次运行的日志（不动 processed_products 和全局日志）"""
    data = request.get_json(silent=True) or {}
    task_id = (data.get("task_id") or "").strip()
    if not task_id:
        return jsonify({"success": False, "message": "必须指定任务 ID"}), 400

    cleared = []
    with TASKS_LOCK:
        t = TASKS.get(task_id)
        if not t:
            return jsonify({"success": False, "message": "任务不存在"}), 404

        # 1. 删除本次运行的 debug_log 文件（如果有）
        if hasattr(t, "debug_log") and os.path.exists(t.debug_log):
            try:
                os.remove(t.debug_log)
                cleared.append(os.path.basename(t.debug_log))
            except Exception:
                pass

        # 2. 清除内存中的终端日志
        if t.logs:
            t.logs.clear()
            cleared.append("终端日志")

        # 3. 清除报错明细
        if t.errors:
            t.errors.clear()
            cleared.append("报错明细")

    msg = "已清除：%s" % "、".join(cleared) if cleared else "没有可清除的日志"
    bus.publish({"type": "task_logs_cleared", "task_id": task_id})
    return jsonify({"success": True, "message": msg, "cleared": cleared})


# ========== 网络信息与开机自启 ==========

def get_network_info():
    """返回本机当前可访问地址（IP 可能随 DHCP 变化，供前端实时展示）"""
    port = PORT
    ip = ""
    hostname = ""
    try:
        out = subprocess.run(["ipconfig", "getifaddr", "en1"],
                            capture_output=True, text=True, timeout=5).stdout.strip()
        ip = out
    except Exception:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            ip = ""
    try:
        hostname = subprocess.run(["scutil", "--get", "LocalHostName"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        hostname = ""
    urls = {
        "local": "http://127.0.0.1:%d" % port,
        "lan": "http://%s:%d" % (ip, port) if ip else "",
        "host": "http://%s.local:%d" % (hostname, port) if hostname else "",
    }
    return {"port": port, "ip": ip, "hostname": hostname, "urls": urls}


PLIST_PATH = os.path.expanduser("~/Library/LaunchAgents/com.atrenew.doudian-panel.plist")


@app.route("/api/network")
def api_network():
    return jsonify(get_network_info())


@app.route("/api/autostart/status")
def api_autostart_status():
    installed = False
    try:
        out = subprocess.run(["launchctl", "list"],
                            capture_output=True, text=True, timeout=5).stdout
        installed = "com.atrenew.doudian-panel" in out
    except Exception:
        installed = False
    return jsonify({"installed": installed})


def _pids_on_port(port):
    pids = set()
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"],
                            capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                pids.add(parts[1])
    except Exception:
        pass
    return pids


@app.route("/api/autostart", methods=["POST"])
def api_autostart():
    """切换开机自启状态（安装 / 卸载）。
    注意：launchctl 必须在用户图形会话中执行；若当前面板是从
    WorkBuddy/Bash 拉起的进程，会失败并给出引导提示。"""
    # 先查当前状态
    installed = os.path.exists(PLIST_PATH)
    try:
        out = subprocess.run(["launchctl", "list"],
                            capture_output=True, text=True, timeout=5).stdout
        installed = "com.atrenew.doudian-panel" in out
    except Exception:
        pass

    data = request.get_json(silent=True) or {}
    action = data.get("action", "toggle")  # install | uninstall | toggle
    if action not in ("install", "uninstall", "toggle"):
        return jsonify({"success": False, "installed": installed,
                        "message": "无效操作"}), 400

    if action == "toggle":
        action = "uninstall" if installed else "install"

    if action == "uninstall":
        if not installed:
            return jsonify({"success": True, "installed": False,
                            "message": "开机自启未安装，无需卸载"})
        r = subprocess.run(["launchctl", "unload", PLIST_PATH],
                          capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip()
            return jsonify({"success": False, "installed": True,
                            "message": "无法卸载开机自启",
                            "hint": "请双击「卸载开机自启.command」来卸载"
                                    "（需在图形会话中执行）。错误：%s" % err})
        return jsonify({"success": True, "installed": False,
                        "message": "已卸载开机自启。服务将在本次进程结束后不再自动重启。"
                                   "可双击「启动面板.command」手动启动。"})

    # action == "install"
    if not os.path.exists(PLIST_PATH):
        return jsonify({"success": False, "installed": False,
                        "message": "未找到启动配置文件 com.atrenew.doudian-panel.plist"})
    old_pids = _pids_on_port(PORT)
    r = subprocess.run(["launchctl", "load", PLIST_PATH],
                      capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        return jsonify({"success": False, "installed": installed,
                        "message": "无法开启开机自启",
                        "hint": "请双击「安装开机自启.command」来开启"
                                "（需在 Mac 图形会话中执行）。错误：%s" % err})
    time.sleep(4)
    for pid in old_pids:
        try:
            subprocess.run(["kill", pid], timeout=5)
        except Exception:
            pass
    time.sleep(1)
    up = bool(_pids_on_port(PORT))
    return jsonify({"success": True, "installed": True, "up": up,
                    "message": "已开启开机自启（崩溃自动重启）。"})


@app.route("/api/stream")
def api_stream():
    def gen():
        q = bus.subscribe()
        try:
            yield "retry: 2000\n\n"
            snapshot = {"type": "snapshot",
                        "tasks": [t.brief() for t in
                                  sorted(TASKS.values(), key=lambda x: x.created_at,
                                         reverse=True)]}
            yield "data: %s\n\n" % json.dumps(snapshot, ensure_ascii=False)
            while True:
                try:
                    data = q.get(timeout=15)
                    yield "data: %s\n\n" % data
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


def main():
    print("=" * 62)
    print("  抖店多店铺控制面板")
    print("  工作目录: %s" % WORK_DIR)
    print("  解释器  : %s" % PYTHON_BIN)
    print("  店铺数量: %d   并行上限: %d" % (len(SHOP_MAPPING), MAX_PARALLEL))
    print("  访问地址: http://%s:%d" % (HOST, PORT))
    print("=" * 62)
    app.run(host=HOST, port=PORT, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()

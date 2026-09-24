#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
panel_runner.py —— 控制面板的任务执行器（子进程入口）

设计原则：**不修改 test_doudian_batch.py 一行代码**。
本文件通过 import 复用其中的函数，自己实现批量循环，从而插入：
  1. 温和停止检查（每个商品处理完毕后检查停止文件）
  2. 结构化事件输出（供 panel_server.py 解析成进度 / 日志）
  3. 多实例隔离（每个任务独立的 chrome profile 与已处理日志）

由 panel_server.py 以子进程方式启动，全部参数通过环境变量传入：
  DP_TASK_ID       任务唯一 ID
  DP_SHOP_ID       目标店铺短 ID（为空则不切换店铺）
  DP_MODE          test | batch
  DP_TEST_N        测试模式处理数量
  DP_PROFILE_DIR   本任务专用的 chrome 用户数据目录
  DP_PROCESSED_LOG 本任务使用的已处理商品日志（json）
  DP_DEBUG_LOG     本任务的调试日志文件
  DP_STOP_FILE     温和停止标志文件，存在即表示请求停止
  DP_EXECUTION_PLAN API-D 生成并校验通过的 API-E 执行计划（可选）
"""

import io
import json
import os
import re
import sys
import time
import traceback

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WORK_DIR)

# 标准输出强制 UTF-8 且不缓冲，保证日志能实时流向服务端
try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass


# ========== 环境变量 ==========

TASK_ID = os.environ.get("DP_TASK_ID", "task")
SHOP_ID = (os.environ.get("DP_SHOP_ID") or "").strip()
MODE = os.environ.get("DP_MODE", "test")
TEST_N = int(os.environ.get("DP_TEST_N", "1") or "1")
PROFILE_DIR = os.environ.get("DP_PROFILE_DIR") or os.path.join(WORK_DIR, "chrome_user_data")
PROCESSED_LOG = os.environ.get("DP_PROCESSED_LOG") or os.path.join(WORK_DIR, "processed_products.json")
EXECUTION_PLAN_PATH = str(os.environ.get("DP_EXECUTION_PLAN") or "").strip()


def resolve_shop_image_base(image_dir):
    """Resolve both flat and legacy category-layered shop image layouts."""
    root = os.path.abspath(str(image_dir or "")) if image_dir else ""
    if not root:
        return ""
    legacy_phone_root = os.path.join(root, "3C数码及配件", "二手手机")
    return legacy_phone_root if os.path.isdir(legacy_phone_root) else root
DEBUG_LOG = os.environ.get("DP_DEBUG_LOG") or os.path.join(WORK_DIR, "debug_run.log")
STOP_FILE = os.environ.get("DP_STOP_FILE") or os.path.join(WORK_DIR, ".stop_" + TASK_ID)

DRAFT_URL = "https://fxg.jinritemai.com/ffa/g/draft"

# 日志中“型号与预期不匹配 / 型号选择失败 / 品牌型号缺失”类关键句，
# 在原始 log() 输出时自动前缀 ❌ [型号异常]，让文件日志和前端看板都能一眼定位。
_MODEL_MM_RE = re.compile(
    r'型号选择失败|型号=FAIL|品牌/型号不完整|型号未匹配|'
    r'发布前型号终检未通过|与预期不匹配|期望型号|型号.*不匹配'
)

# 兼容运营确认的“4 张图可发”规则，但替代行为必须醒目记录；可通过
# DP_ALLOW_WHITE_AS_MAIN=0 按部署收窄/关闭，禁止再静默替代。
# 规则（所有店铺统一）：
#   - 有主图            → 正常用主图，白底图照常传辅助位；
#   - 无主图、有白底图  → 主图位用首张白底图顶替，其余白底图传辅助位；
#   - 主图、白底图都无  → 判"主图白底图缺失"，跳过该商品。
# 通过 monkey-patch dd.get_main_image_path / get_aux_image_paths 实现（仅作用于当前子进程，
# 不修改核心脚本、不污染其他进程）。
SPECIAL_NO_MAIN_IMAGE_SHOPS = set()  # 历史保留字段：曾仅含 {"7970406"}，现已全局化，不再按店铺区分
SPECIAL_NO_MAIN = str(os.environ.get("DP_ALLOW_WHITE_AS_MAIN", "1")).strip().lower() in (
    "1", "true", "yes", "on"
)


# ========== 事件输出 ==========

def emit(obj):
    """向 stdout 输出一条结构化事件（服务端按行解析）"""
    try:
        sys.stdout.write("##EVT" + json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def guess_level(msg):
    """从日志文本推断级别，供前端着色"""
    s = str(msg)
    if "✅" in s or "成功" in s or "完成" in s:
        return "success"
    if "❌" in s or "失败" in s or "错误" in s or "异常" in s:
        return "error"
    if "⚠" in s or "警告" in s or "跳过" in s or "⏭" in s:
        return "warning"
    return "info"


def emit_log(tag, msg, level=None):
    emit({
        "t": "log",
        "time": time.strftime("%H:%M:%S"),
        "tag": str(tag),
        "msg": str(msg),
        "level": level or guess_level(msg),
    })


# 进度状态（全局，随时可推送）
PROGRESS = {
    "total": 0, "processed": 0,
    "success": 0, "failed": 0, "skipped": 0,
    "current": "",
}


def emit_progress():
    payload = {"t": "progress"}
    payload.update(PROGRESS)
    emit(payload)


def emit_state(state, message=""):
    emit({"t": "state", "state": state, "message": message})


def classify_image_error(parsed):
    """判断商品图片是否存在异常，返回 (类型, 原因) 或 (None, None)（正常）。

    两类异常（与前端「查看报错」面板对应）：
      - "型号未匹配": 在本地图片库中找不到该型号对应的文件夹
      - "主图白底图缺失": 型号已匹配，但主图或白底图目录下没有可用图片
    原因字段方便前端总表直接展示具体根因，无需再翻日志。
    """
    brand = parsed.get("brand_cn", "")
    model = parsed.get("model", "")
    try:
        img_dir = dd.get_image_dir(parsed)
    except Exception:
        return "型号未匹配", "无法解析该商品的图片目录（标题可能缺少品牌/型号信息）"
    if not os.path.isdir(img_dir):
        _ibd = getattr(dd, "IMAGE_BASE_DIR", "")
        try:
            _subs = [d for d in os.listdir(_ibd) if os.path.isdir(os.path.join(_ibd, d))]
        except Exception as _e:
            _subs = "LISTDIR_ERROR: %r" % _e
        log("DIAG", "  🔍 img_dir=%r IMAGE_BASE_DIR=%r brand_cn=%r model=%r"
            % (img_dir, _ibd, brand, model))
        log("DIAG", "    子目录列表=%r" % _subs)
        return "型号未匹配", "本地图库不存在该型号的图片文件夹：%s/%s" % (brand, model)
    # 通用规则（已全局化免主图特例）：有主图 或 有任意白底图 即放行；
    # 两者皆无才判"主图白底图缺失"并跳过。无主图时由 get_main_image_path 的
    # monkey-patch 自动用首张白底图顶替主图位（见 apply_no_main_mode）。
    has_main = os.path.isfile(dd.get_main_image_path(parsed))
    has_aux = len(dd.get_aux_image_paths(parsed)) > 0
    if has_main or has_aux:
        return None, None
    return "主图白底图缺失", "该型号目录下既无主图也无白底图：%s/%s" % (brand, model)


# 当前任务累计的图片异常明细（供前端「查看报错」展示）
TASK_ERRORS = []

# 当前正在处理的商品，以及最近一条型号异常日志（用于给“无明确 publish_err”的失败品补充具体原因）
CURRENT_PRODUCT = None
LAST_MM_LOG = None


def emit_errors():
    emit({"t": "errors", "errors": list(TASK_ERRORS), "count": len(TASK_ERRORS)})


def _infer_mm_error_type(msg):
    """从型号异常日志文本推断更具体的 error_type，供总表分类展示。"""
    s = str(msg)
    if "疑似新型号" in s or "平台未收录" in s:
        return "疑似新型号/平台未收录"
    if "自动化操作失败" in s:
        return "自动化操作失败"
    if "型号选择失败" in s:
        return "型号选择失败"
    if "发布前型号终检未通过" in s or "与预期不匹配" in s:
        return "发布前型号终检未通过"
    if "品牌/型号不完整" in s or "型号=FAIL" in s:
        return "品牌/型号不完整"
    return "型号异常"


def should_stop():
    return os.path.exists(STOP_FILE)


# ========== 导入原脚本并重定向路径 ==========

emit_state("starting", "正在加载脚本模块")

try:
    import test_doudian_batch as dd
    from stability_policy import FailureCircuitBreaker
except Exception as e:
    emit_log("FATAL", "导入 test_doudian_batch 失败: %s" % e, "error")
    emit_state("error", str(e))
    sys.exit(1)

# 关键：把原脚本的全局路径重定向到本任务专属目录，实现多实例互不干扰
dd.CHROME_USER_DATA = PROFILE_DIR
dd.LOG_FILE = PROCESSED_LOG
dd.DEBUG_LOG_FILE = DEBUG_LOG
dd.STORAGE_STATE = os.path.join(os.path.dirname(PROFILE_DIR),
                                "storage_state_%s.json" % TASK_ID)
# 把当前目标店铺短ID写入 dd.CURRENT_SHOP_ID，供 test_doudian_batch 内部按店铺启用的特殊逻辑使用
# （例如 636781 发布前/失败后强制关闭“修改规格信息”开关）。注意 process_single_product 内部的
# parsed 来自商品标题、不含 shop_id，故 dd 必须依赖此全局才能正确识别店铺。
dd.CURRENT_SHOP_ID = SHOP_ID

_orig_log = dd.log


def panel_log(tag, msg):
    """替换原脚本的 log()，在保留原行为的同时向面板推送事件。
    若消息命中型号异常关键词，自动前缀 ❌ [型号异常]，让日志文件和前端看板统一高亮，
    并记录为当前商品的最后一条型号异常日志，用于补充失败原因。"""
    global LAST_MM_LOG
    text = str(msg)
    if _MODEL_MM_RE.search(text):
        LAST_MM_LOG = text
        text = "❌ [型号异常] " + text
    line = "[%s] %s" % (tag, text)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), line))
    except Exception:
        pass
    emit_log(tag, text)


dd.log = panel_log
log = panel_log



# ---- 特殊店铺：免主图特例（子进程级，仅作用于当前店铺，不修改核心脚本、不污染其他店铺）----
_NO_MAIN_APPLIED = False


def apply_no_main_mode(shop_id: str = ""):
    """应用图片函数 monkey-patch，使"无主图时用首张白底图顶替主图位"全局生效。

    现已通用化：所有店铺统一启用免主图逻辑（不再按店铺名单区分）。shop_id 参数保留仅为
    兼容调用点（main() 检测到店铺短ID 后仍调用本函数做幂等补应用），但不再用于决定是否启用。

    规则：
      - 有主图            → 正常用主图，白底图照常传辅助位；
      - 无主图、有白底图  → 主图位用首张白底图顶替，其余白底图传辅助位；
      - 主图、白底图都无  → 判"主图白底图缺失"，跳过该商品（由 classify_image_error 处理）。
    """
    global SHOP_ID, SPECIAL_NO_MAIN, _NO_MAIN_APPLIED
    if shop_id:
        SHOP_ID = shop_id
    SPECIAL_NO_MAIN = str(os.environ.get("DP_ALLOW_WHITE_AS_MAIN", "1")).strip().lower() in (
        "1", "true", "yes", "on"
    )
    if not SPECIAL_NO_MAIN:
        if not _NO_MAIN_APPLIED:
            log("INIT", "主图替代模式已关闭：缺少真实主图的商品将转人工")
            _NO_MAIN_APPLIED = True
        return
    if not _NO_MAIN_APPLIED:
        log("INIT", "🚨 [主图替代模式] 已显式开启：缺少真实主图时将用首张白底图顶替；每次替代都会单独告警")

        _orig_get_main = dd.get_main_image_path
        _orig_get_aux = dd.get_aux_image_paths

        def _no_main_get_main_image_path(parsed):
            """优先用真实主图；缺失时以首张白底图顶替主图位"""
            p = _orig_get_main(parsed)
            if os.path.isfile(p):
                return p
            for a in _orig_get_aux(parsed):
                if os.path.isfile(a):
                    log("IMAGE", "🚨 [主图已被替代] 商品没有真实主图，使用白底图顶替: %s" % a)
                    return a
            return p

        def _no_main_get_aux_image_paths(parsed):
            """主图位已用首张白底图顶替，避免该张在辅助位重复上传"""
            aux = list(_orig_get_aux(parsed))
            main_p = _no_main_get_main_image_path(parsed)
            if main_p and main_p in aux:
                aux = [a for a in aux if a != main_p]
            return aux

        dd.get_main_image_path = _no_main_get_main_image_path
        dd.get_aux_image_paths = _no_main_get_aux_image_paths
        _NO_MAIN_APPLIED = True


# 模块加载时先按环境变量 SHOP_ID 应用一次（若通过 DP_SHOP_ID 显式指定）
apply_no_main_mode(SHOP_ID)


# ========== 登录检测（参照原脚本 main() 的逻辑） ==========

def ensure_login(page):
    log("STEP0", "检查登录状态...")
    try:
        page.goto(DRAFT_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        log("STEP0", "⚠ 草稿页首次导航超时，将按当前页面继续检查登录: %s" % exc)
    time.sleep(3)

    def is_login_page():
        if "login" in page.url or "passport" in page.url:
            return True
        for sel in ['.index_loginPage__K_bGK',
                    'input[placeholder*="手机号"]',
                    'input[placeholder*="验证码"]']:
            try:
                if page.locator(sel).count() > 0:
                    return True
            except Exception:
                pass
        return False

    def is_draft_page():
        try:
            if page.locator('tr.ecom-g-table-row').count() > 0:
                return True
            u = page.url.lower()
            if "draft" in u and "login" not in u and "passport" not in u:
                return True
        except Exception:
            pass
        return False

    # 只有明确进入草稿页才算已登录。空白页、错误页、风控中间页都不能
    # 因为“不像登录页”就被误判为登录成功。
    if is_draft_page() and not is_login_page():
        log("STEP0", "已登录")
        return True

    log("STEP0", "⚠ 尚未进入草稿页，请在弹出的 Chrome 窗口中完成登录（最多等待 180 秒）")
    emit_state("waiting_login", "等待扫码登录")
    for i in range(180):
        if should_stop():
            log("STEP0", "⚠ 登录等待期间收到停止信号")
            return False
        time.sleep(1)
        if is_draft_page() and not is_login_page():
            log("STEP0", "✅ 登录成功")
            emit_state("running", "登录完成")
            return True
        if i % 10 == 0 and i > 0:
            log("STEP0", "等待登录... %ds" % i)
    log("STEP0", "❌ 登录超时")
    return False


# ========== 自有批量循环（含温和停止） ==========

def run_batch_with_stop(page, test_mode, test_n):
    direct_execution = bool(EXECUTION_PLAN_PATH)
    mode_label = (
        "API-E执行计划" if direct_execution
        else (("测试模式（仅%d个）" % test_n) if test_mode else "批量模式")
    )
    log("BATCH", "===== 批量处理开始 [%s] =====" % mode_label)

    target_product_id = str(os.environ.get("DP_TARGET_PRODUCT_ID", "")).strip()
    if direct_execution:
        if target_product_id:
            raise RuntimeError("API-E 执行计划不能与单商品扫描模式同时使用")
        from app.listing_execution import load_execution_plan
        plan = load_execution_plan(EXECUTION_PLAN_PATH, expected_shop_id=SHOP_ID)
        save_only = str(os.environ.get("DP_SAVE_DRAFT_ONLY", "")).strip().lower() in (
            "1", "true", "yes", "on"
        )
        expected_save_only = plan.action == "save_draft"
        if save_only != expected_save_only:
            raise RuntimeError("API-E 执行模式与计划动作不一致")
        all_products = [
            {
                "id": item.product_id,
                "name": item.name,
                "execution_item": item.to_dict(),
            }
            for item in plan.items
        ]
        log(
            "HANDOFF",
            "🔒 API-E 执行计划校验通过: action=%s items=%d checksum=%s；跳过草稿箱扫描"
            % (plan.action, len(all_products), plan.checksum[:12]),
        )
    else:
        # 旧运营模式继续以页面草稿箱为操作依据，保留为 API-E 的一键回退路径。
        # 指定商品回归必须扫描完整草稿箱，否则商品不在前 5 页时会误报未找到。
        scan_limit = None if target_product_id else (5 if test_mode else None)
        all_products = dd.collect_all_draft_ids(page, max_pages=scan_limit)
        if not all_products:
            log("BATCH", "草稿箱中没有找到商品，结束")
            return

        if target_product_id:
            all_products = [p for p in all_products if str(p.get("id", "")) == target_product_id]
            if not all_products:
                log("BATCH", "❌ 草稿箱中未找到指定商品 ID: %s" % target_product_id)
                return
            log("BATCH", "🔒 指定商品测试: %s" % target_product_id)

    log_data = dd.load_processed_log()
    processed = log_data.get("products", {})
    # 只跳过真正处理完成的商品；skipped_no_image 不再永久跳过，
    # 因为用户可能后续补了图片，需要每次重新检测图片是否存在。
    from processed_status_policy import should_skip_in_normal_batch
    # 前端明确指定单商品时视为人工发起复测，不受历史 done 记录影响；
    # 普通测试/批量任务仍继续跳过已完成商品。
    remaining = list(all_products) if (target_product_id or direct_execution) else [
        p for p in all_products
        if p["id"] not in processed
        or not should_skip_in_normal_batch(processed[p["id"]].get("status", ""))
    ]

    if not target_product_id and not direct_execution and os.environ.get("DP_FULL_RETEST") == "1":
        remaining = list(all_products)
        log("BATCH", "全量复测模式：忽略历史完成记录，本轮重新检查全部 %d 件草稿" % len(remaining))

    # 全型号覆盖：历史未成功商品全部重点复测；除此之外，按“品牌+型号”各抽一件，
    # 即使该商品历史已完成也纳入本轮，从而覆盖店铺草稿箱当前所有型号。
    if not target_product_id and not direct_execution and os.environ.get("DP_COVER_ALL_MODELS") == "1":
        priority = list(remaining)
        selected_ids = {str(p.get("id", "")) for p in priority}
        covered_models = set()
        for p in priority:
            parsed = dd.parse_title(p.get("name", ""))
            covered_models.add((parsed.get("brand_cn", "").casefold(),
                                parsed.get("model", "").casefold()))
        representatives = []
        for p in all_products:
            parsed = dd.parse_title(p.get("name", ""))
            key = (parsed.get("brand_cn", "").casefold(), parsed.get("model", "").casefold())
            if not key[1] or key in covered_models or str(p.get("id", "")) in selected_ids:
                continue
            covered_models.add(key)
            selected_ids.add(str(p.get("id", "")))
            representatives.append(p)
        remaining = priority + representatives
        log("BATCH", "全型号覆盖模式：历史待复测 %d 件 + 其余型号代表 %d 件，共覆盖 %d 个品牌型号"
            % (len(priority), len(representatives), len(covered_models)))

    total_all = len(all_products)
    log("BATCH", "总草稿: %d | 已完成: %d | 待处理: %d"
        % (total_all, total_all - len(remaining), len(remaining)))

    if not remaining:
        log("BATCH", "所有商品已处理完毕，无需操作")
        return

    # ---- 图片异常分类（供前端「查看报错」面板展示）----
    TASK_ERRORS.clear()
    ok_products = []
    for p in remaining:
        parsed = dd.parse_title(p["name"])
        # 型号专项测试不上传图片，不能让缺图/目录命名问题挡住型号覆盖率。
        if os.environ.get("WB_TEST_MODEL_ONLY"):
            etype, ereason = "", ""
        else:
            etype, ereason = classify_image_error(parsed)
        if etype:
            TASK_ERRORS.append({
                "product_id": p["id"],
                "name": p["name"],
                "brand": parsed.get("brand_cn", ""),
                "model": parsed.get("model", ""),
                "error_type": etype,
                "reason": ereason,
            })
            dd.mark_product_skipped_no_image(p["id"], p["name"], "图片异常:%s" % etype)
            log("BATCH", "  ⚠ [%s] %s（型号：%s）| 原始名称: %s" % (etype, p["name"], parsed.get("model", ""), repr(p["name"])))
        else:
            ok_products.append(p)

    if TASK_ERRORS:
        log("BATCH", "共发现 %d 条图片异常商品（可在面板点击「查看报错」查看明细）" % len(TASK_ERRORS))
    emit_errors()

    if not ok_products:
        log("BATCH", "❌ 所有待处理商品都存在图片异常（%d 条），无法上传" % len(TASK_ERRORS))
        return
    remaining = ok_products

    if test_mode and not direct_execution:
        log("BATCH", "🔧 测试模式：从 %d 个图片正常的商品中选取前 %d 个..." % (len(remaining), test_n))
        picked = []
        for p in remaining:
            if len(picked) >= test_n:
                break
            picked.append(p)
            log("BATCH", "  ✅ 选中商品: %s" % p["name"])
        if not picked:
            log("BATCH", "❌ 没有可测试上传的商品")
            return
        remaining = picked

    PROGRESS["total"] = len(remaining)
    PROGRESS["processed"] = 0
    PROGRESS["success"] = 0
    PROGRESS["failed"] = 0
    PROGRESS["skipped"] = 0
    emit_progress()

    stopped = False
    breaker = FailureCircuitBreaker(
        window=int(os.environ.get("DP_BREAKER_WINDOW", "20")),
        failure_rate=float(os.environ.get("DP_BREAKER_FAILURE_RATE", "0.40")),
        consecutive_same=int(os.environ.get("DP_BREAKER_CONSECUTIVE", "3")),
    )
    for i, product in enumerate(remaining):
        global CURRENT_PRODUCT, LAST_MM_LOG
        CURRENT_PRODUCT = product
        LAST_MM_LOG = None   # 每个商品开始时重置型号异常日志缓存
        PROGRESS["current"] = product["name"]
        emit_progress()

        log("BATCH", "=" * 50)
        log("BATCH", "[%d/%d] %s (ID:%s)" % (i + 1, len(remaining), product["name"], product["id"]))
        log("BATCH", "=" * 50)
        if direct_execution:
            expected = product["execution_item"]
            log(
                "HANDOFF",
                "计划值: brand=%s model=%s platform_model=%s screen_shape=%s condition=%s"
                % (
                    expected["brand"], expected["model"], expected["platform_model"],
                    expected["screen_shape"], expected["condition"],
                ),
            )

        try:
            success, duration = dd.process_single_product(page, product["id"], product["name"])
        except Exception as e:
            success, duration = False, 0.0
            log("BATCH", "❌ 处理异常: %s" % e)

        if success:
            save_only = str(os.environ.get("DP_SAVE_DRAFT_ONLY", "")).strip().lower() in (
                "1", "true", "yes", "on"
            )
            dd.mark_product_done(product["id"], product["name"], duration,
                                 save_only=save_only)
            PROGRESS["success"] += 1
            decision = breaker.record(True)
            log("BATCH", "✅ 完成 (%.1fs)" % duration)
        else:
            publish_err = dd.get_last_publish_error()
            fail_reason = publish_err or "处理异常"
            manual_required = fail_reason.startswith("需人工处理:")
            if manual_required:
                dd.mark_product_manual(product["id"], product["name"], fail_reason)
            else:
                dd.mark_product_failed(product["id"], product["name"], fail_reason)

            # 将运行时检测到的异常追加到「查看报错」明细
            try:
                parsed_for_err = dd.parse_title(product["name"])
                if publish_err:
                    # 发布商品失败：与图片类异常区分开
                    TASK_ERRORS.append({
                        "product_id": product["id"],
                        "name": product["name"],
                        "brand": parsed_for_err.get("brand_cn", ""),
                        "model": parsed_for_err.get("model", ""),
                        "error_type": "需人工处理" if manual_required else "发布商品失败",
                        "reason": publish_err,
                    })
                    emit_errors()
                elif LAST_MM_LOG:
                    # A runtime brand/model failure is the primary cause. Do
                    # not overwrite it with a secondary missing-image result,
                    # especially in WB_TEST_MODEL_ONLY where images are
                    # intentionally outside the test scope.
                    TASK_ERRORS.append({
                        "product_id": product["id"],
                        "name": product["name"],
                        "brand": parsed_for_err.get("brand_cn", ""),
                        "model": parsed_for_err.get("model", ""),
                        "error_type": _infer_mm_error_type(LAST_MM_LOG),
                        "reason": LAST_MM_LOG,
                    })
                    emit_errors()
                else:
                    etype, ereason = classify_image_error(parsed_for_err)
                    if etype:
                        TASK_ERRORS.append({
                            "product_id": product["id"],
                            "name": product["name"],
                            "brand": parsed_for_err.get("brand_cn", ""),
                            "model": parsed_for_err.get("model", ""),
                            "error_type": etype,
                            "reason": ereason,
                        })
                        emit_errors()
                    else:
                        # 兜底：记录一条通用处理异常，避免失败品在总表不可见
                        TASK_ERRORS.append({
                            "product_id": product["id"],
                            "name": product["name"],
                            "brand": parsed_for_err.get("brand_cn", ""),
                            "model": parsed_for_err.get("model", ""),
                            "error_type": "处理异常",
                            "reason": fail_reason,
                        })
                        emit_errors()
            except Exception:
                pass
            if manual_required:
                PROGRESS["skipped"] += 1
                decision = breaker.record(True)
                log("BATCH", "⏭ 已跳过并转人工，不计入系统失败/熔断: %s" % fail_reason)
            else:
                PROGRESS["failed"] += 1
                log("BATCH", "❌ 失败: %s" % fail_reason)

            # 连续相同的系统性失败说明当前页面结构/规则已变化。继续全量只会批量制造
            # 同类错误；达到3件后自动熔断，保留现场供修复。型号名和商品ID从签名中剔除，
            # 仅按品牌 + 故障阶段聚合。
            parsed_for_breaker = None if manual_required else dd.parse_title(product.get("name", ""))
            if not manual_required and ("品牌" in fail_reason or "型号" in fail_reason or LAST_MM_LOG):
                failure_stage = "品牌型号"
            elif not manual_required and ("主图" in fail_reason or "图片" in fail_reason):
                failure_stage = "图片"
            elif not manual_required and "库存" in fail_reason:
                failure_stage = "库存"
            elif not manual_required and ("保存草稿" in fail_reason or "保存" in fail_reason):
                failure_stage = "保存草稿"
            elif not manual_required and ("发布" in fail_reason or "仍在编辑页" in fail_reason):
                failure_stage = "发布"
            elif not manual_required:
                failure_stage = "处理异常"
            if not manual_required:
                signature = "%s:%s" % (parsed_for_breaker.get("brand_cn", "未知品牌"), failure_stage)
                decision = breaker.record(False, signature)
                log("BATCH", "熔断检查: %s" % (decision.reason or "未触发"))
                if decision.tripped:
                    log("BATCH", "⛔ 自动熔断全量任务: %s" % decision.reason)
                    emit_state("stopping", "失败率异常，已自动暂停：%s" % decision.reason)
                    stopped = True

        PROGRESS["processed"] += 1
        emit_progress()

        if stopped:
            break

        # ★ 温和停止检查点：当前商品已完整处理并保存，此处退出最安全
        if should_stop():
            log("BATCH", "⚠ 收到停止信号，当前商品已处理完毕，停止后续任务")
            emit_state("stopping", "温和停止中")
            stopped = True
            break

        if direct_execution:
            log("HANDOFF", "当前商品结束；下一件将按计划 ID 直接进入编辑页")
        else:
            log("BATCH", "回到草稿箱...")
            try:
                page.goto(DRAFT_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
            except Exception as e:
                log("BATCH", "⚠ 返回草稿箱失败: %s" % e)

    PROGRESS["current"] = ""
    emit_progress()
    log("BATCH", "=" * 50)
    log("BATCH", "===== 处理%s [%s] =====" % ("已停止" if stopped else "完成", mode_label))
    log("BATCH", "成功: %d | 失败: %d" % (PROGRESS["success"], PROGRESS["failed"]))
    log("BATCH", "=" * 50)


# ========== 主流程 ==========

def main():
    from playwright.sync_api import sync_playwright

    test_mode = (MODE == "test")
    os.makedirs(PROFILE_DIR, exist_ok=True)

    log("INIT", "任务 %s 启动 | 模式: %s | 店铺ID: %s"
        % (TASK_ID, MODE, SHOP_ID or "(不切换)"))
    log("INIT", "浏览器数据目录: %s" % PROFILE_DIR)

    with sync_playwright() as p:
        launch_extra = {}
        browser_path = os.environ.get("DP_BROWSER_PATH", "").strip()
        if not browser_path and os.name == "nt":
            for candidate in (
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            ):
                if os.path.isfile(candidate):
                    browser_path = candidate
                    break
        if browser_path:
            launch_extra["executable_path"] = browser_path
            log("INIT", "复用系统浏览器: %s" % browser_path)
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            viewport={"width": 1440, "height": 900},
            no_viewport=False,
            **launch_extra,
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            if not ensure_login(page):
                emit_state("error", "登录失败或被停止")
                return

            emit_state("running", "已登录")

            # ========== 登录后店铺一致性核对 ==========
            # 已登录店铺(页面检测) 与 所选上架店铺(SHOP_ID) 对比：
            #   一致  → 不切换店铺，核对名字+ID无误后直接进入草稿箱
            #   不一致 → 自动切换，切换后再次核对
            #   未指定 SHOP_ID → 仅核对当前店铺身份，不切换
            # 复用 test_doudian_batch 的 _verify_shop_identity 做名字<->ID 自洽核对
            if SHOP_ID:
                # 先检测当前已登录店铺，再决定是否切换（已一致则不切换）
                try:
                    cur_info = dd.detect_shop_info(page)
                    cur_id = cur_info.get("shop_id", "")
                    cur_name = cur_info.get("shop_name", "")
                except Exception as e:
                    cur_id, cur_name = "", ""
                    log("SHOP", "⚠ 登录后店铺检测失败: %s" % e)

                if cur_id == SHOP_ID:
                    # 已登录店铺与目标一致 → 不切换，仅核对身份
                    ok, msg = dd._verify_shop_identity(cur_name, cur_id)
                    if ok:
                        log("SHOP", "✓ 已登录店铺与目标一致（%s ID:%s），无需切换，"
                                    "核对名字/ID无误，直接进入草稿箱" % (cur_name, cur_id))
                    else:
                        log("SHOP", "⚠ 店铺身份核对未通过: %s，仍不切换（后续或受影响）" % msg)
                    emit({"t": "shop", "name": cur_name, "id": cur_id})
                else:
                    # 不一致 → 切换，切换后重新检测并核对
                    shop_name = ""
                    for name, sid in dd.SHOP_MAPPING.items():
                        if sid == SHOP_ID:
                            shop_name = name
                            break
                    log("SHOP", "当前店铺 '%s'(ID:%s) 与目标 '%s'(ID:%s) 不一致，准备切换..."
                        % (cur_name, cur_id, shop_name or "未知", SHOP_ID))
                    try:
                        ok = dd.switch_shop(page, SHOP_ID)
                        if not ok:
                            log("SHOP", "❌ 店铺切换未确认，为防止串店上架，任务立即停止")
                            emit_state("error", "店铺切换失败，请手动切换到目标店铺后重试")
                            return
                    except Exception as e:
                        log("SHOP", "❌ 店铺切换异常，为防止串店上架，任务停止: %s" % e)
                        emit_state("error", "店铺切换异常")
                        return
                    try:
                        after = dd.detect_shop_info(page)
                        after_id = after.get("shop_id", "")
                        after_name = after.get("shop_name", "")
                        vok, vmsg = dd._verify_shop_identity(after_name, after_id)
                        if vok and after_id == SHOP_ID:
                            log("SHOP", "✓ 切换后店铺身份核对无误（%s ID:%s）"
                                % (after_name, after_id))
                        else:
                            log("SHOP", "❌ 切换后仍不是目标店铺（实际:%s 目标:%s），任务停止: %s"
                                % (after_id, SHOP_ID, vmsg))
                            emit_state("error", "切换后店铺身份不一致")
                            return
                        emit({"t": "shop", "name": after_name, "id": after_id})
                    except Exception as e:
                        log("SHOP", "⚠ 切换后店铺检测失败: %s" % e)
                        emit({"t": "shop", "name": shop_name, "id": SHOP_ID})
            else:
                # 未指定目标店铺 → 仅核对当前店铺身份，不切换
                try:
                    cur_info = dd.detect_shop_info(page)
                    cur_id = cur_info.get("shop_id", "")
                    cur_name = cur_info.get("shop_name", "")
                except Exception:
                    cur_id, cur_name = "", ""
                ok, msg = dd._verify_shop_identity(cur_name, cur_id)
                if ok:
                    log("SHOP", "✓ 当前店铺身份核对无误（%s ID:%s），直接扫描草稿箱"
                        % (cur_name, cur_id))
                else:
                    log("SHOP", "⚠ 未指定目标店铺，且店铺身份核对未通过: %s（将尝试继续）" % msg)

            # 检测当前店铺
            try:
                info = dd.detect_shop_info(page)
                if info and info.get("shop_name"):
                    emit({"t": "shop", "name": info.get("shop_name", ""),
                          "id": info.get("shop_id", "")})
                    # ★ 关键：把 detect_shop 返回的 image_dir 写回原脚本的 IMAGE_BASE_DIR
                    # 否则 get_image_dir / get_main_image_path 会用空字符串作为基准路径，导致全部商品报“无主图”
                    dd.IMAGE_BASE_DIR = resolve_shop_image_base(info.get("image_dir", ""))
                    detected_id = info.get("shop_id", "")
                    if SHOP_ID and detected_id != SHOP_ID:
                        log("SHOP", "❌ 最终店铺安全校验失败（实际:%s 目标:%s），拒绝执行"
                            % (detected_id, SHOP_ID))
                        emit_state("error", "当前店铺与目标店铺不一致")
                        return
                    # ★ 同步店铺短ID到 dd.CURRENT_SHOP_ID，供 test_doudian_batch 按店铺特殊逻辑使用
                    dd.CURRENT_SHOP_ID = detected_id or SHOP_ID
                    # ★ 把检测到的店铺短ID 写回 SHOP_ID，并幂等补应用免主图特例
                    # （免主图现已全局生效，此处调用仅为兼容与幂等，不影响其他逻辑）。
                    if detected_id:
                        apply_no_main_mode(detected_id)
                    if dd.IMAGE_BASE_DIR:
                        log("SHOP", "✓ IMAGE_BASE_DIR 已设置: %s" % dd.IMAGE_BASE_DIR)
                    else:
                        dd.IMAGE_BASE_DIR = os.path.join(
                            getattr(dd, 'PRODUCT_IMAGE_ROOT', '/Users/atrenew/Desktop/商品图片'),
                            SHOP_ID) if SHOP_ID else getattr(
                                dd, 'PRODUCT_IMAGE_ROOT', '/Users/atrenew/Desktop/商品图片')
                        log("SHOP", "⚠ 未识别到图片目录，使用默认: %s" % dd.IMAGE_BASE_DIR)
            except Exception as e:
                log("SHOP", "⚠ 店铺检测失败: %s" % e)

            if should_stop():
                log("BATCH", "⚠ 任务开始前收到停止信号，退出")
                emit_state("stopped", "已停止")
                return

            run_batch_with_stop(page, test_mode, TEST_N)
            emit_state("stopped" if should_stop() else "done", "任务结束")

        finally:
            try:
                context.close()
            except Exception:
                pass
            log("INIT", "浏览器已关闭")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        emit_state("stopped", "手动中断")
    except Exception as e:
        emit_log("FATAL", "任务异常终止: %s" % e, "error")
        try:
            emit_log("FATAL", traceback.format_exc(), "error")
        except Exception:
            pass
        emit_state("error", str(e))
        sys.exit(1)

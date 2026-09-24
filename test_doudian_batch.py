#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖店草稿箱批量处理脚本 - 通用版 v2

功能：
  1. 扫描草稿箱所有分页，提取商品 ID 列表
  2. 加载已处理日志，自动跳过已完成的商品
  3. 逐个进入商品编辑页，执行完整编辑流程
  4. 处理完成后记录日志，支持中断恢复

完整流程：
  - 标题规范化 ✓
  - 品牌型号选择 ✓
  - 删除原有图片 ✓
  - 删除原有商详图 + 上传新商详图（14张）✓
  - 上传主图 + 白底图 ✓
  - 价格库存 → 规格开关 → 发布商品（失败则兜底保存草稿）✓

测试模式：
  python test_doudian_batch.py --test
  仅处理1个未完成商品，验证图片上传功能

批量模式：
  python test_doudian_batch.py
  处理所有未完成商品
"""

import time
import re
import os
import sys
import json
import glob
import hashlib
import argparse
import struct
import tempfile
import unicodedata
from datetime import datetime
from playwright.sync_api import sync_playwright
from brand_model_rules import (
    image_directory_allowed,
    image_leaf_allowed,
    image_model_leaf_allowed,
    model_exact_overrides,
    platform_brand_for_title,
    platform_brand_map,
)
from upload_semantic_policy import (authoritative_upload_verdict,
                                    causal_main_present, is_required_multiselect_error,
                                    exact_main_slot_binding, missing_aux_indices,
                                    needs_filechooser_fallback,
                                    semantic_aux_preview_present, session_uploads_present,
                                    strict_main_present, unique_main_candidate)
from model_selection_policy import retry_same_keyword_after_modal

# ========== 配置 ==========
# 工作目录（脚本所在目录）
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
CHROME_USER_DATA = os.path.join(WORK_DIR, "chrome_user_data")
STORAGE_STATE = os.path.join(WORK_DIR, "storage_state.json")
LOG_FILE = os.path.join(WORK_DIR, "processed_products.json")

# 最近一次发布商品失败的错误信息（process_single_product 成功后会被清空）
LAST_PUBLISH_ERROR = None


def get_last_publish_error() -> str:
    """获取最近一次发布商品失败的错误信息。"""
    return LAST_PUBLISH_ERROR


def set_last_publish_error(reason: str) -> None:
    """设置最近一次发布商品失败的错误信息。"""
    global LAST_PUBLISH_ERROR
    LAST_PUBLISH_ERROR = reason


def clear_last_publish_error() -> None:
    """清空发布商品失败的错误信息。"""
    global LAST_PUBLISH_ERROR
    LAST_PUBLISH_ERROR = None

# 草稿箱 URL（不带 product_id，通用入口）
DRAFT_URL = "https://fxg.jinritemai.com/ffa/g/draft"

# 品牌映射表
BRAND_MAP = platform_brand_map()

# 型号强制映射只从 brand_model_rules.json 加载，代码中不再维护第二份表。
MODEL_EXACT_OVERRIDE = model_exact_overrides()


def _resolve_model_override(model: str, brand_cn: str, brand_en: str):
    """按型号强制映射表解析下拉精确选项，兼容三种写法：

    1) 原值（可能带品牌前缀，如 '华为 nova 14 (活力版)'）
    2) 去品牌前缀（如 'nova 14 (活力版)'）
    3) 再去括号符号、保留括注内容（如 'nova 14 活力版'）

    这样标题/解析带前缀+括号、图片目录/用户输入无前缀无括号，都能命中同一映射。
    命中返回 (override_key, exact_value)，未命中返回 (None, None)。
    """
    if not model:
        return None, None
    cands = []
    for s in (model, _strip_brand_tokens(model, brand_cn, brand_en)):
        if s and s not in cands:
            cands.append(s)
        # 仅去括号符号、保留括注内容（绝不连内容一起删），兼容"活力版"有无括号两种写法
        d = s.replace('(', '').replace(')', '').replace('（', '').replace('）', '')
        if d and d not in cands:
            cands.append(d)
    for c in cands:
        if c in MODEL_EXACT_OVERRIDE:
            return c, MODEL_EXACT_OVERRIDE[c]
    return None, None

# 本地商品图片根目录（所有店铺图片的父目录）
PRODUCT_IMAGE_ROOT = os.environ.get("DP_PRODUCT_IMAGE_ROOT", "/Users/atrenew/商品图片")
# 当前店铺的图片根目录（主图 + 白底图）— 登录后根据店铺ID动态设置
IMAGE_BASE_DIR = ""
# 当前店铺短ID（由 panel_runner 在运行时写入 dd.CURRENT_SHOP_ID，用于按店铺启用的特殊逻辑，
# 例如 636781 的“发布前/失败后强制关闭修改规格信息开关”。process_single_product 内部的 parsed
# 来自商品标题、不含 shop_id，故不能依赖 parsed["shop_id"]，必须以本全局为准）。
CURRENT_SHOP_ID = ""
_API_MODEL_PROPERTY_CACHE = {}
_STANDARD_MODEL_CATALOGUE = None
_STANDARD_MODEL_CATALOGUE_MTIME = None
_DOUDIAN_EXPORT_CATALOGUE = None
_DOUDIAN_EXPORT_CATALOGUE_MTIME = None
_BROWSER_CLOUD_MATERIAL_STAGER = None
_BROWSER_IMAGE_DIR_CACHE = {}


def resolve_standard_model(parsed: dict):
    """Resolve parsed title to one Excel-authoritative model without fuzzy matching."""
    global _STANDARD_MODEL_CATALOGUE, _STANDARD_MODEL_CATALOGUE_MTIME
    path = os.environ.get(
        "DP_MODEL_CATALOGUE",
        os.path.join(WORK_DIR, "panel_data", "gaia_excel_phone_models.json"),
    )
    if not os.path.isfile(path):
        return None, "标准型号库不存在"
    try:
        mtime = os.path.getmtime(path)
        if _STANDARD_MODEL_CATALOGUE is None or _STANDARD_MODEL_CATALOGUE_MTIME != mtime:
            from model_catalogue import ModelCatalogue
            _STANDARD_MODEL_CATALOGUE = ModelCatalogue.from_json(path)
            _STANDARD_MODEL_CATALOGUE_MTIME = mtime
        hit = _STANDARD_MODEL_CATALOGUE.resolve(
            parsed.get("brand_cn") or parsed.get("brand_normalized"),
            parsed.get("model"),
        )
        return (dict(hit), "") if hit else (None, "标准型号库无唯一精确匹配")
    except Exception as exc:
        return None, "标准型号库读取失败: %s" % exc


def resolve_doudian_export_model(parsed: dict):
    """Resolve the platform spelling collected from DouDian attribute-detail exports."""
    global _DOUDIAN_EXPORT_CATALOGUE, _DOUDIAN_EXPORT_CATALOGUE_MTIME
    path = os.environ.get(
        "DP_DOUDIAN_MODEL_CATALOGUE",
        os.path.join(WORK_DIR, "panel_data", "doudian_phone_models.json"),
    )
    if not os.path.isfile(path):
        return None, "抖店属性型号库不存在"
    try:
        mtime = os.path.getmtime(path)
        if _DOUDIAN_EXPORT_CATALOGUE is None or _DOUDIAN_EXPORT_CATALOGUE_MTIME != mtime:
            from model_catalogue import ModelCatalogue
            _DOUDIAN_EXPORT_CATALOGUE = ModelCatalogue.from_json(path)
            _DOUDIAN_EXPORT_CATALOGUE_MTIME = mtime
        hit = _DOUDIAN_EXPORT_CATALOGUE.resolve(
            parsed.get("brand_cn") or parsed.get("brand_normalized"), parsed.get("model")
        )
        return (dict(hit), "") if hit else (None, "抖店属性型号库无唯一精确匹配")
    except Exception as exc:
        return None, "抖店属性型号库读取失败: %s" % exc


def resolve_official_model_via_api(parsed: dict):
    """Read-only resolution to one current official DouDian model option."""
    if str(os.environ.get("DOUDIAN_ORDER_API_ENABLED", "")).lower() not in ("1", "true", "yes"):
        return None, "API 型号解析未启用"
    shop_id = str(CURRENT_SHOP_ID or "").strip()
    model = str(parsed.get("model") or "").strip()
    if not shop_id or not model:
        return None, "缺少店铺或型号"
    try:
        from doudian_api import (
            DoudianClient, DoudianConfig, MODEL_PROPERTY_ID,
            find_property, resolve_model_option,
        )
        prop = _API_MODEL_PROPERTY_CACHE.get(shop_id)
        if prop is None:
            # The panel can stay alive while the token store atomically rotates
            # env.local.  Reading os.environ here would inherit the token value
            # from panel startup and can therefore be stale for every child task.
            # Load the credential file at the moment of the API call instead.
            env_file = str(os.environ.get("DP_ENV_FILE") or
                           os.path.join(WORK_DIR, "env.local")).strip()
            config = (DoudianConfig.from_env_file(env_file)
                      if os.path.isfile(env_file)
                      else DoudianConfig.from_mapping(os.environ))
            client = DoudianClient(config, shop_id)
            prop = find_property(client.get_category_properties(), MODEL_PROPERTY_ID) or {}
            _API_MODEL_PROPERTY_CACHE[shop_id] = prop
        brand_normalized = str(parsed.get("brand_normalized") or "")
        brand_en = brand_normalized.split("/")[0] if brand_normalized else ""
        brands = tuple(x for x in (parsed.get("brand_cn"), brand_en) if x)
        hit = resolve_model_option(prop, model, brand_names=brands)
        if not hit:
            return None, "官方型号无唯一匹配"
        return {"name": str(hit.get("name") or ""), "value": hit.get("value")}, ""
    except Exception as exc:
        return None, "API 型号解析失败: %s" % exc
# 商详图片目录（成色图 + 13张通用图 = 共14张，所有店铺共享）
DETAIL_IMAGE_COUNT = 14
def _default_detail_image_dir() -> str:
    """兼容运营约定的新旧目录名以及 Windows/macOS 专用上架机。"""
    configured = os.environ.get("DP_DETAIL_IMAGE_DIR", "").strip()
    if configured:
        return configured
    candidates = []
    if os.name == "nt":
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        candidates.extend([
            os.path.join(desktop, "手机详情"),
            os.path.join(desktop, "手机商详"),
        ])
    candidates.extend(["/Users/atrenew/手机详情", "/Users/atrenew/手机商详"])
    return next((path for path in candidates if os.path.isdir(path)), candidates[-1])


DETAIL_IMAGE_DIR = _default_detail_image_dir()
DETAIL_VERSION_DIR = os.environ.get(
    "DP_DETAIL_VERSION_DIR",
    os.path.join(WORK_DIR, "panel_data", "detail_image_versions"),
)

# 店铺名称 → 短ID 映射表（用于登录后识别店铺并对应本地图片目录）
# 不同店铺的主图不同，白底图和商详图所有店铺共享
SHOP_MAPPING = {
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

# 品类词过滤模式（移除标题中的品类词，避免被包含在型号中）
CATEGORY_PATTERNS = [
    r'\s*二手手机\s*', r'\s*智能手机\s*',
    r'\s*智能机\s*', r'\s*折叠屏\s*', r'\s*折叠手机\s*', r'\s*商务机\s*',
    # “智能大屏”是营销描述，不属于官方型号。智能被单独清理后
    # 不能让残留的“大屏”进入型号与图库目录匹配。
    r'\s*大屏\s*',
    # "二手手" 是抖店标题截断的结果（如 "二手手机" 被截成 "二手手"），
    # 需同时匹配 "二手" 和 "二手手"，避免残留的 "手" 漏入型号
    r'\s*二手(?:手)?\s*',
    r'(?<!\S)手机(?!\S)', r'\s*智能\s*',
    # "系列" 是品类描述词（如 "nova 14 系列"），非型号一部分
    r'\s*系列\s*',
    # 尾部孤立的 "二" 是 "二手" 截断残留（标题被截成 "二手" 仅剩 "二"）
    # 使用 $ 锚定尾部避免误伤型号中的 "二代"（二代是连在一起的两个字）
    r'\s+二\s*$',
]

# 搜索关键词过滤模式（这些词用于标题增加曝光率，不属于手机型号）
# 王师傅说明：全网通、国行、北斗卫星定位等是搜索关键词，不是手机品类
SEO_KEYWORD_PATTERNS = [
    r'\s*全网通\s*', r'\s*国行\s*', r'\s*北斗卫星定位\s*',
    r'\s*双卡双待\s*', r'\s*移动联通电信\s*',
    r'\s*港版\s*', r'\s*美版\s*', r'\s*日版\s*',
    r'\s*韩版\s*', r'\s*欧版\s*',
    r'\s*全新\s*', r'\s*未拆封\s*', r'\s*未激活\s*',
    r'\s*包邮\s*', r'\s*现货\s*',
    r'\s*正品\s*', r'\s*原装\s*', r'\s*全国联保\s*',
    # 商品卖点/材质/系统描述，不属于官方型号。
    r'\s*昆仑玻璃\s*', r'\s*严选优品\s*',
    r'\s*小米澎湃\s*OS\s*', r'\s*澎湃\s*OS\s*',
    r'\s*骁龙\s*8s\s*',
    r'\s*高容\s*', r'\s*光影猎人\s*', r'\s*蔡司潜望长焦\s*',
    # “二手手机”被平台截断后偶尔只剩尾部“手”。
    r'\s+手\s*$',
    # 内部渠道尾标，仅在尾部删除，避免伤及型号中的连字符。
    r'\s*(?:机\s*)?-Z\s*$', r'\s*(?:机\s*)?-z\s*$',
    # 网络制式关键词（5G/4G 等常出现在标题中但不是型号的一部分）
    # 使用 \b 词边界替代 \s*，避免误删括号内的 (5G版)/(4G版) 等型号标识
    r'\b5G\b', r'\b5g\b', r'\b4G\b', r'\b4g\b',
    r'\bLTE\b', r'\blte\b', r'\bWiFi\b', r'\bWi-Fi\b',
    r'\bNFC\b', r'\bnfc\b',
    # "全网" 是 "全网通" 的简写，出现在标题末尾（如 "国行 全网"）
    # 也覆盖卖家常写的 "全网二/全网三/全网四"（双网/三网/四网通），
    # 用 [二三四]? 适配 CJK 后缀——注意 \b 词边界在「网」与「二」之间不生效，
    # 故不能用 \b，必须用显式可选后缀。
    r'\s*全网[二三四]?\s*',
]


def _strip_title_noise(value: str) -> str:
    """反复移除品类/营销词，避免相邻词因规则顺序产生残留。"""
    cleaned = value
    while True:
        previous = cleaned
        for pattern in CATEGORY_PATTERNS + SEO_KEYWORD_PATTERNS:
            cleaned = re.sub(pattern, ' ', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        if cleaned == previous.strip():
            return cleaned

# ★ 特定 5G 机型保护列表
# 这些机型的 "5G" 后缀是型号名不可分割的一部分（非独立的网络制式关键词），
# 当 SEO_KEYWORD_PATTERNS 中的 \b5G\b 将其错误剥离后，需从原始标题中识别并恢复。
# 注意：此列表不做通用化处理，仅覆盖本地店铺目录下确需保护的 5G 机型。
# 生成来源：对 /Users/atrenew/商品图片/25595926/3C数码及配件/二手手机 遍历后，
#          与抖音官方货品维度命名表交叉核对。
_5G_MODEL_PROTECT = [
    (re.compile(r'\bOPPO\s+A3\s*\(?5G\)?\b', re.I), 'OPPO A3 (5G)'),
    (re.compile(r'\bREDMI\s+15R\s+5G\b', re.I),      'REDMI 15R 5G'),
    (re.compile(r'\bRedmi\s+14R\s+5G\b', re.I),      'Redmi 14R 5G'),
    (re.compile(r'\bRedmi\s+13C\s+5G\b', re.I),       'Redmi 13C 5G'),
    (re.compile(r'\bGalaxy\s+A53\s+5G\b', re.I),       'Galaxy A53 5G'),
    (re.compile(r'\bGalaxy\s+A54\s+5G\b', re.I),       'Galaxy A54 5G'),
    (re.compile(r'\bGalaxy\s+A55\s+5G\b', re.I),       'Galaxy A55 5G'),
]


# 日志文件路径（用于调试时查看完整日志）
DEBUG_LOG_FILE = os.path.join(WORK_DIR, "debug_run.log")

def log(tag, msg):
    line = f"[{tag}] {msg}"
    print(line, flush=True)
    # 同时写入日志文件
    try:
        with open(DEBUG_LOG_FILE, 'a', encoding='utf-8') as f:
            from datetime import datetime
            f.write(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} {line}\n")
            f.flush()
    except Exception:
        pass


# ========== 诊断快照（默认关闭，仅 DP_SNAP_DIR 设置时启用） ==========
# 用途：在型号选择流程的关键节点抓取编辑页截图，配合日志定位"型号没真选上/选错"问题。
_SNAP_COUNTER = {}

def snap(page, name: str):
    """抓取当前编辑页截图到 DP_SNAP_DIR（未设置该环境变量时为空操作，零开销）。

    name 形如 'model_opened' / 'model_input' / 'model_selected'，多次同名自动编号。
    """
    import os as _os
    snap_dir = _os.environ.get("DP_SNAP_DIR", "")
    if not snap_dir:
        return
    try:
        _os.makedirs(snap_dir, exist_ok=True)
        _SNAP_COUNTER[name] = _SNAP_COUNTER.get(name, 0) + 1
        path = _os.path.join(snap_dir, f"{name}_{_SNAP_COUNTER[name]}.png")
        page.screenshot(path=path)
    except Exception:
        pass


# ========== 图片路径构建 ==========

def _get_local_image_dir(parsed: dict) -> str:
    """
    根据品牌和型号构建图片根目录路径。
    支持两种目录结构：
      1) 旧结构: IMAGE_BASE_DIR / {品牌} {型号}
      2) 新结构: IMAGE_BASE_DIR / {品牌子文件夹} / {品牌} {型号}
    本地文件夹命名不统一（如 "REDMI K80" / "Redmi 10X" / "红米 K90"），
    因此尝试多种品牌名前缀进行匹配。
    """
    brand_cn = parsed.get("brand_cn", "")
    brand_normalized = parsed.get("brand_normalized", "")
    model = parsed.get("model", "")

    # Search from the shop root at any hierarchy depth. Duplicates are surfaced
    # instead of silently choosing one member's folder.
    try:
        from image_catalogue import find_model_image_dirs
        recursive_matches = find_model_image_dirs(IMAGE_BASE_DIR, model)
        root_abs = os.path.abspath(IMAGE_BASE_DIR)
        bounded_matches = []
        for match in recursive_matches:
            rel = os.path.relpath(os.path.abspath(match), root_abs)
            top_level = rel.split(os.sep, 1)[0] if rel not in (".", "") else ""
            brand_parent = os.path.basename(os.path.dirname(os.path.abspath(match)))
            # 新目录结构必须遵守品牌边界；旧根目录叶子由下方精确名称逻辑处理。
            leaf_name = os.path.basename(match)
            if (((os.sep not in rel and image_leaf_allowed(brand_cn, top_level)) or
                    # 有些店铺在品牌目录前多一层分类。型号的直接
                    # 父目录仍必须是正确品牌，所以不会跨品牌借图。
                    (os.sep in rel and image_directory_allowed(brand_cn, brand_parent))) and
                    image_model_leaf_allowed(brand_cn, leaf_name)):
                bounded_matches.append(match)
        recursive_matches = bounded_matches
        if len(recursive_matches) == 1:
            return recursive_matches[0]
        if len(recursive_matches) > 1:
            log("IMAGE", "同一型号存在多个图片目录，拒绝自动选择: %s" % recursive_matches)
            return os.path.join(IMAGE_BASE_DIR, "__AMBIGUOUS__", f"{brand_cn} {model}")
    except Exception as exc:
        log("IMAGE", "递归图片索引失败，回退旧查找逻辑: %s" % exc)

    # 构建候选品牌前缀（中文 + 英文 + 大写英文）
    candidate_prefixes = []
    if brand_cn:
        candidate_prefixes.append(brand_cn)
    if brand_normalized and "/" in brand_normalized:
        en_name = brand_normalized.split("/")[0]
        if en_name and en_name not in candidate_prefixes:
            candidate_prefixes.append(en_name)
            candidate_prefixes.append(en_name.upper())
            candidate_prefixes.append(en_name.lower())

    # 扩展品牌别名（处理文件夹名前缀与解析品牌不一致的情况）
    # 例: brand_cn="苹果" 但文件夹前缀是 "iPhone"; brand_cn="黑鲨" 但前缀是 "黑鲨游戏手机"
    #     brand_cn="真我" 但文件夹前缀是 "真我GT"; brand_cn="荣耀" 但前缀是 "荣耀GT"/"荣耀Magic"
    # Redmi 是独立品牌，禁止把 Redmi/红米作为小米图片目录别名。
    EXTRA_BRAND_PREFIXES = {
        "苹果": ["iPhone", "IPHONE", "iphone"],
        "黑鲨": ["黑鲨游戏手机"],
        "真我": ["真我GT"],
        "荣耀": ["荣耀GT", "荣耀Magic"],
        "华为": ["华为畅享"],
        "三星": ["三星Galaxy"],
        "魅族": ["魅族20", "魅族21"],
    }
    extra = EXTRA_BRAND_PREFIXES.get(brand_cn, [])
    for ep in extra:
        if ep not in candidate_prefixes:
            candidate_prefixes.append(ep)

    # 去重，保留顺序（保留大小写差异，因为文件夹名可能大小写敏感，如 REDMI vs Redmi）
    seen = set()
    unique_prefixes = []
    for p in candidate_prefixes:
        if p not in seen:
            seen.add(p)
            unique_prefixes.append(p)

    # 品牌无关核心归一化所需的品牌 token 集合（全部小写、去空格，含中/英各种写法）。
    # 例: 华为->{"华为","huawei","华为畅享"}；小米->{"小米","xiaomi","红米","redmi",...}
    # 用于把别名里嵌的中文/英文品牌前缀统一剥掉，仅比对机型核心。
    brand_core_tokens = {p.lower().replace(' ', '') for p in unique_prefixes if p.strip()}

    def _norm_core(s: str) -> str:
        """机型核心归一化：小写 + 去空格(缩进) + 剥掉任意品牌写法(中/英)。
        不触碰 Pro/Max/Ultra/+/s/i/z/x/xx版 等机型区分性后缀（它们属于核心内容，原样保留）。
        长品牌 token 优先匹配，避免 "华为畅享" 被 "华为" 截断。"""
        s = s.lower().replace(' ', '')
        for bt in sorted(brand_core_tokens, key=len, reverse=True):
            if bt and s.startswith(bt):
                s = s[len(bt):]
                break
        return s

    def _prefix_match(entry_lower: str, prefix_lower: str) -> bool:
        """检查 entry 是否以 prefix 开头（支持空格或紧接字符边界）"""
        if not entry_lower.startswith(prefix_lower):
            return False
        # 前缀后面必须是空格、结束，或其他分隔符
        tail = entry_lower[len(prefix_lower):]
        return tail == "" or tail[0] in (" ", "-", "_")

    def _find_in_dir(search_dir: str) -> str:
        """在指定目录下查找匹配的机型文件夹"""
        if not os.path.isdir(search_dir):
            return None
        # 精确匹配：{品牌} {型号}
        for prefix in unique_prefixes:
            path = os.path.join(search_dir, f"{prefix} {model}")
            if os.path.isdir(path):
                return path
        # 特殊处理：型号等于品牌名时（如 iQOO 品牌的 iQOO 机型），目录名就是型号本身
        path = os.path.join(search_dir, model)
        if os.path.isdir(path):
            return path
        # 模糊匹配：剥离品牌前缀后，型号部分必须精确匹配（大小写不敏感）
        # 这样 "X50" 不会误匹配到 "X50i"，但 "Redmi K80" 能匹配 "REDMI K80"
        try:
            entries = os.listdir(search_dir)
        except OSError:
            return None

        # 构建型号别名集合，处理本地目录命名与官方/标题命名的常见差异
        model_lower = model.lower().strip()
        model_aliases = {model_lower}
        # 括注归一化：标题常写 "(活力版)"/"(优享版)"，本地文件夹多为 "活力版"/"优享版"（无括号）。
        # 仅去除括号符号、保留括注内容（"(活力版)"→"活力版"），作为额外别名，
        # 使 带括号 ↔ 不带括号 两侧都能互相匹配。
        model_no_parens = (model_lower.replace('(', '').replace(')', '')
                                      .replace('（', '').replace('）', '')).strip()
        if model_no_parens and model_no_parens != model_lower:
            model_aliases.add(model_no_parens)
            model_aliases.add(model_no_parens.replace(' ', ''))

        def _match_alias(alias_lower: str, remaining: str, prefix_lower: str) -> bool:
            """对单个型号别名与目录剩余部分做全量匹配"""
            if remaining.lower() == alias_lower:
                return True
            # 统一核心比较：忽略 大小写 / 空格(缩进) / 中英文品牌写法，仅比对机型核心。
            # 要求核心完全相等，故 Pro/Max/Ultra/+/s/i/z/x/xx版 等区分性后缀不会被吞掉。
            if _norm_core(remaining) == _norm_core(alias_lower):
                return True
            remaining_no_space = remaining.lower().replace(' ', '')
            if alias_lower.startswith(prefix_lower):
                alias_remaining_no_space = alias_lower[len(prefix_lower):].replace(' ', '').strip()
                if remaining_no_space == alias_remaining_no_space:
                    return True
            # Redmi/红米 前缀不一致（支持双重前缀如 "红米 Redmi Note 13"）
            alias_stripped = alias_lower
            changed = True
            while changed:
                changed = False
                for rp in ["redmi ", "redmi", "红米 ", "红米"]:
                    if alias_stripped.startswith(rp):
                        alias_stripped = alias_stripped[len(rp):].strip()
                        changed = True
                        break
            if alias_stripped != alias_lower and remaining.lower() == alias_stripped:
                return True
            if alias_stripped != alias_lower and remaining_no_space == alias_stripped.replace(' ', ''):
                return True
            # Redmi 前缀已剥离后，再去掉括号比较（处理 "REDMI K80 (至尊版)" vs "Redmi K80 至尊版"）
            if alias_stripped != alias_lower:
                alias_stripped_no_parens = alias_stripped.replace('(', '').replace(')', '').replace('（', '').replace('）', '').strip()
                if remaining.lower() == alias_stripped_no_parens:
                    return True
                if remaining_no_space == alias_stripped_no_parens.replace(' ', ''):
                    return True
            # 空格归一化比较（处理 "畅享70S" vs "畅享 70S" 的差异）
            if remaining.lower().replace(' ', '') == alias_lower.replace(' ', ''):
                return True
            # 括号后缀剥离比较（处理 "P50" vs "P50（骁龙版）" 的差异）
            remaining_no_suffix = re.sub(r'\s*[（(][^)）]*[)）]\s*$', '', remaining).strip()
            if remaining_no_suffix.lower() == alias_lower:
                return True
            # 括号剥离 + 空格归一化
            if remaining_no_suffix.lower().replace(' ', '') == alias_lower.replace(' ', ''):
                return True
            # 全局去括号比较（处理 "Magic5 至臻版" vs "Magic5 (至臻版)" 的差异）
            remaining_no_parens = remaining.replace('(', '').replace(')', '').replace('（', '').replace('）', '').strip()
            if remaining_no_parens.lower() == alias_lower:
                return True
            if remaining_no_parens.lower().replace(' ', '') == alias_lower.replace(' ', ''):
                return True
            # 也去掉标题型号(alias)一侧的括号，处理 "REDMI K80 (至尊版)" vs "Redmi K80 至尊版"
            alias_no_parens = alias_lower.replace('(', '').replace(')', '').replace('（', '').replace('）', '').strip()
            if remaining_no_parens.lower() == alias_no_parens:
                return True
            if remaining_no_parens.lower().replace(' ', '') == alias_no_parens.replace(' ', ''):
                return True
            rem_lower = remaining.lower().strip()
            # 正向：目录比标题多尾部修饰词（有空格/括号）
            # 安全约束：extra 必须以中文或括号开头，防止 "Power"→"Power 2"、"A1"→"A1 Pro" 误匹配
            if rem_lower.startswith(alias_lower + ' ') or rem_lower.startswith(alias_lower + '(') or rem_lower.startswith(alias_lower + '（'):
                extra = rem_lower[len(alias_lower):].strip()
                if extra and (extra[0] == '(' or extra[0] == '（' or '\u4e00' <= extra[0] <= '\u9fff'):
                    return True
            # 正向：目录比标题多无空格中文修饰词（如 "保时捷设计"）
            if rem_lower and rem_lower.startswith(alias_lower):
                extra = rem_lower[len(alias_lower):].strip()
                if extra and (extra[0] == '(' or extra[0] == '（' or '\u4e00' <= extra[0] <= '\u9fff'):
                    return True
            # 反方向：entry 作为 model 前缀（如标题多了尾部修饰词）
            # 安全约束：extra 必须以中文或括号开头，防止 "X60"→"X60 GT" 误匹配
            if alias_lower.startswith(rem_lower + ' ') or alias_lower.startswith(rem_lower + '(') or alias_lower.startswith(rem_lower + '（'):
                extra = alias_lower[len(rem_lower):].strip()
                if extra and (extra[0] == '(' or extra[0] == '（' or '\u4e00' <= extra[0] <= '\u9fff'):
                    return True
            # 反方向：标题比目录多无空格中文修饰词
            if rem_lower and alias_lower.startswith(rem_lower):
                extra = alias_lower[len(rem_lower):].strip()
                if extra and (extra[0] == '(' or extra[0] == '（' or '\u4e00' <= extra[0] <= '\u9fff'):
                    return True
            return False

        for entry in entries:
            entry_path = os.path.join(search_dir, entry)
            # 只匹配文件夹，且排除品牌子文件夹本身（避免把品牌目录当成机型）
            if not os.path.isdir(entry_path):
                continue
            entry_lower = entry.lower()
            # 尝试每个品牌前缀：剥离前缀后比较型号
            for prefix in unique_prefixes:
                prefix_lower = prefix.lower()
                if entry_lower.startswith(prefix_lower):
                    remaining = entry[len(prefix):].strip()
                    # 括注归一化：仅去括号符号（保留括注内容），与标题侧归一化别名对称匹配
                    remaining = (remaining.replace('(', '').replace(')', '')
                                          .replace('（', '').replace('）', '')).strip()
                    for alias_lower in model_aliases:
                        if _match_alias(alias_lower, remaining, prefix_lower):
                            return entry_path
        return None

    # 1) 先在根目录查找（兼容旧结构，或个别未归类的机型）
    result = _find_in_dir(IMAGE_BASE_DIR)
    if result:
        return result

    # 2) 再到各品牌子文件夹中查找（新结构）
    if os.path.isdir(IMAGE_BASE_DIR):
        try:
            subdirs = [d for d in os.listdir(IMAGE_BASE_DIR)
                       if os.path.isdir(os.path.join(IMAGE_BASE_DIR, d))]
        except OSError:
            subdirs = []
        for subdir in subdirs:
            brand_subdir = os.path.join(IMAGE_BASE_DIR, subdir)
            result = _find_in_dir(brand_subdir)
            if result:
                return result

    # 本店整套图片缺失时，使用公共白底图库的品牌+型号精确匹配。
    white_library_root = str(os.environ.get("DP_WHITE_IMAGE_LIBRARY", "")).strip()
    if white_library_root and os.path.isdir(white_library_root):
        try:
            from image_catalogue import resolve_white_library_image_dir
            library_dir, library_status, library_candidates = resolve_white_library_image_dir(
                white_library_root, brand_cn or parsed.get("brand_normalized", ""), model
            )
            if library_dir:
                log("IMAGE", f"当前店铺无整套图片，公共白底图库精确命中: {library_dir}")
                return library_dir
            if library_status == "conflicting_sources":
                log("IMAGE", f"公共白底图库同品牌型号存在冲突，转人工: {library_candidates}")
                return os.path.join(IMAGE_BASE_DIR, "__AMBIGUOUS_WHITE_LIBRARY__", f"{brand_cn} {model}")
        except Exception as exc:
            log("IMAGE", f"公共白底图库索引失败，继续按缺图转人工: {exc}")

    # 跨店图片属于最后一级、待业务确认的兜底规则，默认关闭。
    cross_store_enabled = str(os.environ.get("DP_CROSS_STORE_IMAGE_FALLBACK", "")).strip().lower() in (
        "1", "true", "yes", "on"
    )
    if cross_store_enabled:
        try:
            from image_catalogue import resolve_cross_store_image_dir
            shared_dir, shared_status, shared_candidates = resolve_cross_store_image_dir(
                PRODUCT_IMAGE_ROOT, IMAGE_BASE_DIR, model
            )
            if shared_dir:
                log("IMAGE", f"公共白底图库未命中，跨店同型号兜底: {shared_dir}")
                return shared_dir
            if shared_status == "conflicting_sources":
                log("IMAGE", f"跨店同型号存在内容不一致的图片，转人工: {shared_candidates}")
                return os.path.join(IMAGE_BASE_DIR, "__AMBIGUOUS_CROSS_STORE__", f"{brand_cn} {model}")
        except Exception as exc:
            log("IMAGE", f"跨店图片兜底索引失败: {exc}")

    # 所有店铺均未找到，返回默认路径（用于错误提示）
    return os.path.join(IMAGE_BASE_DIR, f"{brand_cn} {model}")


def _cloud_material_brand(parsed: dict) -> str:
    """Return the reviewed platform brand used by both local and cloud rules."""
    brand = str(parsed.get("brand_cn") or "").strip()
    if brand:
        return brand
    normalized = str(parsed.get("brand_normalized") or "").strip()
    if "/" in normalized:
        return normalized.rsplit("/", 1)[-1].strip()
    return normalized


def _get_browser_cloud_stager():
    global _BROWSER_CLOUD_MATERIAL_STAGER
    if _BROWSER_CLOUD_MATERIAL_STAGER is None:
        from app.browser_material_source import BrowserCloudMaterialStager
        _BROWSER_CLOUD_MATERIAL_STAGER = BrowserCloudMaterialStager.from_mapping(os.environ)
    return _BROWSER_CLOUD_MATERIAL_STAGER


def get_image_dir(parsed: dict) -> str:
    """Resolve browser-upload images from Feishu first, then the local gallery.

    The local resolver above remains unchanged and is the explicit operational
    fallback.  A cloud ambiguity, policy failure, missing configuration or
    network error never causes the browser workflow to guess a cloud folder.
    """
    mode = str(os.environ.get("DP_BROWSER_IMAGE_SOURCE") or
               "feishu_local_fallback").strip().casefold()
    if mode in {"local", "local_only"}:
        return _get_local_image_dir(parsed)

    shop_id = str(CURRENT_SHOP_ID or "").strip()
    brand = _cloud_material_brand(parsed)
    model = str(parsed.get("model") or "").strip()
    cache_key = (mode, shop_id, brand, model)
    if cache_key in _BROWSER_IMAGE_DIR_CACHE:
        return _BROWSER_IMAGE_DIR_CACHE[cache_key]

    if shop_id.isdigit() and brand and model:
        try:
            selection = _get_browser_cloud_stager().stage(shop_id, brand, model)
            result = str(selection.directory)
            _BROWSER_IMAGE_DIR_CACHE[cache_key] = result
            plan = selection.plan
            log(
                "IMAGE_SOURCE",
                "云盘主来源命中: 店铺=%s 品牌=%s 型号=%s 目录=%s 解析=%s 主图替代=%s 白底图=%d" % (
                    shop_id, brand, model, plan.resolved_model_folder or model,
                    plan.model_folder_resolution, "是" if plan.main_substituted else "否",
                    len(plan.white_images),
                ),
            )
            return result
        except Exception as exc:
            log(
                "IMAGE_SOURCE",
                "云盘未安全命中，切换本地备选: 店铺=%s 品牌=%s 型号=%s 原因=%s" % (
                    shop_id or "未设置", brand or "未识别", model or "未识别", exc,
                ),
            )
    else:
        log(
            "IMAGE_SOURCE",
            "云盘解析条件不足，切换本地备选: 店铺=%s 品牌=%s 型号=%s" % (
                shop_id or "未设置", brand or "未识别", model or "未识别",
            ),
        )

    result = _get_local_image_dir(parsed)
    _BROWSER_IMAGE_DIR_CACHE[cache_key] = result
    return result


def get_main_image_path(parsed: dict) -> str:
    """获取主图文件路径（支持 .png/.jpg/.jpeg）"""
    img_dir = get_image_dir(parsed)
    main_dir = os.path.join(img_dir, "主图")
    for name in (
        "主图.png", "主图.jpg", "主图.jpeg", "主图.webp",
        "主图.PNG", "主图.JPG", "主图.JPEG", "主图.WEBP",
    ):
        path = os.path.join(main_dir, name)
        if os.path.isfile(path):
            return path
    # 运营图库有些目录以机型名保存主图（如“真我 GT8 Pro.jpg”）。当且
    # 仅当主图目录中只有一张合法图片时可安全采用；多张时拒绝按顺序猜。
    candidates = []
    if os.path.isdir(main_dir):
        for name in os.listdir(main_dir):
            path = os.path.join(main_dir, name)
            if os.path.isfile(path) and os.path.splitext(name)[1].lower() in {
                ".png", ".jpg", ".jpeg", ".webp"
            }:
                candidates.append(path)
    if len(candidates) == 1:
        log("IMAGE", f"主图使用唯一图片文件: {candidates[0]}")
        return candidates[0]
    if len(candidates) > 1:
        log("IMAGE", f"主图目录存在多张非标准命名图片，拒绝自动选择: {candidates}")
    return os.path.join(main_dir, "主图.png")  # 默认返回.png路径（不存在时用于报错）


def get_aux_image_paths(parsed: dict) -> list:
    """获取内容唯一的白底图，按文件名排序（支持 .png/.jpg/.jpeg）。

    抖店不会为内容完全相同的图片生成第二个预览。若只按文件名计数，
    ``白底图_1.jpg``/``白底图_2.jpg`` 即使字节完全相同也会被误算成
    两张，最终进入永远无法成功的补传循环。因此这里在素材入口按内容
    SHA-256 去重，后续动态门槛和上传列表天然使用同一份权威数据。
    """
    img_dir = get_image_dir(parsed)
    aux_dir = os.path.join(img_dir, "白底图")
    if not os.path.isdir(aux_dir):
        return []
    files = []
    for ext in (
        "*.png", "*.jpg", "*.jpeg", "*.webp",
        "*.PNG", "*.JPG", "*.JPEG", "*.WEBP",
    ):
        files.extend(glob.glob(os.path.join(aux_dir, f"白底图_{ext}")))
    files = sorted(set(files))
    unique_files = []
    seen_hashes = {}
    for path in files:
        try:
            digest = hashlib.sha256()
            with open(path, "rb") as image_file:
                for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            key = digest.hexdigest()
        except OSError:
            key = "path:" + os.path.abspath(path)
        if key in seen_hashes:
            log("IMAGE", "跳过内容重复的白底图: %s（与 %s 相同）" %
                (os.path.basename(path), os.path.basename(seen_hashes[key])))
            continue
        seen_hashes[key] = path
        unique_files.append(path)
    return unique_files


def get_detail_image_paths(parsed: dict) -> list:
    """
    获取商详图片路径列表（共14张）。
    成色图（9新/95新/99新）放在第三张位置，其余为通用商详图。

    顺序: 1.jpg, 2.jpg, 成色图, 4.jpg, 5.jpg ... 14.jpg

    参数:
        parsed: parse_title() 返回的解析结果，需包含 condition 字段

    返回:
        14张图片路径的列表
    """
    condition = parsed.get("condition", "")  # 如 "95新", "9新", "99新"
    detail_dir = DETAIL_IMAGE_DIR

    # 1. 收集13张通用商详图（1、2、4...14，3号位由成色图替代）
    expected_numbers = [1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    general_paths = []
    for n in expected_numbers:
        jpg_path = os.path.join(detail_dir, f"{n}.jpg")
        png_path = os.path.join(detail_dir, f"{n}.png")
        if os.path.isfile(jpg_path):
            general_paths.append(jpg_path)
        elif os.path.isfile(png_path):
            general_paths.append(png_path)
        else:
            log("DETAIL_IMG", f"警告: 商详图 {n}.jpg 不存在")

    # 2. 查找成色图
    condition_path = None
    candidates = [
        os.path.join(detail_dir, f"{condition}.jpg"),
        os.path.join(detail_dir, f"{condition}.png"),
        os.path.join(detail_dir, f" {condition}.jpg"),
        os.path.join(detail_dir, f" {condition}.png"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            condition_path = c
            break

    if not condition_path:
        log("DETAIL_IMG", f"警告: 成色图不存在 - {condition}.jpg，跳过成色图")

    # 3. 组装：成色图放在第三张位置（index 2，即 2.jpg 之后、4.jpg 之前）
    paths = []
    inserted_condition = False
    for i, p in enumerate(general_paths):
        # 在第3个位置（i==2，即 2.jpg 之后）插入成色图
        if i == 2 and condition_path:
            paths.append(condition_path)
            inserted_condition = True
        paths.append(p)

    # 如果成色图未插入（通用图不足3张等边界情况），追加到末尾
    if condition_path and not inserted_condition:
        paths.append(condition_path)

    log("DETAIL_IMG", f"成色: {condition}, 商详图数量: {len(paths)} (预期{DETAIL_IMAGE_COUNT}张)")
    return paths


def calculate_detail_image_version(paths: list) -> str:
    """Return a stable content fingerprint for one complete detail-image set."""
    if len(paths) != DETAIL_IMAGE_COUNT:
        raise ValueError(f"商详图片不完整: 实际{len(paths)}张，预期{DETAIL_IMAGE_COUNT}张")
    digest = hashlib.sha256()
    for path in paths:
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        digest.update(os.path.basename(path).encode("utf-8"))
        digest.update(b"\0")
        with open(path, "rb") as image_file:
            for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _detail_version_path(shop_id: str, product_id: str) -> str:
    safe_shop = re.sub(r"[^0-9A-Za-z_-]", "_", str(shop_id or "unknown"))
    safe_product = re.sub(r"[^0-9A-Za-z_-]", "_", str(product_id or "unknown"))
    return os.path.join(DETAIL_VERSION_DIR, safe_shop, safe_product + ".json")


def load_detail_image_version(shop_id: str, product_id: str) -> str:
    path = _detail_version_path(shop_id, product_id)
    try:
        with open(path, "r", encoding="utf-8") as version_file:
            data = json.load(version_file)
        return str(data.get("version") or "")
    except (OSError, ValueError, TypeError):
        return ""


def save_detail_image_version(shop_id: str, product_id: str, version: str) -> None:
    """Record a version only after the product has been published successfully."""
    path = _detail_version_path(shop_id, product_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + f".{os.getpid()}.tmp"
    payload = {
        "shop_id": str(shop_id),
        "product_id": str(product_id),
        "version": str(version),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with open(temp_path, "w", encoding="utf-8") as version_file:
        json.dump(payload, version_file, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def should_refresh_detail_images(shop_id: str, product_id: str,
                                 current_count: int, local_version: str) -> tuple:
    """Decide whether local detail images must replace the images on DouDian."""
    uploaded_version = load_detail_image_version(shop_id, product_id)
    if current_count != DETAIL_IMAGE_COUNT:
        return True, f"页面商详图为{current_count}张，不是{DETAIL_IMAGE_COUNT}张"
    if not uploaded_version:
        return True, "没有该商品的商详图版本记录"
    if uploaded_version != local_version:
        return True, "本地商详图片内容已更新"
    return False, "页面数量和本地图片版本均一致"


# ========== 标题解析 ==========

def _strip_repeated_brand(after_brand: str, cn_name: str, en_name: str) -> str:
    """
    循环剥离 after_brand 开头重复出现的品牌名（中英文、斜杠分隔）。

    抖店编辑页标题经常出现灰框前缀与可编辑区重复拼接的情况，例如：
      "95新 Huawei/华为 【95新】Huawei/华为 华为畅享 70X 二手手机"
    仅剥离一次品牌名无法得到干净型号，需要循环剥离直到前缀不再包含品牌名。
    """
    if not after_brand:
        return after_brand
    changed = True
    while changed:
        changed = False
        if en_name:
            en_lower = en_name.lower()
            ab_lower = after_brand.lower()
            if ab_lower.startswith(en_lower):
                after_brand = re.split(en_name, after_brand, flags=re.I, maxsplit=1)[-1].strip()
                after_brand = after_brand.lstrip('/').strip()
                changed = True
        if cn_name and after_brand.startswith(cn_name):
            after_brand = after_brand.split(cn_name, 1)[-1].strip()
            changed = True
    return after_brand


def _normalize_condition(cond: str) -> str:
    """
    标准化成色字段。

    抖店草稿箱标题命名过长时，方括号内的成色可能被截成 "95"、"99"、"9" 等
    不带"新"字的简写，导致后续按 "95.jpg" 找不到成色图。
    本函数在保持原有标准格式识别的同时，把常见简写数字映射回标准成色词。
    """
    if not cond:
        return ""
    cond = cond.strip()
    # 已经是标准格式，直接返回
    if re.fullmatch(r'(99新|95新|9新|8成新|9成新)', cond):
        return cond
    # 简写数字成色：9/95/99 → 9新/95新/99新
    if re.fullmatch(r'9|95|99', cond):
        return cond + "新"
    return cond


def _cleanup_model(raw_model: str) -> str:
    """
    清理 parse_title 提取的型号字符串中的残留词和空括号。

    常见残留来源：
    - 空括号 "( )" / "（ ）" → SEO 关键词如 (5G) 被剥离后残留
    - 尾部孤立字符 "全"/"国"/"网通" → SEO 关键词如 "国行全" 被剥离后残留
    - 括号内前导空白："(  骁龙版)" → 规范化后保留内容
    - 标题前缀营销词："直播" / "新"（如"直播iPhone 15"、"新iPhone 16"）
    """
    model = raw_model.strip()
    # 1) 去掉空括号/空全角括号以及仅含空白的括号
    model = re.sub(r'\(\s*\)', '', model)
    model = re.sub(r'（\s*）', '', model)
    # 1.5) 去掉标题前缀营销词（不属于型号）
    # 例：直播iPhone 15 Pro Max → iPhone 15 Pro Max；新iPhone 16 Plus → iPhone 16 Plus
    # "新" 仅在后面紧跟字母/数字时剥离，避免误伤如"全新"等词（已被 SEO 关键词提前过滤）
    model = re.sub(r'^(直播|新\s*(?=[A-Za-z0-9]))\s*', '', model)
    # 2) 去掉 SEO 关键词剥离后残留的截断孤儿字符
    model = re.sub(r'\s+[全国网通]通?$', '', model)
    # 3) 清理括号内因 5G/4G 被剥离后留下的前导空白，例如 "(  骁龙版)" → "(骁龙版)"
    model = re.sub(r'\(\s+', '(', model)
    model = re.sub(r'\s+\)', ')', model)
    model = re.sub(r'（\s+', '（', model)
    model = re.sub(r'\s+）', '）', model)
    # 4) 再清理一次多余空格
    model = model.strip()
    # 5) 递归清理（去掉括号后可能暴露出新的孤儿字符）
    if model != raw_model.strip():
        model = re.sub(r'\s+[全国网通]通?$', '', model).strip()
        model = re.sub(r'\(\s*\)', '', model).strip()
        model = re.sub(r'（\s*）', '', model).strip()

    # 6) 规范化无空格型号："90pro" → "90 Pro"，"x60gt" → "X60 GT"
    model = _normalize_model_spacing(model)
    return model


def _normalize_model_spacing(model: str) -> str:
    """
    规范化型号字符串中的空格和大小写（最小侵入式）。

    仅处理抖店草稿箱标题中最常见的两种异常连写：
      - 纯数字 + 英文后缀：90pro → 90 Pro, 300ultra → 300 Ultra
      - 单字母系列前缀 + 数字 + 英文后缀：x60gt → X60 GT, s20pro → S20 Pro

    不触碰已有的正确格式（如 "X60"、"Mate 70"、"Pura 80 Pro" 保持原样）。
    """
    if not model:
        return model

    # 常见后缀映射（小写 -> 规范形式）
    suffix_map = {
        'pro': 'Pro', 'max': 'Max', 'plus': 'Plus', 'ultra': 'Ultra',
        'mini': 'Mini', 'se': 'SE', 'gt': 'GT', 'gt2': 'GT2', 'gt3': 'GT3',
        'gt4': 'GT4', 'gt5': 'GT5', 'gt6': 'GT6', 'gt7': 'GT7',
        'neo': 'NEO', 'rs': 'RS', 'rsr': 'RSR', 'turbo': 'Turbo',
        'fold': 'Fold', 'flip': 'Flip', 'edition': 'Edition',
    }
    suffix_pattern = '|'.join(re.escape(k) for k in suffix_map.keys())

    # 模式1: 纯数字 + 后缀（忽略大小写）
    m = re.match(r'^(\d+)(' + suffix_pattern + r')$', model, flags=re.I)
    if m:
        return f"{m.group(1)} {suffix_map[m.group(2).lower()]}"

    # 模式2: 单字母前缀 + 数字 + 后缀（忽略大小写）
    m = re.match(r'^([a-zA-Z])(\d+)(' + suffix_pattern + r')$', model, flags=re.I)
    if m:
        return f"{m.group(1).upper()}{m.group(2)} {suffix_map[m.group(3).lower()]}"

    return model


def _normalize_model_for_title_compare(value: str) -> str:
    """生成仅供标题终检使用的型号比较键。

    平台枚举、标题和内部型号库对空格/大小写的写法可能不同，例如
    ``12Pro`` 与 ``12 Pro``。这里只消除不承载型号语义的格式差异，
    不修改标题、解析结果或平台下拉框中的真实值。
    """
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s\-_·・]+", "", value)


def _model_appears_in_title(model: str, title: str) -> tuple[bool, str]:
    """按统一格式判断型号是否出现在标题中，并返回匹配方式。"""
    model_key = _normalize_model_for_title_compare(model)
    title_key = _normalize_model_for_title_compare(title)
    if not model_key:
        return True, "empty"
    if model_key in title_key:
        return True, "normalized"

    # 保留原有兼容规则：少数平台标题会省略型号末尾的加号。
    model_no_plus = model_key.rstrip("+")
    if model_no_plus and model_no_plus in title_key and len(model_no_plus) >= len(model_key) - 1:
        return True, "plus_omitted"
    return False, "missing"


def _restore_5g_model(title: str, result: dict) -> None:
    """恢复因 \b5G\b 被错误剥离的特定 5G 机型型号后缀。

    仅匹配 _5G_MODEL_PROTECT 中定义的精确机型，
    在原始标题确认包含 "XXX 5G" 模式且 parse_title 提取的型号恰好缺失 "5G" 时恢复。
    """
    model = result.get("model")
    if not model:
        return
    model_norm = model.strip().lower()
    brand_cn = result.get("brand_cn", "")
    brand_normalized = result.get("brand_normalized", "")
    brand_en = brand_normalized.split("/")[0].lower() if brand_normalized else ""

    for pattern, full_model in _5G_MODEL_PROTECT:
        if not pattern.search(title):
            continue
        # 计算 full_model 去掉 5G 相关部分后的 base
        base_with_brand = re.sub(r'\(?\s*5G\s*\)?', '', full_model, flags=re.I).strip()
        base_with_brand = re.sub(r'\(\s*\)|（\s*）', '', base_with_brand).strip()
        base_with_brand = base_with_brand.lower()

        # 部分 parse_title 分支会把品牌前缀从 model 中剥离（如 OPPO A3 -> A3）
        base_no_brand = base_with_brand
        if brand_en and base_no_brand.startswith(brand_en):
            base_no_brand = base_no_brand[len(brand_en):].strip()
        if brand_cn and base_no_brand.startswith(brand_cn.lower()):
            base_no_brand = base_no_brand[len(brand_cn):].strip()
        # 同时兼容 Redmi/REDMI 这种 model 中保留品牌前缀的情况
        if model_norm in (base_with_brand, base_no_brand):
            result["model"] = full_model
            break


def parse_title(title: str) -> dict:
    """从商品标题解析品牌、型号、成色"""
    # 草稿列表偶发把序号紧贴在品牌前（如“1荣耀 WIN RT”）。
    # 仅在数字后紧跟已知中文品牌时去掉，不会伤及型号数字。
    title = re.sub(
        r'(?<!\S)\d{1,3}(?=(?:荣耀|华为|苹果|小米|红米|三星|真我|一加|魅族|黑鲨))',
        '', str(title or '').strip()
    )
    # 草稿标题偶发在重复的中文品牌前混入单个渠道标记 ``z``，例如
    # ``Apple/苹果 z苹果 iPhone 17 Pro``。只清理“独立 z/Z + 已知中文品牌”
    # 这一种已复现格式；型号本身的 Z 系列（如 vivo Z10）不受影响。
    title = re.sub(
        r'(?<![A-Za-z0-9])z(?=(?:荣耀|华为|苹果|小米|红米|三星|真我|一加|魅族|黑鲨))',
        '', title, flags=re.I,
    )
    result = {"raw": title, "brand_cn": "", "brand_normalized": "", "model": "", "condition": ""}

    # 成色提取：支持三种格式
    # 1. 【95新】/【95】（方括号格式，草稿箱列表标题；容错不带"新"字的简写）
    # 2. --95新-- / --9新-- / --99新--（双横线格式，编辑页灰框前缀）
    # 3. 99新 / 95新 / 9新（无前缀格式，系统灰框 + 编辑页标题）
    m = re.search(r'【([^】]+)】', title)
    if m:
        # 仅当真为成色时才采纳，避免【直播】【24期免息】等营销前缀被误判为成色
        maybe_cond = _normalize_condition(m.group(1))
        if maybe_cond in ("99新", "95新", "9新", "8成新", "9成新"):
            result["condition"] = maybe_cond

    # 若方括号不是成色（如【直播】），继续检查后两种成色格式
    if not result["condition"]:
        m_dash = re.search(r'--\s*(99新|95新|9新|8成新|9成新)\s*--', title)
        if m_dash:
            result["condition"] = _normalize_condition(m_dash.group(1))

    if not result["condition"]:
        # 匹配开头的成色词：9新 / 95新 / 99新 / 8成新 等，容错纯数字简写
        m2 = re.match(r'(99新|95新|9新|99|95|9|8成新|9成新)', title)
        if m2:
            result["condition"] = _normalize_condition(m2.group(1))

    # 平台品牌身份只从统一规则入口读取；旧解析分支仅负责提取型号。
    ruled_brand = platform_brand_for_title(title)

    # 子品牌/系列关键词检测
    # Redmi 在抖店选择独立品牌。Excel 即使归在小米，型号目录层仍会把两者归为同组。
    title_lower = title.lower()
    redmi_keywords = ["红米", "redmi"]
    is_redmi = ruled_brand == "红米"

    if is_redmi:
        cn_name = "红米"
        full_name = BRAND_MAP[cn_name]
        result["brand_cn"] = cn_name
        result["brand_normalized"] = full_name

        # 先去掉成色标记，简化后续处理
        after_brand = re.sub(r'^【[^】]+】', '', title).strip()
        # 也去掉 --xxx新-- 格式的成色标记（头部或尾部）
        after_brand = re.sub(r'^--\s*\d+[新成][新新]?\s*--', '', after_brand).strip()
        after_brand = re.sub(r'--\s*\d+[新成][新新]?\s*--\s*$', '', after_brand).strip()
        # 去掉开头的成色前缀（如 "99新 " / "95新 " / "9新 "）
        after_brand = re.sub(r'^(99新|95新|9新|8成新|9成新)\s*', '', after_brand).strip()

        # 去掉品牌前缀组合（如 "Redmi/红米 " / "Xiaomi/小米 "）
        # 这些是系统灰框前缀中的品牌名，不是型号的一部分
        after_brand = re.sub(r'^(Redmi/红米|REDMI/红米|redmi/红米|Xiaomi/小米|xiaomi/小米)\s*', '', after_brand, flags=re.I).strip()

        # 只去掉开头的父品牌，不能按任意位置 split；否则标题尾部
        # “小米澎湃OS”会把前面的真实 Redmi 型号全部截掉。
        if re.match(r'^(?:Xiaomi/小米|小米)\s*', after_brand, flags=re.I):
            after_brand = re.sub(r'^(?:Xiaomi/小米|小米)\s*', '', after_brand, flags=re.I).strip()
            # 去掉可能残留的品牌前缀（如 "Redmi/红米"）
            after_brand = re.sub(r'^(Redmi/红米|REDMI/红米|redmi/红米)\s*', '', after_brand, flags=re.I).strip()

        # 保留原始 Redmi/REDMI/红米 前缀，不强制标准化
        # get_image_dir 的模糊匹配会处理大小写差异（REDMI vs Redmi vs 红米）
        # 只需清理可能残留的父品牌前缀中的多余空格
        after_brand = after_brand.strip()

        # 型号匹配：保留 Redmi/REDMI/红米 前缀
        # 过滤品类词 + 搜索关键词（全网通、国行等不属于型号）
        after_brand_clean = _strip_title_noise(after_brand)
        model_match = re.match(r'([\u4e00-\u9fa5A-Za-z0-9\s+\-().·／/（）]+)', after_brand_clean)
        if model_match:
            result["model"] = _cleanup_model(model_match.group(1).strip())
            result["model"] = re.sub(r'^红米\s*', 'Redmi ', result["model"], flags=re.I).strip()
        _restore_5g_model(title, result)  # ★ 恢复被 \b5G\b 错误剥离的特定 5G 机型
        return result  # Redmi 命中后直接返回，不再遍历

    for cn_name, full_name in BRAND_MAP.items():
        if cn_name in title:
            result["brand_cn"] = cn_name
            result["brand_normalized"] = full_name

            # 特殊品牌：型号包含品牌名本身（如 麦芒9停用、黑鲨游戏手机，无空格格式）
            if cn_name in ('麦芒', '黑鲨') and not re.search(rf'{cn_name}\s', title):
                after_brand = title  # 无空格格式保留品牌名
            else:
                after_brand = title.split(cn_name, 1)[-1].strip()

            # 先去掉成色标记（如【95新】或 --95新-- 后面的内容），否则灰框会挡住品牌剥离
            # 注意：--xx新-- 可能出现在头部或尾部（编辑页灰框前缀格式）
            after_brand = re.sub(r'^【[^】]+】', '', after_brand).strip()
            after_brand = re.sub(r'^--\s*\d+[新成][新新]?\s*--', '', after_brand).strip()
            after_brand = re.sub(r'--\s*\d+[新成][新新]?\s*--\s*$', '', after_brand).strip()

            # 修复: 循环去掉前缀中重复出现的品牌名（如 "99新 vivo vivo S60" → "S60"）
            # 抖店编辑页经常出现前缀与输入框内容重复拼接，导致品牌名出现多次
            en_name = full_name.split("/")[0]
            after_brand = _strip_repeated_brand(after_brand, cn_name, en_name)

            # 型号匹配：支持字母、数字、空格、+、-、括号以及中文型号词（如"畅享"）
            # 先去掉品类词，避免被包含在型号中
            # 精确移除品类词 + 搜索关键词（全网通、国行等不属于型号）
            after_brand_clean = _strip_title_noise(after_brand)
            # 正则支持中文、英文、数字、空格、+、-、括号、点号、斜杠等常见型号字符
            model_match = re.match(r'([\u4e00-\u9fa5A-Za-z0-9\s+\-().·／/（）]+)', after_brand_clean)
            if model_match:
                result["model"] = _cleanup_model(model_match.group(1).strip())
            break

    if not result["brand_cn"]:
        for cn_name, full_name in BRAND_MAP.items():
            en_name = full_name.split("/")[0]
            if en_name.lower() in title_lower:
                result["brand_cn"] = cn_name
                result["brand_normalized"] = full_name
                after_brand = re.split(en_name, title, flags=re.I)[-1].strip()

                # 先去掉成色标记，再循环剥离重复品牌名
                after_brand = re.sub(r'^【[^】]+】', '', after_brand).strip()
                after_brand = re.sub(r'^--\s*\d+[新成][新新]?\s*--', '', after_brand).strip()
                after_brand = re.sub(r'--\s*\d+[新成][新新]?\s*--\s*$', '', after_brand).strip()
                after_brand = _strip_repeated_brand(after_brand, cn_name, en_name)
                # 精确移除品类词 + 搜索关键词（全网通、国行等不属于型号）
                after_brand_clean = _strip_title_noise(after_brand)
                model_match = re.match(r'([\u4e00-\u9fa5A-Za-z0-9\s+\-().·／/（）]+)', after_brand_clean)
                if model_match:
                    result["model"] = _cleanup_model(model_match.group(1).strip())
                break

    # 品牌推断：标题中未出现显式品牌名时，根据独占型号关键词推断
    # 解决部分抖店标题省略品牌前缀的问题，如 "Galaxy S25+"、"Magic Vs3"、"Mate 60"
    if not result["brand_cn"]:
        title_lower = title.lower()
        inferred_brand = None
        # 按独占性从高到低排列，避免误判
        if "galaxy" in title_lower:
            inferred_brand = "三星"
        elif "magic" in title_lower:
            inferred_brand = "荣耀"
        elif "mate" in title_lower or "nova" in title_lower or "pura" in title_lower or "畅享" in title_lower:
            inferred_brand = "华为"
        elif "iphone" in title_lower:
            inferred_brand = "苹果"
        elif re.search(r"\b(?:find|reno)\s*[a-z0-9]", title_lower):
            inferred_brand = "OPPO"
        elif "mac" in title_lower:
            inferred_brand = "苹果"
        elif "xperia" in title_lower:
            inferred_brand = "索尼"

        if inferred_brand:
            result["brand_cn"] = inferred_brand
            result["brand_normalized"] = BRAND_MAP[inferred_brand]

            # 对该推断品牌重新提取型号（复用主分支的清理逻辑）
            after_brand = title
            after_brand = re.sub(r'^【[^】]+】', '', after_brand).strip()
            after_brand = re.sub(r'^--\s*\d+[新成][新新]?\s*--', '', after_brand).strip()
            after_brand = re.sub(r'--\s*\d+[新成][新新]?\s*--\s*$', '', after_brand).strip()
            after_brand = re.sub(r'^(99新|95新|9新|8成新|9成新)\s*', '', after_brand).strip()
            # 推断品牌后同样要循环剥离重复的品牌名
            inf_en_name = BRAND_MAP[inferred_brand].split("/")[0]
            after_brand = _strip_repeated_brand(after_brand, inferred_brand, inf_en_name)
            after_brand_clean = _strip_title_noise(after_brand)
            model_match = re.match(r'([\u4e00-\u9fa5A-Za-z0-9\s+\-().·／/（）]+)', after_brand_clean)
            if model_match:
                result["model"] = _cleanup_model(model_match.group(1).strip())

    # 纯品牌名处理只对确有“型号=品牌”记录的 iQOO 生效。
    # 旧逻辑对所有品牌都回填英文名，导致“小米澎湃OS”在营销词被清除后
    # 反而得到型号“Xiaomi”。无真实型号时必须保持空值并转人工。
    if result["brand_cn"] == "iQOO" and not result["model"]:
        en_name = result["brand_normalized"].split("/")[0] if result["brand_normalized"] else ""
        if en_name:
            result["model"] = en_name

    _restore_5g_model(title, result)  # ★ 恢复被 \b5G\b 错误剥离的特定 5G 机型
    if ruled_brand:
        result["brand_cn"] = ruled_brand
        result["brand_normalized"] = BRAND_MAP[ruled_brand]
    return result


def normalize_title(parsed: dict, category: str = "") -> str:
    """生成规范化标题"""
    brand = parsed["brand_normalized"]
    # 特例处理：无中文名的品牌（如 vivo/OPPO/iQOO）斜杠两边相同，
    # 标题里只需保留一份（如 "vivo"），避免出现 "vivo/vivo"。
    # 有中文名的品牌（如 "Redmi/红米"）两边不同，保持原样。
    if brand and "/" in brand:
        parts = brand.split("/")
        if len(parts) == 2 and parts[0].lower() == parts[1].lower():
            brand = parts[0]
    model = parsed["model"]
    cat = category.strip() if category else ""
    if brand and model:
        if cat:
            return f"{brand} {model} {cat}"
        return f"{brand} {model}"
    return parsed["raw"]


def read_category(page) -> str:
    """读取商品类目，返回最小分级"""
    log("CATEGORY", "读取商品类目...")

    cat_text = page.evaluate(r"""
        () => {
            let allElements = document.querySelectorAll('*');
            for (let el of allElements) {
                let text = el.innerText || '';
                if (text.includes('商品类目') && text.includes('>')) {
                    let idx = text.indexOf('商品类目');
                    let rest = text.substring(idx + 4).trim();
                    let endIdx = rest.indexOf('修改');
                    if (endIdx > 0) rest = rest.substring(0, endIdx).trim();
                    if (rest.includes('>')) return rest;
                }
                let m = text.match(/^([^>\n]{2,20})\s*>\s*([^>\n]{2,20})$/);
                if (m) return text.trim();
            }
            let divs = document.querySelectorAll('div, span');
            for (let d of divs) {
                let text = d.innerText || '';
                if (text.includes('>') && !text.includes('\n')) {
                    let parts = text.split('>').map(s => s.trim());
                    if (parts.length >= 2 && parts.every(s => s.length > 0 && s.length < 20)) {
                        return text.trim();
                    }
                }
            }
            return '';
        }
    """)

    category = ""
    if cat_text:
        parts = [p.strip() for p in cat_text.split('>')]
        category = parts[-1] if parts else ""
        log("CATEGORY", f"类目路径: '{cat_text}' → 最小分级: '{category}'")
    else:
        log("CATEGORY", "未读取到类目")

    return category


def _title_display_len(s: str) -> int:
    """按抖店规则计算标题“显示长度”：CJK/全角字符计 2，其余计 1（对应“30个汉字/60个字符”限制）。"""
    n = 0
    for ch in s:
        o = ord(ch)
        # CJK 及常见全角字符（排除半角片假名等）计 2，ASCII/数字/半角标点评 1
        if (0x2E80 <= o <= 0x9FFF) or (0xF900 <= o <= 0xFAFF) or (0xFF00 <= o <= 0xFF60) or (0x3000 <= o <= 0x303F):
            n += 2
        else:
            n += 1
    return n


# 标题超限精简：仅删除“成色”与“国行”占位字段，不改变其它内容，不做规范化命名。
_TITLE_CONDITION_BRACKET_RE = re.compile(r"【\s*\d{1,3}\s*新?\s*】")   # 【95新】/【95】/【9】
_TITLE_CONDITION_PLAIN_RE = re.compile(r"\d{1,3}\s*新")                # 95新 / 99新 / 9新（无括号）
_TITLE_LIST_INDEX_RE = re.compile(
    r'(?<!\S)\d{1,3}(?=(?:荣耀|华为|苹果|小米|红米|三星|真我|一加|魅族|黑鲨))'
)

# 平台违禁/占位词：无论标题是否超限，只要读到就全局删除（"国行" 为平台违禁词，绝不可出现在最终标题）。
# 用户要求：全局脚本禁用该字段——识别到就删除，不做任何条件判断。
BANNED_TITLE_TOKENS = ["国行"]


def _strip_banned_title_tokens(value: str) -> str:
    """全局删除平台违禁/占位词（如"国行"），与标题长度/是否超限无关。"""
    s = value or ""
    for tok in BANNED_TITLE_TOKENS:
        s = s.replace(tok, " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _trim_title_overlength(title_value: str) -> str:
    """标题超限时的精简：仅删除成色（如 95新/99新/【95】）与国行占位字段。

    不改变其它逻辑、不做规范化命名——只针对 editable input 值剔除这两个占位字段，
    使其 + 灰框前缀 后的总长度降到 60 以内，满足平台“最长不能超过30个汉字（60个字符）”的限制。
    """
    cleaned = title_value
    cleaned = _TITLE_CONDITION_BRACKET_RE.sub(" ", cleaned)   # 方括号成色
    cleaned = _TITLE_CONDITION_PLAIN_RE.sub(" ", cleaned)     # 无括号成色 95新/99新/9新
    cleaned = cleaned.replace("国行", " ")                     # 国行
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _ensure_category_word(value: str):
    """确保可编辑标题含品类词'二手手机'，防止平台因缺失品类词误判为非二手手机类目。

    返回 (new_value, action)，action ∈ 'already' | 'restore' | 'append'：
    - already: 已含'二手手机'，无需处理；
    - restore: 把被平台截断的孤儿残留（独立成词的'二手手'/'二手'/'二'）补全为'二手手机'；
    - append : 连残留都没有，则在末尾追加' 二手手机'。
    注意：'二'/'二手' 仅作为“独立 token”匹配（前后为空白/标点或行首行尾），
    不满足边界的连写字（如型号中的'二代'：'二'后接'代'）不会被误匹配。
    """
    s = value or ""
    if "二手手机" in s:
        return s, "already"
    _bd_start = r"(?:^|[\s,，、/（）()])"   # 词边界（前）：行首 或 空白/标点
    _bd_end = r"(?:$|[\s,，、/（）()])"     # 词边界（后）：行尾 或 空白/标点
    for tok in ("二手手", "二手", "二"):
        pat = re.compile(_bd_start + re.escape(tok) + _bd_end)
        if pat.search(s):
            new_s = pat.sub(lambda mo, t=tok: mo.group(0).replace(t, "二手手机"), s, count=1)
            return new_s, "restore"
    return (s.rstrip() + " 二手手机").strip(), "append"


def _clean_editable_title(value: str):
    """统一清洗【可编辑标题(input 非灰框值)】：

    1) 无条件删除 成色(方括号/无括号)、国行、全网通/全网 等占位/违禁/SEO 词
       （与标题是否超限无关——只要读到就删，避免这些词干扰平台类目识别）；
    2) 确保含品类词'二手手机'（见 _ensure_category_word：已有则保留，
       否则把被截断的孤儿'二手手'/'二手'/'二'补全，连孤儿都没有则追加）。

    返回 (cleaned_value, actions:list[str])。
    """
    s = value or ""
    actions = []
    without_index = _TITLE_LIST_INDEX_RE.sub("", s)
    if without_index != s:
        actions.append("删异常列表序号")
        s = without_index
    # 1. 平台违禁/占位词：国行（及 BANNED_TITLE_TOKENS 其它词）
    stripped = _strip_banned_title_tokens(s)
    if stripped != s:
        actions.append("删违禁词(国行等)")
    s = stripped
    # 全网通 / 全网（搜索关键词，非型号一部分；平台也常据此误判类目）
    if "全网通" in s:
        s = s.replace("全网通", " ")
        actions.append("删全网通")
    if "全网" in s:
        s = s.replace("全网", " ")
        actions.append("删全网")
    # 成色：方括号 + 无括号（95新/99新/9新）
    s2 = _TITLE_CONDITION_BRACKET_RE.sub(" ", s)
    s2 = _TITLE_CONDITION_PLAIN_RE.sub(" ", s2)
    if s2 != s:
        actions.append("删成色")
    s = s2
    s = re.sub(r"\s+", " ", s).strip()
    # 2. 确保品类词
    s, cat_action = _ensure_category_word(s)
    if cat_action == "restore":
        actions.append("补全二手手机(孤儿)")
    elif cat_action == "append":
        actions.append("追加二手手机")
    return s, actions


def _write_title_value(page, value: str) -> bool:
    """把清洗后的标题写回编辑页 input（设置 value 并触发 input 事件，供 React 状态同步）。"""
    try:
        page.evaluate("""
            (val) => {
                let input = document.getElementById('pg-title-input');
                if (!input) {
                    let inputs = document.querySelectorAll('input');
                    for (let i of inputs) {
                        let ph = i.getAttribute('placeholder') || '';
                        if (ph.includes('15-60个字符')) { input = i; break; }
                    }
                }
                if (!input) return false;
                let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(input, val);
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }
        """, value)
        return True
    except Exception as e:
        log("TITLE", f"写回标题失败: {e}")
        return False


def read_and_normalize_title(page, category: str = "") -> dict:
    """读取商品标题，规范化命名，写回输入框"""
    log("TITLE", "读取商品标题...")

    title_info = page.evaluate("""
        () => {
            let input = document.getElementById('pg-title-input');
            if (!input) {
                let inputs = document.querySelectorAll('input');
                for (let i of inputs) {
                    let ph = i.getAttribute('placeholder') || '';
                    if (ph.includes('15-60个字符')) { input = i; break; }
                }
            }
            if (!input) return { value: '', prefix: '', found: false };

            let prefix = '';
            let wrapper = input.closest('.ecom-g-input-group');
            if (wrapper) {
                let addon = wrapper.querySelector('.ecom-g-input-group-addon');
                if (addon) prefix = addon.innerText.trim();
            }

            return { value: input.value, prefix: prefix, found: true };
        }
    """)

    title_value = title_info.get("value", "")
    prefix = title_info.get("prefix", "")
    log("TITLE", f"前缀(addon): '{prefix}'")
    log("TITLE", f"原始标题(input值): {title_value}")

    # ===== 统一清洗【可编辑标题(input 非灰框值)】=====
    # 用户要求：在“商品标题”非灰框部分，读到了 成色 / 国行 / 全网通，全部删除（与是否超限无关）；
    # 并确保含品类词“二手手机”——后台传来的标题若因超长被平台截断成“二/二手/二手手”，
    # 需补全为“二手手机”，否则平台会把类目误判成非二手手机，导致脚本跑崩。
    cleaned_value, actions = _clean_editable_title(title_value)
    if cleaned_value != title_value:
        if _write_title_value(page, cleaned_value):
            log("TITLE", f"清洗可编辑标题 [{', '.join(actions)}] → input='{cleaned_value}'")
            title_value = cleaned_value
        else:
            log("TITLE", "⚠ 清洗后写回失败，保留清洗值继续解析")
            title_value = cleaned_value
    else:
        log("TITLE", "可编辑标题已合规（无成色/国行/全网通 且含'二手手机'），未修改")

    full_title = f"{prefix} {title_value}".strip()
    log("TITLE", f"完整标题(前缀+输入): '{full_title}'")
    full_len = _title_display_len(full_title)
    log("TITLE", f"完整标题显示长度(汉字计2): {full_len}")
    if full_len > 60:
        # 成色/国行/全网通 已在清洗中无条件删除（已释放长度），仍超限说明型号名本身很长；
        # 此时“二手手机”必须保留，平台可能从末尾再次截断，需关注类目是否被二次误判。
        log("TITLE", f"⚠ 清洗后标题仍超限({full_len}>60)，'二手手机'可能被平台从末尾截断，关注类目")

    parsed = parse_title(full_title)
    log("TITLE", f"解析: 品牌={parsed['brand_cn']}, 规范={parsed['brand_normalized']}, 型号={parsed['model']}, 成色={parsed['condition']}")

    new_title = normalize_title(parsed, category)
    log("TITLE", f"规范化完整标题: {new_title}")

    # 王师傅要求：系统灰框前缀(成色+品牌)是系统自动拼的只读字段，
    # 不需要出现在后面的可编辑标题里，但可编辑标题按原逻辑保留"品牌名+型号+类目"完整内容。
    # 直接用 normalize_title 的结果作为写入值，不再根据前缀去重品牌/成色。
    write_value = new_title

    # 如果 normalize_title 因品牌或型号缺失而回退到 raw（=前缀+输入，含灰框成色+品牌），
    # 改用输入框原值 title_value（不含灰框前缀），避免把成色写入可编辑标题。
    # 注意：不改动 parse_title / 品牌型号筛选逻辑，仅在标题写入环节兜底。
    if write_value == parsed.get("raw", "") and title_value:
        write_value = title_value
        log("TITLE", f"品牌/型号未完全解析，回退到输入框原值(不含灰框前缀): '{write_value}'")

    if not write_value:
        write_value = title_value
        log("TITLE", f"规范化结果为空，回退到原始输入值: '{write_value}'")

    log("TITLE", f"最终 input 值: '{title_value}' (前缀='{prefix}')")
    log("TITLE", f"页面显示完整标题: '{full_title}'")
    if full_len > 60:
        log("TITLE", "⚠ 清洗后标题仍超限，'二手手机'可能被平台从末尾截断")
    elif actions:
        log("TITLE", "✅ 已清洗可编辑标题（删成色/国行/全网通/全网 + 确保二手手机）并写回")
    else:
        log("TITLE", "⏭ 可编辑标题已合规，未修改")

    # 标题已清洗并写回（已含'二手手机'），稍等平台重新识别类目，再读一次类目返回给上层，
    # 防止仍以被截断/误判前的类目为准（类目误判也会让脚本跑崩）。
    try:
        time.sleep(1.2)
        re_cat = read_category(page)
        if re_cat:
            parsed["category"] = re_cat
            log("TITLE", f"清洗后重新识别类目: '{re_cat}'")
        else:
            log("TITLE", "清洗后重新识别类目为空，沿用上层传入类目")
    except Exception as e:
        log("TITLE", f"重新识别类目异常(忽略): {e}")

    return parsed


# ========== 品牌型号 ==========

def probe_select_positions(page) -> list:
    """探测页面上所有 ecom-g-select 的位置"""
    positions = page.evaluate("""
        () => {
            let selects = document.querySelectorAll('.ecom-g-select');
            let result = [];
            for (let s of selects) {
                let rect = s.getBoundingClientRect();
                let y = Math.round(rect.top);
                if (y < 0 || y > window.innerHeight) continue;

                let ph = s.querySelector('.ecom-g-select-selection-placeholder');
                let sel = s.querySelector('.ecom-g-select-selection-item');

                result.push({
                    x: Math.round(rect.left),
                    y: y,
                    hasPlaceholder: !!ph,
                    placeholderText: ph ? ph.innerText.trim() : '',
                    selectedText: sel ? (sel.title || sel.innerText || '').trim() : ''
                });
            }
            result.sort((a, b) => a.y - b.y);
            return result;
        }
    """)
    log("PROBE", f"探测到 {len(positions)} 个 select (视口内):")
    for p in positions:
        log("PROBE", f"  x={p.get('x', -1)} y={p['y']} | placeholder='{p['placeholderText']}' | selected='{p['selectedText']}'")
    return positions


# 平台品牌必须保持严格身份一致。父品牌/子品牌（例如 Xiaomi 与 Redmi）不能
# 互相作为别名，否则会跳过品牌切换，并在错误的型号库中搜索。
BRAND_VERIFY_ALIASES = {}


def resolve_brand_model_plan(parsed: dict) -> dict:
    """生成品牌/型号流程唯一使用的目标方案。

    解析层只描述标题；本函数把它转换成平台执行目标。后续品牌验证、品牌切换、
    型号搜索和终检都必须读取本方案，不再各自推断父品牌/子品牌。
    """
    source_brand_cn = str(parsed.get("brand_cn") or "").strip()
    source_brand_value = str(parsed.get("brand_normalized") or "").strip()
    source_model = str(parsed.get("model") or "").strip()
    title_model = str(parsed.get("title_model") or source_model).strip()
    source_brand_en = source_brand_value.split("/", 1)[0].strip() if source_brand_value else ""

    is_redmi = source_brand_cn == "红米" or bool(
        re.search(r"(?:^|[\s/])(?:redmi|红米)(?:[\s/]|$)", source_model, re.I)
    )
    target_brand_cn = "红米" if is_redmi else source_brand_cn
    target_brand_value = BRAND_MAP.get(target_brand_cn, source_brand_value)
    target_brand_en = target_brand_value.split("/", 1)[0].strip() if target_brand_value else source_brand_en
    override_key, exact_model = _resolve_model_override(
        source_model, target_brand_cn, target_brand_en
    )
    return {
        "source_brand_cn": source_brand_cn,
        "source_brand_value": source_brand_value,
        "source_model": source_model,
        "title_model": title_model,
        "target_brand_cn": target_brand_cn,
        "target_brand_en": target_brand_en,
        "target_brand_value": target_brand_value,
        "target_model_exact": exact_model or "",
        "target_model_override_key": override_key or "",
        "is_redmi": is_redmi,
    }


def resolve_brand_model(title: str) -> dict:
    """品牌/型号解析唯一公开入口，同时给出标题型号和平台精确型号。"""
    parsed = parse_title(title)
    plan = resolve_brand_model_plan(parsed)
    source_model = _strip_brand_tokens(
        plan["source_model"], plan["target_brand_cn"], plan["target_brand_en"]
    )
    plan["brand"] = plan["target_brand_value"]
    plan["model"] = plan["target_model_exact"] or source_model
    plan["raw"] = parsed.get("raw", title)
    plan["condition"] = parsed.get("condition", "")
    return plan


def _brand_value_matches_plan(plan: dict, selected_text: str) -> bool:
    """严格比较页面品牌与目标平台品牌，不允许跨父/子品牌。"""
    selected = re.sub(r"\s+", "", str(selected_text or "")).casefold()
    if not selected:
        return False
    expected = re.sub(r"\s+", "", str(plan.get("target_brand_value") or "")).casefold()
    if expected and selected == expected:
        return True
    # 兼容平台偶尔只显示联合品牌的一侧，但仍要求是同一品牌身份。
    allowed = {
        re.sub(r"\s+", "", str(plan.get("target_brand_cn") or "")).casefold(),
        re.sub(r"\s+", "", str(plan.get("target_brand_en") or "")).casefold(),
    }
    return selected in {v for v in allowed if v}

def _brand_matches(brand_cn: str, selected_text: str, model: str = "") -> bool:
    """检查解析出的品牌是否与下拉框选中值匹配（支持别名）"""
    normalized = BRAND_MAP.get(brand_cn, brand_cn)
    return _brand_value_matches_plan(resolve_brand_model_plan({
        "brand_cn": brand_cn,
        "brand_normalized": normalized,
        "model": model,
    }), selected_text)


def _display_brand_for_redmi(brand_cn: str, brand_en: str, model: str = "") -> tuple:
    """兼容旧调用点；实际规则统一由 resolve_brand_model_plan 产生。"""
    plan = resolve_brand_model_plan({
        "brand_cn": brand_cn,
        "brand_normalized": f"{brand_en}/{brand_cn}" if brand_en else BRAND_MAP.get(brand_cn, ""),
        "model": model,
    })
    return (plan["target_brand_cn"], plan["target_brand_en"])

def verify_brand(page, parsed: dict, select_positions: list) -> dict:
    """验证品牌下拉框的值与标题对应"""
    log("BRAND", "验证品牌下拉框...")

    plan = resolve_brand_model_plan(parsed)
    brand_cn = plan["target_brand_cn"]
    model = plan["source_model"]

    if not brand_cn:
        log("BRAND", "标题中未解析到品牌，跳过验证")
        return {"matched": False, "brand_value": "", "brand_y": -1}

    brand_info = {"matched": False, "brand_value": "", "brand_y": -1, "brand_x": -1}

    for p in select_positions:
        if _brand_value_matches_plan(plan, p["selectedText"]):
            brand_info["brand_value"] = p["selectedText"]
            brand_info["brand_y"] = p["y"]
            brand_info["brand_x"] = p.get("x", -1)
            brand_info["matched"] = True
            log("BRAND", f"品牌匹配: '{p['selectedText']}' (x={p.get('x', -1)}, y={p['y']}) 与 '{brand_cn}' 匹配")
            break

    if not brand_info["matched"]:
        for p in select_positions:
            if p["selectedText"] and not p["hasPlaceholder"]:
                brand_info["brand_value"] = p["selectedText"]
                brand_info["brand_y"] = p["y"]
                brand_info["brand_x"] = p.get("x", -1)
                brand_info["matched"] = _brand_value_matches_plan(plan, p["selectedText"])
                log("BRAND", f"备选品牌: '{p['selectedText']}' (x={p.get('x', -1)}, y={p['y']}) 匹配={brand_info['matched']}")
                break

    if not brand_info["brand_value"]:
        log("BRAND", "未找到品牌下拉框的值!")
    elif not brand_info["matched"]:
        log("BRAND", f"品牌不匹配! 标题品牌='{brand_cn}', 下拉框='{brand_info['brand_value']}'")

    return brand_info


def change_brand(page, parsed: dict, brand_y: int) -> dict:
    """修改品牌下拉框为标题识别的品牌"""
    plan = resolve_brand_model_plan(parsed)
    brand_cn = plan["target_brand_cn"]
    brand_normalized = plan["target_brand_value"]
    if not brand_cn:
        return {"success": False, "brand_value": "", "error": "品牌为空"}

    log("BRAND", f"开始修改品牌下拉框为 '{brand_cn}'...")

    # 品牌框在探测后可能因页面吸顶/弹窗产生纵向位移，不能只接受 ±2px。
    # 重新选取离原坐标最近一行的最左侧下拉框，再用可信鼠标点击打开。
    open_result = page.evaluate("""
        (brandY) => {
            let selects = document.querySelectorAll('.ecom-g-select');
            let candidates = [];
            for (let s of selects) {
                let rect = s.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0 || rect.bottom <= 0) continue;
                let selected = s.querySelector('.ecom-g-select-selection-item');
                let trigger = s.querySelector('.ecom-g-select-selector');
                if (!selected || !trigger) continue;
                candidates.push({
                    x: Math.round(rect.left), y: Math.round(rect.top),
                    cx: Math.round(rect.left + rect.width / 2),
                    cy: Math.round(rect.top + rect.height / 2),
                    dist: Math.abs(Math.round(rect.top) - brandY),
                    value: (selected.title || selected.innerText || '').trim()
                });
            }
            candidates.sort((a, b) => a.dist - b.dist || a.x - b.x);
            let best = candidates.length ? candidates[0] : null;
            if (!best || best.dist > 140) return { opened: false, y: -1 };
            return { opened: true, ...best };
        }
    """, brand_y)

    if not open_result.get("opened"):
        log("BRAND", "打开品牌下拉框失败!")
        return {"success": False, "brand_value": "", "error": "打开下拉框失败"}

    page.mouse.click(open_result["cx"], open_result["cy"])
    log("BRAND", f"品牌下拉框已打开 (value='{open_result.get('value', '')}', x={open_result.get('x')}, y={open_result['y']})")
    time.sleep(0.8)

    # 首次修改品牌时，抖店可能先弹出“商品规格填写规则升级”提示。
    # 该提示会挡住品牌弹层；安全关闭后必须重新定位并再次真实点击品牌框。
    modal_result = {"closed": False, "text": "", "action": ""}
    # 弹窗正文和按钮经常分两阶段渲染，最多等待约5秒让“我知道了”可点击。
    for _ in range(5):
        modal_result = _dismiss_blocking_model_modal(page)
        if modal_result.get("closed") or not modal_result.get("text"):
            break
        time.sleep(1)
    if modal_result.get("closed"):
        log("BRAND", f"品牌切换前已安全关闭规则提示: {modal_result.get('action', '')}")
        time.sleep(0.5)
        reopened = page.evaluate("""
            (expectedValue) => {
                const selects = [...document.querySelectorAll('.ecom-g-select')];
                const matches = selects.map(s => {
                    const r = s.getBoundingClientRect();
                    const item = s.querySelector('.ecom-g-select-selection-item');
                    const value = item ? (item.title || item.innerText || '').trim() : '';
                    return {s, r, value};
                }).filter(x => x.r.width > 0 && x.r.height > 0 && x.value === expectedValue);
                if (!matches.length) return {found:false};
                matches.sort((a,b) => a.r.left - b.r.left);
                const x = matches[0];
                return {
                    found:true,
                    cx:Math.round(x.r.left + x.r.width/2),
                    cy:Math.round(x.r.top + x.r.height/2),
                    arrowX:Math.round(x.r.right - 16),
                    y:Math.round(x.r.top)
                };
            }
        """, open_result.get("value", ""))
        if not reopened.get("found"):
            return {"success": False, "brand_value": "", "error": "关闭规则提示后未重新定位到品牌框"}
        # 品牌字段中心是只读展示区，抖店此控件需点右侧箭头才能真正展开。
        page.mouse.click(reopened["arrowX"], reopened["cy"])
        brand_y = reopened["y"]
        log("BRAND", f"关闭规则提示后点击品牌框右侧箭头 (x={reopened['arrowX']}, y={brand_y})")
        time.sleep(0.8)
        dropdown_open = page.evaluate("""
            () => [...document.querySelectorAll('.ecom-g-select-dropdown')].some(d => {
                const r=d.getBoundingClientRect(), s=getComputedStyle(d);
                return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
            })
        """)
        if not dropdown_open:
            # 页面重排时坐标点击偶尔落在遮罩层；按当前值重新找到可见品牌 selector 并 DOM 直点。
            dropdown_open = page.evaluate("""
                (expectedValue) => {
                    const candidates=[...document.querySelectorAll('.ecom-g-select')].filter(s => {
                        const r=s.getBoundingClientRect(), item=s.querySelector('.ecom-g-select-selection-item');
                        const value=item ? (item.title || item.innerText || '').trim() : '';
                        return r.width>0 && r.height>0 && r.top>=0 && r.bottom<=innerHeight && value===expectedValue;
                    }).sort((a,b)=>a.getBoundingClientRect().left-b.getBoundingClientRect().left);
                    if (!candidates.length) return false;
                    const trigger=candidates[0].querySelector('.ecom-g-select-selector') || candidates[0];
                    trigger.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));
                    trigger.click();
                    return true;
                }
            """, open_result.get("value", ""))
            log("BRAND", f"坐标点击未展开品牌弹层，DOM直点重试: {'已执行' if dropdown_open else '未找到可见品牌框'}")
            time.sleep(1.0)
    elif modal_result.get("text"):
        return {"success": False, "brand_value": "", "error": "规格规则提示未能安全关闭"}
    else:
        time.sleep(0.7)

    # 输入品牌关键字搜索
    # 修复: 优先使用规范化品牌名(如 "Redmi/红米")搜索，平台下拉选项是这种格式
    search_keywords = []
    brand_en = brand_normalized.split("/")[0] if brand_normalized else ""

    # Redmi 在 Excel 中可能仍归为小米，但抖店必须先切到独立的 Redmi/红米品牌。
    if plan["is_redmi"]:
        search_keywords = ["Redmi/红米", "红米", "Redmi"]
        log("BRAND", f"【红米子品牌】检测到 Redmi 机型(model={parsed.get('model')!r})，强制搜索关键字切到: {search_keywords}")
    else:
        # 第1优先级: 规范化品牌名 (如 "Redmi/红米")
        if brand_normalized and brand_normalized not in search_keywords:
            search_keywords.append(brand_normalized)

        # 第2优先级: 中文品牌名 (如 "红米")
        if brand_cn and brand_cn not in search_keywords:
            search_keywords.append(brand_cn)

        # 第3优先级: 英文品牌名 (如 "Redmi")
        if brand_en and brand_en not in search_keywords:
            search_keywords.append(brand_en)

    log("BRAND", f"品牌搜索关键字候选: {search_keywords}")

    for keyword in search_keywords:
        # 每个关键字都重新聚焦/展开品牌框。规则提示关闭、上一轮搜索无结果等场景会让
        # Select 保持“看似展开但键盘焦点已丢失”的状态；若直接继续 type，文字不会进入
        # 过滤器，甚至会把多个候选词累积起来。重新点击并清空可确保每次尝试相互独立。
        try:
            page.mouse.click(open_result["cx"], open_result["cy"])
            time.sleep(0.8)
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            time.sleep(0.2)
        except Exception as exc:
            log("BRAND", f"品牌框重新聚焦失败({exc})，继续现有兜底路径")

        # 方案-1：新版 ecom Select 的可搜索 input 位于 Select 本体内部，不在
        # dropdown DOM 内。按“当前品牌值 + 最左列”锁定品牌框，只在其内部真实
        # fill，随后再读取可见弹层。这样不会误写标题，也不依赖漂移坐标。
        try:
            brand_boxes = page.locator('.ecom-g-select').filter(has=page.locator(
                '.ecom-g-select-selection-item', has_text=open_result.get("value", "")
            ))
            brand_box = None
            for box_index in range(brand_boxes.count()):
                candidate = brand_boxes.nth(box_index)
                if candidate.is_visible():
                    if brand_box is None or candidate.bounding_box()["x"] < brand_box.bounding_box()["x"]:
                        brand_box = candidate
            if brand_box is not None:
                trigger = brand_box.locator('.ecom-g-select-selector').first
                trigger.click(force=True)
                inner_input = brand_box.locator('input').first
                if inner_input.count():
                    inner_input.click(force=True)
                    inner_input.fill(keyword, force=True)
                    time.sleep(1.2)
                    log("BRAND", f"[品牌框内输入] 已在锁定品牌 Select 内输入: '{keyword}'")
                    visible_options = page.locator(
                        '.ecom-g-select-dropdown:not(.ecom-g-select-dropdown-hidden) '
                        '.ecom-g-select-item:visible, [role="option"]:visible'
                    )
                    matches = []
                    for option_index in range(min(visible_options.count(), 100)):
                        option = visible_options.nth(option_index)
                        option_text = (option.inner_text() or "").strip()
                        if option_text and option_text.casefold() == keyword.casefold():
                            matches.append((option, option_text))
                    if matches:
                        matches[0][0].click()
                        time.sleep(1)
                        log("BRAND", f"品牌框内输入精确选择成功: '{matches[0][1]}'")
                        return {"success": True, "brand_value": matches[0][1], "error": ""}
                else:
                    log("BRAND", "锁定品牌框内没有 input，继续下拉层兼容路径")
        except Exception as exc:
            log("BRAND", f"品牌框内真实输入失败({exc})，继续下拉层兼容路径")

        # 方案0（与型号搜索框"真实点击+真实键盘"一致的已验证方案，2026-08-27 补齐）：
        # 品牌搜索框之前只有 locator.fill()/合成 dispatchEvent 两条路径，对本控件不可靠——
        # 实测(店铺 51165770/255 的 Redmi 子品牌切换)输入后下拉选项完全未过滤，仍是打开时的
        # 默认全量列表(如'爱回收''BARBIE/芭比'等无关品牌)，导致"Redmi/红米"这类已知存在于
        # 列表中的选项永远搜不到，进而级联到型号框定位失败。型号搜索框已用"JS定位输入框坐标
        # + 真实鼠标点击聚焦 + Playwright 真实键盘输入"解决过同一类问题，这里复用同一方案。
        try:
            input_coords = page.evaluate("""
                () => {
                    let dropdown = [...document.querySelectorAll('.ecom-g-select-dropdown')].find(d => {
                        const r = d.getBoundingClientRect(), s = getComputedStyle(d);
                        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                    });
                    if (!dropdown) return null;
                    let input = dropdown.querySelector('input,textarea');
                    if (!input) return null;
                    let r = input.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return null;
                    return { cx: Math.round(r.left + r.width / 2), cy: Math.round(r.top + r.height / 2) };
                }
            """)
            if input_coords:
                page.mouse.click(input_coords["cx"], input_coords["cy"])
                time.sleep(0.2)
                page.keyboard.press("Control+a")
                time.sleep(0.1)
                page.keyboard.type(keyword, delay=30)
                time.sleep(1.2)
                log("BRAND", f"[真实输入] 点击品牌搜索框并键盘输入关键字: '{keyword}'")

                visible_options = page.locator(
                    '.ecom-g-select-dropdown:not(.ecom-g-select-dropdown-hidden) '
                    '.ecom-g-select-item:visible, [role="option"]:visible'
                )
                option_texts = []
                for idx in range(min(visible_options.count(), 100)):
                    option = visible_options.nth(idx)
                    text_value = (option.inner_text() or "").strip()
                    if text_value:
                        option_texts.append((option, text_value))
                exact = [item for item in option_texts if item[1].casefold() == keyword.casefold()]
                contains = [item for item in option_texts if keyword.casefold() in item[1].casefold()]
                chosen = (exact or contains)
                if chosen:
                    chosen[0][0].click()
                    selected_text = chosen[0][1]
                    time.sleep(1)
                    log("BRAND", f"真实输入精确选择品牌: '{selected_text}'")
                    return {"success": True, "brand_value": selected_text, "error": ""}
                log("BRAND", f"真实输入未命中 '{keyword}'，选项样本: {[x[1] for x in option_texts[:20]]}")
            else:
                log("BRAND", "未定位到品牌搜索输入框坐标，跳过真实输入方案，回退旧路径")
        except Exception as exc:
            log("BRAND", f"真实输入方案失败({exc})，回退旧路径")

        # 旧路径兜底（保留，避免真实输入方案覆盖不到的边缘情况回归）：
        # 优先限定在当前可见品牌弹层内真实输入/点击，避免误写商品标题等页面输入框。
        try:
            dropdown_inputs = page.locator(
                '.ecom-g-select-dropdown:not(.ecom-g-select-dropdown-hidden) input:visible, '
                '[role="listbox"] input:visible'
            )
            if dropdown_inputs.count() > 0:
                search_input = dropdown_inputs.last
                search_input.click()
                search_input.fill(keyword)
            else:
                # 部分抖店 Select 使用隐藏搜索 input；打开后键盘输入会交给当前 Select。
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                page.keyboard.type(keyword)
            time.sleep(1.2)

            visible_options = page.locator(
                '.ecom-g-select-dropdown:not(.ecom-g-select-dropdown-hidden) '
                '.ecom-g-select-item:visible, [role="option"]:visible'
            )
            option_texts = []
            for idx in range(min(visible_options.count(), 100)):
                option = visible_options.nth(idx)
                text_value = (option.inner_text() or "").strip()
                if text_value:
                    option_texts.append((option, text_value))
            exact = [item for item in option_texts if item[1].casefold() == keyword.casefold()]
            contains = [item for item in option_texts if keyword.casefold() in item[1].casefold()]
            chosen = (exact or contains)
            if chosen:
                chosen[0][0].click()
                selected_text = chosen[0][1]
                time.sleep(1)
                log("BRAND", f"可见弹层精确选择品牌: '{selected_text}'")
                return {"success": True, "brand_value": selected_text, "error": ""}
            log("BRAND", f"可见品牌弹层未命中 '{keyword}'，选项样本: {[x[1] for x in option_texts[:20]]}")
        except Exception as exc:
            log("BRAND", f"可见品牌弹层搜索失败，将使用兼容路径: {exc}")

        search_result = page.evaluate("""
            (keyword) => {
                let listbox = document.querySelector('[role=listbox]');
                let input = null;
                if (listbox) {
                    let parent = listbox.parentElement;
                    input = parent ? parent.querySelector('input') : null;
                }
                if (!input) {
                    let inputs = document.querySelectorAll('input');
                    for (let i of inputs) {
                        let rect = i.getBoundingClientRect();
                        if (rect.height > 0 && rect.width > 0 && rect.top > 0) {
                            let ph = i.getAttribute('placeholder') || '';
                            if (ph.includes('搜索') || ph.includes('请输入') || ph.includes('选择') || !ph) {
                                input = i;
                                break;
                            }
                        }
                    }
                }
                if (!input) {
                    return { success: false, error: '未找到搜索输入框' };
                }
                let nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                nativeSetter.call(input, keyword);
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
                // 模拟键盘事件触发搜索过滤
                input.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }));
                input.dispatchEvent(new KeyboardEvent('keyup', { key: 'End', bubbles: true }));
                return { success: true, inputFound: true };
            }
        """, keyword)

        if not search_result.get("success"):
            log("BRAND", f"搜索输入失败: {search_result.get('error', '未知错误')}")
            continue

        log("BRAND", f"已输入搜索关键字: '{keyword}'")
        time.sleep(3)

        # 选择匹配项（增强选择器，支持多种下拉框结构）
        select_result = page.evaluate("""
            (keyword) => {
                // 收集所有可能的选项元素
                let allItems = [];
                let selectors = [
                    'div.ecom-g-select-item',
                    'div[role="option"]',
                    '.ecom-g-select-dropdown-menu-item',
                    '.ecom-g-select-item-option-content',
                    'div.ant-select-item-option-content',
                    'div.rc-virtual-list-holder-inner > div'
                ];
                for (let sel of selectors) {
                    let found = document.querySelectorAll(sel);
                    for (let item of found) {
                        allItems.push(item);
                    }
                }

                // 调试: 输出所有选项文本
                let allTexts = allItems.map(d => d.innerText.trim()).filter(t => t);

                // 精确匹配（包含关系）
                for (let d of allItems) {
                    let text = d.innerText.trim();
                    if (text && text.includes(keyword)) {
                        d.dispatchEvent(new MouseEvent('click', {
                            bubbles: true, cancelable: true, view: window
                        }));
                        return { clicked: true, text: text, allTexts: allTexts };
                    }
                }
                // 模糊匹配（不区分大小写）
                for (let d of allItems) {
                    let text = d.innerText.trim();
                    if (text && text.toLowerCase().includes(keyword.toLowerCase())) {
                        d.dispatchEvent(new MouseEvent('click', {
                            bubbles: true, cancelable: true, view: window
                        }));
                        return { clicked: true, text: text, fuzzy: true, allTexts: allTexts };
                    }
                }
                return { clicked: false, error: '未找到匹配项', allTexts: allTexts };
            }
        """, keyword)

        if select_result.get("clicked"):
            selected_text = select_result.get("text", "")
            fuzzy = select_result.get("fuzzy", False)
            log("BRAND", f"{'模糊' if fuzzy else '精确'}匹配选择品牌: '{selected_text}'")
            time.sleep(1)

            # 验证
            verify = page.evaluate("""
                (brandY) => {
                    let selects = document.querySelectorAll('.ecom-g-select');
                    for (let s of selects) {
                        let rect = s.getBoundingClientRect();
                        let y = Math.round(rect.top);
                        if (y === brandY || Math.abs(y - brandY) <= 2) {
                            let sel = s.querySelector('.ecom-g-select-selection-item');
                            if (sel) {
                                return { verified: true, value: (sel.title || sel.innerText || '').trim() };
                            }
                        }
                    }
                    return { verified: false, value: '' };
                }
            """, brand_y)

            if verify.get("verified"):
                log("BRAND", f"品牌修改验证成功: '{verify['value']}'")
                page.keyboard.press("Escape")
                time.sleep(0.5)
                return {"success": True, "brand_value": verify["value"], "error": ""}
            else:
                log("BRAND", f"品牌修改后未验证到值，但已尝试选择: '{selected_text}'")
                page.keyboard.press("Escape")
                time.sleep(0.5)
                return {"success": True, "brand_value": selected_text, "error": "验证未通过但已选择"}

    log("BRAND", f"所有搜索关键字均未匹配: {search_keywords}")
    # 调试: 输出最后看到的所有选项文本
    try:
        all_texts = select_result.get("allTexts", [])
        if all_texts:
            log("BRAND", f"下拉框所有选项: {all_texts}")
    except:
        pass
    page.keyboard.press("Escape")
    return {"success": False, "brand_value": "", "error": f"未找到匹配品牌，已尝试: {search_keywords}"}


def _ensure_title_suffix(page, category: str) -> None:
    """确保标题 input 末尾包含类目后缀（已禁用：跳过标题修改）"""
    if not category:
        return
    log("TITLE", f"⏭ 跳过标题类目后缀补回（不修改标题）")


def _locate_model_open_result(page, model_x: int, model_y: int, brand_y: int) -> dict:
    """回退用：通过坐标最近/bestByLabel 等方式定位型号下拉框并返回其中心坐标与元数据。

    正常路径（方案C）已改用 _pin_and_locate_model_box 直接锁定型号框并以其返回的中心点击，
    不再二次"找最近"猜测；此函数仅作为 pin 失败时的兜底。
    """
    return page.evaluate("""
        (params) => {
            let brandY = params.brandY;
            let modelX = params.modelX;
            let modelY = params.modelY;

            function bestByCoords(){
                if (modelX < 0 || modelY < 0) return null;
                let selects = document.querySelectorAll('.ecom-g-select');
                let best = null, bestDist = 999999;
                for (let s of selects) {
                    let rect = s.getBoundingClientRect();
                    let x = rect.left + rect.width / 2;
                    let y = rect.top + rect.height / 2;
                    let dist = Math.abs(x - modelX) + Math.abs(y - modelY);
                    if (dist < bestDist) { bestDist = dist; best = s; }
                }
                return (best && bestDist <= 200) ? {el: best, dist: bestDist, method: 'exact_model_coords'} : null;
            }

            function bestByBrandRow(){
                if (brandY < 0) return null;
                let selects = document.querySelectorAll('.ecom-g-select');
                let best = null, bestX = -1;
                for (let s of selects) {
                    let ph = s.querySelector('.ecom-g-select-selection-placeholder');
                    if (ph && ph.innerText.trim() === '请选择') {
                        let rect = s.getBoundingClientRect();
                        let y = Math.round(rect.top);
                        let x = Math.round(rect.left);
                        if (Math.abs(y - brandY) <= 2 && x > bestX) {
                            bestX = x; best = s;
                        }
                    }
                }
                return best ? {el: best, dist: 0, method: 'same_row_rightmost'} : null;
            }

            function bestByLabel(){
                let allElements = document.querySelectorAll('*');
                let best = null, bestDist = 999999;
                for (let el of allElements) {
                    let text = (el.innerText || el.textContent || '').trim();
                    if (text.includes('型号') && text.length <= 10) {
                        let r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0 || r.top < 0 || r.bottom > window.innerHeight) continue;
                        let parent = el.parentElement;
                        for (let i = 0; i < 6 && parent; i++) {
                            let select = parent.querySelector('.ecom-g-select');
                            if (select) {
                                let sr = select.getBoundingClientRect();
                                if (sr.width > 0 && sr.height > 0 && sr.top > 0 && sr.bottom < window.innerHeight) {
                                    let dist = 0;
                                    if (modelX >= 0 && modelY >= 0) {
                                        let sx = sr.left + sr.width/2, sy = sr.top + sr.height/2;
                                        dist = Math.abs(sx - modelX) + Math.abs(sy - modelY);
                                    }
                                    if (dist < bestDist) {
                                        bestDist = dist;
                                        best = {el: select, dist: dist, method: 'model_label'};
                                    }
                                }
                                break;
                            }
                            parent = parent.parentElement;
                        }
                    }
                }
                if (best && bestDist <= 250) return best;
                return null;
            }

            function bestByFirstPlaceholder(){
                let selects = document.querySelectorAll('.ecom-g-select');
                for (let s of selects) {
                    let ph = s.querySelector('.ecom-g-select-selection-placeholder');
                    if (ph && ph.innerText.trim() === '请选择') {
                        let rect = s.getBoundingClientRect();
                        let y = Math.round(rect.top);
                        if (y < 0 || y > window.innerHeight) continue;
                        return {el: s, dist: 0, method: 'first_placeholder'};
                    }
                }
                return null;
            }

            let found = bestByCoords() || bestByBrandRow() || bestByLabel() || bestByFirstPlaceholder();
            if (!found) return { opened: false, x: -1, y: -1, cx: -1, cy: -1 };
            let rect = found.el.getBoundingClientRect();
            let trigger = found.el.querySelector('.ecom-g-select-selector');
            return {
                opened: true,
                x: Math.round(rect.left), y: Math.round(rect.top),
                cx: Math.round(rect.left + rect.width / 2),
                cy: Math.round(rect.top + rect.height / 2),
                method: found.method,
                aria_controls: trigger ? (trigger.getAttribute('aria-controls') || trigger.getAttribute('aria-owns') || '') : ''
            };
        }
    """, {"brandY": brand_y, "modelX": model_x, "modelY": model_y})


def _dismiss_blocking_model_modal(page) -> dict:
    """Close only non-destructive modal actions before touching the model field."""
    result = page.evaluate("""
        () => {
            const dialogs = [...document.querySelectorAll(
                '[role="dialog"], .ecom-g-modal-wrap, .ant-modal-wrap, [class*="modal-wrap"]'
            )].filter(d => {
                const r = d.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            });
            if (!dialogs.length) return {found:false, closed:false, text:'', buttons:[]};
            const dialog = dialogs[dialogs.length - 1];
            const text = (dialog.innerText || '').trim().slice(0, 300);
            const buttons = [...dialog.querySelectorAll('button, [role="button"]')]
                .filter(b => { const r=b.getBoundingClientRect(); return r.width>0 && r.height>0; });
            const labels = buttons.map(b => (b.innerText || b.getAttribute('aria-label') || '').trim());
            const safe = ['取消', '关闭', '暂不', '稍后', '我知道了', '知道了'];
            for (const wanted of safe) {
                const btn = buttons.find(b => {
                    const label=(b.innerText || b.getAttribute('aria-label') || '').trim();
                    return label === wanted || label.includes(wanted);
                });
                if (btn) { btn.click(); return {found:true, closed:true, action:wanted, text, buttons:labels}; }
            }
            const close = dialog.querySelector(
                '[aria-label="Close"], [aria-label="关闭"], .ecom-g-modal-close, .ant-modal-close, [class*="modal-close"]'
            );
            if (close) { close.click(); return {found:true, closed:true, action:'close-icon', text, buttons:labels}; }
            return {found:true, closed:false, action:'', text, buttons:labels};
        }
    """)
    if result.get("found") and not result.get("closed"):
        # Escape is non-affirming and therefore safer than guessing a confirm button.
        try:
            page.keyboard.press("Escape")
            time.sleep(0.5)
            result["closed"] = not bool(page.locator('[role="dialog"]:visible').count())
            result["action"] = "Escape" if result["closed"] else ""
        except Exception:
            pass
    if result.get("found"):
        log("MODEL", "[安全清弹窗] closed=%s action=%s text=%r buttons=%r" % (
            result.get("closed"), result.get("action"), result.get("text"), result.get("buttons")))
        if result.get("closed"):
            time.sleep(0.8)
    return result


def _try_select_model_with_keyword(page, keyword: str, category: str = "", brand_y: int = -1, original_model: str = "",
                                    model_x: int = -1, model_y: int = -1,
                                    oppo_brand: bool = False, pure_model: str = "",
                                    brand_cn: str = "", brand_en: str = "", brand_x: int = -1,
                                    _modal_retry_count: int = 0) -> dict:
    """使用指定关键字尝试搜索并选择型号

    参数:
        original_model: 原始完整型号，用于匹配验证（避免截断后的短关键字误匹配）
    """
    blocking_modal = _dismiss_blocking_model_modal(page)
    if blocking_modal.get("found") and not blocking_modal.get("closed"):
        return {"success": False, "model_value": "", "error": "型号框被未知确认弹窗遮挡，已安全停止"}

    # 每次尝试都重新锁定基础信息页 + 刷新型号框坐标，避免上次尝试后页面滚动/下拉关闭
    # 导致 model_x/model_y 失效、误点/误搜到错误下拉框（如 CPU/芯片列表）。
    # 关键修复：hint 传品牌框坐标(brand_x, brand_y)，确保 pin 定位到品牌框后取右侧型号框，
    # 直接点击型号框中心，绝不误点同高同行、x 位移不同的品牌框。
    # hint 传"新鲜型号框坐标(model_x,model_y)"而非品牌框坐标：pin 内部按"离 hint 最近且有
    # 右侧型号框"择优，可避免跨商品残留的旧品牌框被误选（旧框无右侧型号框会被过滤）。
    pin = _pin_and_locate_model_box(page, model_x, model_y, brand_cn, brand_en)
    # 型号框中心坐标：供后续验证探针使用，必须钉死在型号框中心，
    # 绝不能用被点击选项的旧中心坐标（否则下拉关闭后该点落入相邻 select，验证读到错误值）。
    model_cx, model_cy = model_x, model_y
    if pin.get("ok"):
        px, py = pin["x"], pin["y"]
        #  Sanity check：pin 结果不应离原始 hint(新鲜 PROBE 坐标)太远；若偏差过大，说明 pin 可能
        #  命中了 stale/错误品牌框，应沿用原始 hint 而不是接受错误坐标。
        dx = abs(px - model_x)
        dy = abs(py - model_y)
        # 阈值要足够紧：平台布局固定，型号框 x 不应偏离 hint 超过一个列宽(~234px)，
        # 否则会把成色/保修框误当型号框（如 hint x=692，错误 pin 返回 x=926，dx=234）。
        if dx > 150 or dy > 80:
            log("MODEL", f"[坐标刷新] 警告：pin 结果(x={px},y={py}) 离 hint(x={model_x},y={model_y}) 过远(dx={dx},dy={dy})，视为不可信，沿用 hint")
        else:
            model_x, model_y = px, py
            model_cx = pin.get("cx", model_x)
            model_cy = pin.get("cy", model_y)
            log("MODEL", f"[坐标刷新] 关键字'{keyword}'尝试前型号框坐标: x={model_x}, y={model_y} (中心 cx={model_cx}, cy={model_cy})")
    else:
        log("MODEL", f"[坐标刷新] 关键字'{keyword}'未能重新定位，沿用 x={model_x}, y={model_y}")

    # 打开下拉框：用真实鼠标点击型号下拉框中心，触发 rc-select 真正的 onClick，
    # 这样 popup 才会正确挂上 aria-controls，搜索 input 才能被 Playwright 可靠定位。
    # 旧的合成 mousedown 会导致 popup 状态不完整、搜索输入框找不到或输入不触发过滤。
    # 方案C：pin 成功时直接以锁定到的型号框中心点击，不再走 bestByCoords 二次"找最近"猜测
    # （解决"左上角当中心点比对"造成的偏移、误选隔壁框）；pin 失败时回退 _locate_model_open_result 兜底。
    if pin.get("ok"):
        log("MODEL", f"[方案C] 直接点击锁定型号框中心(cx={pin.get('cx')}, cy={pin.get('cy')}, method=pinned_model_box, 品牌框x={pin.get('brandX')}, 型号框x={pin.get('x')})")
        open_result = {
            "opened": True,
            "x": pin["x"], "y": pin["y"],
            "cx": pin.get("cx", model_x), "cy": pin.get("cy", model_y),
            "method": "pinned_model_box",
            "aria_controls": pin.get("ariaControls", "")
        }
    else:
        open_result = _locate_model_open_result(page, model_x, model_y, brand_y)

    if not open_result.get("opened"):
        return {"success": False, "model_value": "", "error": "打开下拉框失败"}

    # 直接使用 pin 返回的当前视口坐标。不再调用 locator.scroll_into_view，
    # 避免 Redmi 页面在点击前发生纵向重排。
    cx = open_result.get("cx", -1)
    cy = open_result.get("cy", -1)
    opened_by_dom = False

    if cx >= 0 and cy >= 0:
        if not opened_by_dom:
            try:
                page.mouse.click(cx, cy)
                log("MODEL", f"[真实点击] 打开型号下拉框中心 (x={cx}, y={cy}, method={open_result.get('method')})")
            except Exception as e:
                log("MODEL", f"[真实点击] 打开失败({e})，回退合成 mousedown")
                page.evaluate("""
                    (params) => {
                        let x = params.cx, y = params.cy;
                        let el = document.elementFromPoint(x, y);
                        while (el && !el.matches('.ecom-g-select, .ecom-g-select-selector')) {
                            el = el.parentElement;
                        }
                        if (el) {
                            let trigger = el.matches('.ecom-g-select-selector') ? el : el.querySelector('.ecom-g-select-selector');
                            if (trigger) trigger.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
                        }
                    }
                """, {"cx": cx, "cy": cy})
    else:
        return {"success": False, "model_value": "", "error": "未拿到型号下拉框中心坐标"}

    time.sleep(1.5)
    snap(page, "model_opened")

    # 方案D：校验真实点击后弹出的下拉框确实是"型号"下拉（与锁定框水平对齐），
    # 避免点到隔壁框（成色/保修方式/品牌）却在错误列表里选型号。校验不过直接判失败，
    # 不让后续滚动遍历在错误列表上静默选错型号。
    verify_open = page.evaluate("""
        (params) => {
            let expectCx = params.cx, expectCy = params.cy;
            let listbox = [...document.querySelectorAll('[role=listbox]')].find(lb => {
                const r=lb.getBoundingClientRect(), s=getComputedStyle(lb);
                return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
            });
            if (!listbox) {
                const pinned=document.querySelector('[data-wb-pin-model="1"]');
                const combobox=pinned && (pinned.querySelector('[role=combobox]') || pinned.querySelector('input'));
                const expanded=(pinned && pinned.getAttribute('aria-expanded')==='true')
                    || (combobox && combobox.getAttribute('aria-expanded')==='true');
                const popup=[...document.querySelectorAll('.ecom-g-select-dropdown,[class*="select-dropdown"]')].find(d => {
                    const r=d.getBoundingClientRect(), s=getComputedStyle(d);
                    return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
                });
                if (expanded || popup) return {ok:true, reason:expanded?'aria-expanded':'visible-dropdown'};
                return { ok: false, reason: '点击后未检测到型号下拉展开状态' };
            }
            let lr = listbox.getBoundingClientRect();
            let lcx = lr.left + lr.width / 2;
            // 关键判定：listbox 中心 x 必须与型号框点击中心 x 水平对齐（同一下拉），
            // 否则就是误点了隔壁框（成色/保修方式/品牌）的下拉，应判失败。
            if (Math.abs(lcx - expectCx) > 160) {
                return { ok: false, reason: '弹出的 listbox 与型号框水平不对齐(可能误点隔壁框): listbox_x=' + Math.round(lcx) + ' 型号框_x=' + Math.round(expectCx) };
            }
            return { ok: true, reason: '' };
        }
    """, {"cx": cx, "cy": cy})
    if not verify_open.get("ok"):
        log("MODEL", f"[方案D] 校验未通过: {verify_open.get('reason')} (点击 x={cx}, y={cy}, method={open_result.get('method')})")
        # A rules-upgrade modal may render only after the first trusted click.
        # That click never opened/searched the model list, so advancing to the
        # next keyword would falsely turn a UI interruption into "model absent"
        # (observed with Z9x: exact `Z9x` was consumed by the modal, then the
        # broader `iQOO Z9x` naturally matched no pure-model option).  Close only
        # a known-safe action and retry this exact keyword once from a fresh pin.
        interrupted = _dismiss_blocking_model_modal(page)
        if retry_same_keyword_after_modal(
                modal_found=interrupted.get("found", False),
                modal_closed=interrupted.get("closed", False),
                retry_count=_modal_retry_count):
            log("MODEL", "规则提示打断了型号框点击；关闭后重试同一关键词 '%s'（不推进到下一关键词）" % keyword)
            return _try_select_model_with_keyword(
                page, keyword, category, brand_y, original_model=original_model,
                model_x=model_x, model_y=model_y, oppo_brand=oppo_brand,
                pure_model=pure_model, brand_cn=brand_cn, brand_en=brand_en,
                brand_x=brand_x, _modal_retry_count=_modal_retry_count + 1,
            )
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return {"success": False, "model_value": "", "error": f"型号框校验失败: {verify_open.get('reason')}"}

    listbox_check = page.evaluate("""
        () => {
            let listbox = [...document.querySelectorAll('[role=listbox]')].find(lb => {
                const r=lb.getBoundingClientRect(), s=getComputedStyle(lb);
                return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
            });
            if (listbox) {
                let rect = listbox.getBoundingClientRect();
                return { found: true, visible: rect.height > 0 };
            }
            return { found: false, visible: false };
        }
    """)

    if not listbox_check.get("found"):
        log("MODEL", "listbox 未出现，尝试备选方案...")
        try:
            all_selects = page.locator('.ecom-g-select').all()
            best_sel = None
            best_dist = 999999
            for sel in all_selects:
                ph = sel.locator('.ecom-g-select-selection-placeholder')
                if ph.count() > 0 and ph.first.inner_text().strip() == "请选择":
                    box = sel.bounding_box()
                    if box and box['y'] > 0:
                        # 优先选择离目标型号框坐标最近的
                        dist = abs(box['x'] - model_x) + abs(box['y'] - model_y) if (model_x >= 0 and model_y >= 0) else box['y']
                        if dist < best_dist:
                            best_dist = dist
                            best_sel = sel
            if best_sel:
                box = best_sel.bounding_box()
                log("MODEL", f"通过 Playwright 点击型号框 (x={int(box['x'])}, y={int(box['y'])})")
                # 同样把型号下拉框打开等待时间限制在 10s（避免 listbox 不存在时等满 30s）
                best_sel.locator('.ecom-g-select-selector').first.click(timeout=10000)
                time.sleep(1.5)
        except Exception as e:
            log("MODEL", f"备选方案失败: {e}")

    # 输入搜索关键字
    # 关键修复：合成事件输入（dispatchEvent）无法可靠触发 rc-select 搜索框的 onChange，
    # 导致输入进去了但搜索过滤没生效，右侧选项仍是未过滤的品牌/默认码（E2/A6...）。
    # 方案0：先 JS 定位搜索框中心坐标，再用真实鼠标点击聚焦 + Playwright keyboard 输入真实按键事件。
    search_method = ""
    try:
        input_coords = page.evaluate("""
            (params) => {
                let modelX = params.modelX, modelY = params.modelY;
                // 找到型号下拉框（离目标坐标最近）
                let selects = document.querySelectorAll('.ecom-g-select');
                let best = null, bestDist = 999999;
                for (let s of selects) {
                    let r = s.getBoundingClientRect();
                    let cx = r.left + r.width / 2, cy = r.top + r.height / 2;
                    let d = Math.abs(cx - modelX) + Math.abs(cy - modelY);
                    if (d < bestDist) { bestDist = d; best = s; }
                }
                if (!best || bestDist > 120) return null;
                let br = best.getBoundingClientRect();
                // rc/ecom select keeps its searchable input inside the select
                // selector (not inside the portal listbox). This is the normal
                // shape used by the Xiaomi model control.
                let selectInput = best.querySelector(
                    'input.ecom-g-select-selection-search-input, input[class*="selection-search-input"], input[role="combobox"]'
                );
                if (selectInput) {
                    let ir = selectInput.getBoundingClientRect();
                    if (ir.width > 0 && ir.height > 0) {
                        return { cx: Math.round(ir.left + ir.width / 2),
                                 cy: Math.round(ir.top + ir.height / 2), source: 'select-input' };
                    }
                }
                // 定位该型号的 listbox（aria-controls 或位置就近）
                let trigger = best.querySelector('.ecom-g-select-selector');
                let ariaControls = trigger ? (trigger.getAttribute('aria-controls') || trigger.getAttribute('aria-owns')) : null;
                let listbox = ariaControls ? document.getElementById(ariaControls) : null;
                if (!listbox) {
                    // 严格就近：listbox 必须与型号框水平对齐（中心 x 接近）且在型号框正下方，
                    // 避免误命中其它行（如成色/电池框 ">85%"）的下拉列表导致选错型号。
                    let bx = br.left + br.width / 2;
                    let all = document.querySelectorAll('[role=listbox]');
                    for (let lb of all) {
                        let r = lb.getBoundingClientRect();
                        let lcx = r.left + r.width / 2;
                        if (r.top >= br.bottom - 40 && r.top < br.bottom + 800
                            && Math.abs(lcx - bx) < 160) {
                            listbox = lb; break;
                        }
                    }
                }
                if (!listbox) return null;
                // 搜索框 input 与 listbox 是兄弟分支（如 .rc-select-dropdown-header），
                // 向上找到 dropdown 根再在整棵子树里找 input。若型号下拉框不可搜索（无 input），
                // 则 input 为 null，跳过输入、交给下方"列表视口匹配"逻辑，绝不抓取页面任意可见 input。
                // 搜索框定位的 listbox 已经过上方"方案D"水平对齐校验（与型号框中心 x 对齐），
                // 只有确实回溯到 dropdown 根容器时，才在根内找 input；否则宁可返回 null，
                // 也绝不抓到标题/成色/保修等其它输入框（避免把型号关键字误写入商品标题）。
                let root = listbox;
                let depth = 0;
                while (root && root.parentElement && root.parentElement !== document.body
                       && depth < 8
                       && !/(dropdown|popup|portal|select-drop|select-dropdown|rc-select)/i.test(root.className || '')) {
                    root = root.parentElement;
                    depth++;
                }
                let input = null;
                let isDropdownRoot = root && /(dropdown|popup|portal|select-drop|select-dropdown|rc-select)/i.test(root.className || '');
                if (isDropdownRoot) {
                    input = root.querySelector('input,textarea');
                }
                if (!input) input = listbox.querySelector('input,textarea');
                if (!input) return null;
                try { input.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
                let r = input.getBoundingClientRect();
                if (r.height <= 0) return null;
                return { cx: Math.round(r.left + r.width / 2), cy: Math.round(r.top + r.height / 2), source: 'dropdown-input' };
            }
        """, {"modelX": (cx if cx > 0 else model_x), "modelY": (cy if cy > 0 else model_y)})
        if not input_coords:
            raise Exception("未定位到搜索框坐标")
        page.mouse.click(input_coords["cx"], input_coords["cy"])
        time.sleep(0.2)
        # 全选清空后再输入（避免旧值 E2 残留）
        page.keyboard.press("Control+a")
        time.sleep(0.1)
        page.keyboard.type(keyword, delay=30)
        time.sleep(0.3)
        # Never press Enter here: when filtering returns no exact option,
        # rc-select commits the first visible row (observed as Redmi 9A for a
        # Redmi Turbo 3 target). Selection happens only after the exact option
        # scanner below returns a trusted coordinate.
        search_method = "real_mouse_keyboard"
        log("MODEL", f"[真实输入] 点击搜索框({input_coords.get('source','unknown')})并键盘输入关键字: '{keyword}'")
        time.sleep(0.5)
        snap(page, "model_input")
    except Exception as e0:
        log("MODEL", f"[真实输入] 型号下拉框无可用搜索框({e0})，禁止滚动遍历并转人工")

    if not search_method:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return {"success": False, "model_value": "", "error": "型号下拉未找到可用搜索输入框"}

    if False:  # 型号下拉框不可搜索：跳过搜索输入回退（Playwright/JS 耗时且易误选其它下拉框），改由下方列表滚动遍历精确选择
        try:
            # 方案1：Playwright locator 直接定位 model 下拉框对应 listbox 内的 input
            search_input = None
            try:
                aria_controls = page.evaluate("""
                    (params) => {
                        let selects = document.querySelectorAll('.ecom-g-select');
                        let best = null, bestDist = 999999;
                        for (let s of selects) {
                            let rect = s.getBoundingClientRect();
                            let cx = rect.left + rect.width / 2;
                            let cy = rect.top + rect.height / 2;
                            let dist = Math.abs(cx - params.modelX) + Math.abs(cy - params.modelY);
                            if (dist < bestDist) { bestDist = dist; best = s; }
                        }
                        if (!best || bestDist > 100) return '';
                        let trigger = best.querySelector('.ecom-g-select-selector');
                        return trigger ? (trigger.getAttribute('aria-controls') || trigger.getAttribute('aria-owns') || '') : '';
                    }
                """, {"modelX": (cx if cx > 0 else model_x), "modelY": (cy if cy > 0 else model_y)})
                if aria_controls:
                    search_input = page.locator(f'[id="{aria_controls}"] input, [aria-controls="{aria_controls}"] input').first
            except Exception:
                search_input = None
            if not search_input:
                search_input = page.locator('[role=listbox] input').first
            search_input.click(force=True, timeout=10000)
            time.sleep(0.3)
            search_input.fill(keyword)
            time.sleep(0.3)
            search_input.press("Enter")
            time.sleep(0.3)
            search_method = "playwright_click_fill"
            log("MODEL", f"[Playwright] locator点击+fill输入关键字: '{keyword}'")
        except Exception as e1:
            log("MODEL", f"Playwright locator 失败({e1})，尝试 JS 聚焦...")
            try:
                # 方案2：JS 找到并聚焦 listbox 内的搜索输入框
                focused = page.evaluate("""
                    (params) => {
                        let modelX = params.modelX;
                        let modelY = params.modelY;
                        let selects = document.querySelectorAll('.ecom-g-select');
                        let bestSelect = null, bestDist = 999999, bestRect = null;
                        for (let s of selects) {
                            let rect = s.getBoundingClientRect();
                            let cx = rect.left + rect.width / 2;
                            let cy = rect.top + rect.height / 2;
                            let dist = Math.abs(cx - modelX) + Math.abs(cy - modelY);
                            if (dist < bestDist) { bestDist = dist; bestSelect = s; bestRect = rect; }
                        }
                        if (!bestSelect || bestDist > 100) return false;
                        let trigger = bestSelect.querySelector('.ecom-g-select-selector');
                        let ariaControls = trigger ? (trigger.getAttribute('aria-controls') || trigger.getAttribute('aria-owns')) : null;
                        let listbox = ariaControls ? document.getElementById(ariaControls) : null;
                        if (!listbox) {
                            let all = document.querySelectorAll('[role=listbox]');
                            for (let lb of all) {
                                let r = lb.getBoundingClientRect();
                                let bx = bestRect.left + bestRect.width / 2;
                                let lcx = r.left + r.width / 2;
                                if (r.top >= bestRect.bottom - 40 && r.top < bestRect.bottom + 800 && Math.abs(lcx - bx) < 160) {
                                    listbox = lb; break;
                                }
                            }
                        }
                        if (!listbox) return false;
                        // 向上找到 dropdown 根节点，再在整棵子树找 input（搜索框与 listbox 是兄弟分支）
                        let root = listbox;
                        while (root && root.parentElement && root.parentElement !== document.body
                               && !/(dropdown|popup|portal|select-drop|select-dropdown)/i.test(root.className || '')) {
                            root = root.parentElement;
                        }
                        let input = root ? root.querySelector('input,textarea') : null;
                        if (!input) input = listbox.querySelector('input,textarea');
                        if (input) {
                            input.focus();
                            input.click();
                            let s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                            s.call(input, '');
                            input.dispatchEvent(new Event('input', {bubbles:true}));
                            return true;
                        }
                        return false;
                    }
                """, {"modelX": (cx if cx > 0 else model_x), "modelY": (cy if cy > 0 else model_y)})
                if focused:
                    time.sleep(0.3)
                    page.keyboard.type(keyword, delay=50)
                    time.sleep(0.3)
                    page.keyboard.press("Enter")
                    time.sleep(0.3)
                    search_method = "js_focus_then_keyboard"
                    log("MODEL", f"[Playwright] JS聚焦+键盘输入关键字: '{keyword}'")
                else:
                    raise Exception("JS聚焦失败")
            except Exception as e2:
                log("MODEL", f"JS聚焦+键盘输入失败({e2})，回退到 JS 全操作...")
                # 方案3：JS 完成全部操作（严格限制在 listbox 内的 input）
                page.evaluate("""
                    (params) => {
                        let keyword = params.keyword;
                        let modelX = params.modelX;
                        let modelY = params.modelY;
                        let selects = document.querySelectorAll('.ecom-g-select');
                        let bestSelect = null, bestDist = 999999, bestRect = null;
                        for (let s of selects) {
                            let rect = s.getBoundingClientRect();
                            let cx = rect.left + rect.width / 2;
                            let cy = rect.top + rect.height / 2;
                            let dist = Math.abs(cx - modelX) + Math.abs(cy - modelY);
                            if (dist < bestDist) { bestDist = dist; bestSelect = s; bestRect = rect; }
                        }
                        if (!bestSelect || bestDist > 100) {
                            console.log('JS全操作: 未找到型号select');
                            return;
                        }
                        let trigger = bestSelect.querySelector('.ecom-g-select-selector');
                        let ariaControls = trigger ? (trigger.getAttribute('aria-controls') || trigger.getAttribute('aria-owns')) : null;
                        let listbox = ariaControls ? document.getElementById(ariaControls) : null;
                        if (!listbox) {
                            let all = document.querySelectorAll('[role=listbox]');
                            for (let lb of all) {
                                let r = lb.getBoundingClientRect();
                                if (r.top >= bestRect.bottom - 20 && r.left >= bestRect.left - 100 && r.left <= bestRect.right + 100) {
                                    listbox = lb;
                                    break;
                                }
                            }
                        }
                        if (!listbox) {
                            console.log('JS全操作: 未找到型号listbox');
                            return;
                        }
                        // 向上找到 dropdown 根节点，再在整棵子树找 input（搜索框与 listbox 是兄弟分支）
                        let root = listbox;
                        while (root && root.parentElement && root.parentElement !== document.body
                               && !/(dropdown|popup|portal|select-drop|select-dropdown)/i.test(root.className || '')) {
                            root = root.parentElement;
                        }
                        let input = root ? root.querySelector('input,textarea') : null;
                        if (!input) input = listbox.querySelector('input,textarea');
                        if (!input) {
                            console.log('JS全操作: 未找到 listbox input，放弃');
                            return;
                        }
                        input.focus();
                        input.click();
                        let s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        s.call(input, '');
                        input.dispatchEvent(new Event('input', {bubbles:true}));
                        for (let c of keyword) {
                            s.call(input, input.value + c);
                            input.dispatchEvent(new KeyboardEvent('keydown', {key:c, bubbles:true}));
                            input.dispatchEvent(new Event('input', {bubbles:true}));
                            input.dispatchEvent(new KeyboardEvent('keyup', {key:c, bubbles:true}));
                        }
                        input.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true}));
                        input.dispatchEvent(new Event('change', {bubbles:true}));
                        input.dispatchEvent(new KeyboardEvent('keyup', {key:'Enter', bubbles:true}));
                    }
                """, {"keyword": keyword, "modelX": (cx if cx > 0 else model_x), "modelY": (cy if cy > 0 else model_y)})
                search_method = "js_type"
                log("MODEL", f"[JS] 逐字符输入关键字: '{keyword}'")

    # 输入完成后停顿并轮询目标选项，确保下拉渲染稳定、脚本不会跳走
    # 用户要求：输入正确的型号后停顿一两秒，让脚本能够扫描读到正确的型号
    # 2026-08-28 补充：非 OPPO 品牌原来只给 2.5s，实测 iQOO Neo9 打完字后下拉
    # 还没刷新出真正匹配的选项（仍是 Neo9S/Neo9S Pro+ 这类半成品列表），2.5s
    # 等不到，判定"型号未匹配"——但型号匹配环节（Excel 标准型号库→抖店属性
    # 型号库两层）其实已经正确解析出目标是 'Neo9'，问题只在下拉渲染没等够。
    # 统一放宽到跟 OPPO 一样的 4s，不改匹配逻辑本身。
    poll_target = (pure_model if (oppo_brand and pure_model) else keyword).lower()
    max_wait = 4.0
    wait_start = time.time()
    target_ready = False
    while time.time() - wait_start < max_wait:
        time.sleep(0.2)
        found = page.evaluate("""
            (params) => {
                let target = params.target.toLowerCase().split(' ').join('');
                let selectors = [
                    'div.ecom-g-select-item', 'div[role="option"]',
                    '.ecom-g-select-dropdown-menu-item',
                    '.ecom-g-select-item-option-content',
                    'div.ant-select-item-option-content',
                    'div.rc-virtual-list-holder-inner > div'
                ];
                for (let sel of selectors) {
                    for (let el of document.querySelectorAll(sel)) {
                        let t = (el.innerText || '').trim().toLowerCase().split(' ').join('');
                        if (t === target) return true;
                    }
                }
                return false;
            }
        """, {"target": poll_target})
        if found:
            target_ready = True
            break
    log("MODEL", f"输入完成，{'已' if target_ready else '未'}扫描到目标选项 '{poll_target}'，等待 {time.time()-wait_start:.1f}s")

    # 选择匹配项（增强选择器，支持多种下拉框结构）
    # 关键修复：不再用 JS dispatchEvent 合成 click（合成事件无法可靠触发 rc-select 的 React
    # onChange，导致下拉框只更新了"显示文本"、而表单受控 value 仍是旧值/空值，下次重渲染就回弹到
    # 默认 E2/空值——这正是"日志看到选对了、前台复查却变回默认值"的根因）。
    # 改为：JS 只负责定位匹配项并返回其中心坐标，由 Python 端用 page.mouse.click 发起"真实可信"
    # 的鼠标点击（isTrusted=true），rc-select 的 onClick/onChange 才会真正触发并写回表单 state。
    select_result = page.evaluate("""
        (params) => {
            let modelKeyword = params.keyword;
            let originalModel = params.originalModel || modelKeyword;

            // 搜索轮询已经确认精确文本出现后，优先直接取该可见选项的中心坐标。
            // 抖店会把品牌与型号弹层同时留在 DOM，不能再依赖 listbox 的父子归属。
            let compactExact=s=>(s||'').toLowerCase().replace(/[\\s()（）]/g,'');
            let wantedExact=compactExact(modelKeyword);
            let exactVisible=[...document.querySelectorAll(
                'div.ecom-g-select-item,div[role="option"],.ecom-g-select-dropdown-menu-item,'+
                '.ecom-g-select-item-option-content,div.ant-select-item-option-content,'+
                'div.rc-virtual-list-holder-inner > div'
            )].filter(el=>{
                let r=el.getBoundingClientRect(), st=getComputedStyle(el);
                return r.width>0&&r.height>0&&st.visibility!=='hidden'&&st.display!=='none'&&
                       compactExact(el.innerText)===wantedExact;
            });
            // 同一精确文本可能同时命中选项容器和文字子节点；统一选择面积最小
            // 的可见节点，不再为单一型号维护业务分支。
            if(exactVisible.length>=1){
                let el=exactVisible[0];
                if(exactVisible.length>1){
                    exactVisible.sort((a,b)=>{
                        let ar=a.getBoundingClientRect(), br=b.getBoundingClientRect();
                        return (ar.width*ar.height)-(br.width*br.height);
                    });
                    el=exactVisible[0];
                }
                let r=el.getBoundingClientRect();
                return Promise.resolve({clicked:true,text:(el.innerText||'').trim(),
                    cx:Math.round(r.left+r.width/2),cy:Math.round(r.top+r.height/2),
                    fuzzy:false,matchLevel:0,aligned:true,scrollAttempts:0,allTexts:[(el.innerText||'').trim()]});
            }

            let selectors = [
                'div.ecom-g-select-item',
                'div[role="option"]',
                '.ecom-g-select-dropdown-menu-item',
                '.ecom-g-select-item-option-content',
                'div.ant-select-item-option-content',
                'div.rc-virtual-list-holder-inner > div'
            ];
            let visibleListboxes=[...document.querySelectorAll('[role=listbox]')].filter(lb=>{
                let r=lb.getBoundingClientRect(); return r.width>0&&r.height>0;
            });
            let activeListbox=null;
            if(visibleListboxes.length){
                // 多个 Select 的弹层可能同时残留在 DOM。精确关键字已出现时，优先选择
                // 真正包含该选项的 listbox，避免误把品牌/CPU 等同列弹层当成手机型号列表。
                let compact=s=>(s||'').toLowerCase().replace(/[\\s()（）]/g,'');
                let wanted=compact(modelKeyword);
                let containing=visibleListboxes.filter(lb=>
                    [...lb.querySelectorAll('[role=option],.ecom-g-select-item')]
                      .some(o=>compact(o.innerText)===wanted)
                );
                let pool=containing.length ? containing : visibleListboxes;
                activeListbox=pool.sort((a,b)=>{
                    let ar=a.getBoundingClientRect(), br=b.getBoundingClientRect();
                    let ad=Math.abs((ar.left+ar.width/2)-params.modelCx)+Math.abs(ar.top-params.modelCy);
                    let bd=Math.abs((br.left+br.width/2)-params.modelCx)+Math.abs(br.top-params.modelCy);
                    return ad-bd;
                })[0];
            }
            function collect(){
                let items=[];
                let scope=activeListbox || document;
                for (let sel of selectors){ for (let el of scope.querySelectorAll(sel)) items.push(el); }
                return items;
            }
            // 把选项滚入视口中央后取中心坐标（虚拟列表选项在视口外时 cy 为负/超界，真实点击会落空）
            function centerOf(el){
                try { el.scrollIntoView({block:'center', inline:'nearest'}); } catch(e){}
                let r = el.getBoundingClientRect();
                return { cx: Math.round(r.left + r.width/2), cy: Math.round(r.top + r.height/2) };
            }
            function isValidContainMatch(text, keyword){
                if (!text || !keyword) return false;
                let idx = text.indexOf(keyword);
                if (idx === -1) return false;
                let after = text.substring(idx + keyword.length);
                if (after && /^[a-zA-Z0-9\\+\\-]/.test(after)) return false;
                return true;
            }
            // 容错核心：折叠所有空白（解决平台下拉选项"漏掉缩进/缺空格"导致
            // '畅享 70X 活力版' 与后台实际 '畅享70X活力版'(无空格) 无法精确匹配的问题）。
            function norm(s){ s=(s||'').toLowerCase(); return s.split(' ').join('').split('(').join('').split(')').join('').split('（').join('').split('）').join('').split(String.fromCharCode(9)).join('').split(String.fromCharCode(10)).join('').split(String.fromCharCode(13)).join(''); }
            // 在 items 中按优先级找最佳匹配（全部按 norm 折叠空白比较）。
            // aligned: 候选是否对齐完整目标型号(originalModel)——即候选以完整目标型号开头，
            //          避免出现 "畅享 70X 活力版" 被截断关键字 "畅享 70X" 误匹配成 "畅享 70X 尊享版"
            //          这种"兄弟变体"（平台漏缩进时，规范缩进项先被命中）。对齐项优先；非对齐项
            //          仅作最后兜底，并继续滚动寻找真正对齐完整目标的选项。
            function findBest(items){
                let nk = norm(modelKeyword);
                // 对齐基准：同时接受"带品牌完整目标"与"剥品牌后的纯型号目标"。
                // 平台下拉选项写法不一：华为机型选项常为纯型号(nova 14 活力版，无品牌、括号变空格)，
                // 而另一些品牌选项带品牌前缀(HUAWEI nova 14 (活力版))。只要求"以完整纯型号开头"
                // 即可判定对齐，既避免把纯型号选项误判为兄弟变体拒绝，又保留后缀截断拦截
                // （如 畅享70X活力版 不会被 畅享70X尊享版 误中，因为后者不以纯型号开头）。
                let noFull = norm(originalModel || modelKeyword);
                let noPure = norm(params.pureModel || originalModel || modelKeyword);
                function alignedWith(t){ return t.startsWith(noFull) || t.startsWith(noPure); }
                // 官方完整名称优先，不受当前短关键字影响。
                for (let d of items){ let t=norm(d.innerText); if (t===noFull || t===noPure) return {el:d, matchLevel:0, aligned:true}; }
                // L1: 完全相等(norm) 且 对齐完整目标（避免 "畅享 70X" 这类通用前缀项被当成对齐命中）
                for (let d of items){ let t=norm(d.innerText); if (t===nk && alignedWith(t)) return {el:d, matchLevel:1, aligned:true}; }
                // L2: 以keyword开头(norm) 且 对齐完整目标型号
                let pre=[];
                for (let d of items){ let t=norm(d.innerText); if (t && t.startsWith(nk) && alignedWith(t)) pre.push({el:d,len:t.length, aligned:true}); }
                if (pre.length){ pre.sort((a,b)=>b.len-a.len); return {el:pre[0].el, matchLevel:2, aligned:true}; }
                // L2-soft: 以keyword开头但不对齐完整目标(兄弟变体，如 尊享版 vs 活力版) —— 仅作最后兜底，不立即点击
                let soft=[];
                for (let d of items){ let t=norm(d.innerText); if (t && t.startsWith(nk) && !alignedWith(t)) soft.push({el:d,len:t.length, aligned:false}); }
                if (soft.length){ soft.sort((a,b)=>b.len-a.len); return {el:soft[0].el, matchLevel:2, aligned:false}; }
                // L3: keyword以text开头(norm)
                let kpre=[];
                for (let d of items){ let t=norm(d.innerText); if (t && (noFull===t||noPure===t||noFull.startsWith(t)||noPure.startsWith(t))) kpre.push({el:d,len:t.length}); }
                if (kpre.length){ kpre.sort((a,b)=>b.len-a.len); return {el:kpre[0].el, matchLevel:3}; }
                // L4: 有效包含(norm)
                for (let d of items){ let t=norm(d.innerText); if (isValidContainMatch(t, nk)) return {el:d, matchLevel:4}; }
                for (let d of items){ let t=norm(d.innerText); if ((noFull.includes(t)||noPure.includes(t)) && t.length>=noPure.length*0.6){ return {el:d, matchLevel:4}; } }
                // L5: 模糊(小写 norm)
                let kl=nk.toLowerCase();
                for (let d of items){ let t=norm(d.innerText).toLowerCase(); if (t===kl) return {el:d, matchLevel:5, fuzzy:true}; if (t.startsWith(kl)) return {el:d, matchLevel:5, fuzzy:true}; }
                return null;
            }
            let allTextsSeen=[];
            // Deterministic virtual-list traversal: scope to the model listbox,
            // move its actual scrollable descendant in fixed viewport steps,
            // and accept only the full official model after normalization.
            async function run(){
                let scope=activeListbox || document;
                let candidates=activeListbox ? [activeListbox,...activeListbox.querySelectorAll('*')] : [];
                // Some implementations put the scroll holder one level above role=listbox.
                let parent=activeListbox ? activeListbox.parentElement : null;
                for(let i=0;i<4&&parent;i++,parent=parent.parentElement) candidates.push(parent);
                let holders=candidates.filter(h=>{
                    let r=h.getBoundingClientRect();
                    return r.width>0&&r.height>0&&h.scrollHeight>h.clientHeight+10;
                });
                holders.sort((a,b)=>(b.scrollHeight-b.clientHeight)-(a.scrollHeight-a.clientHeight));
                let holder=holders[0] || null;
                if(holder) holder.scrollTop=0;
                let previousTexts='';
                for(let attempt=0;attempt<300;attempt++){
                    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
                    let items=collect();
                    for (let d of items){ let t=d.innerText.trim(); if (t && allTextsSeen.indexOf(t)<0) allTextsSeen.push(t); }
                    let best=findBest(items);
                    // 完整原始型号或当前官方映射关键词精确相等时立即选择。
                    // 例如标题为 Redmi Note 14，而平台官方选项为 Note 14 5G：
                    // 强制映射已把 keyword 设为 Note 14 5G，L1 就是确定性的精确命中，
                    // 不应继续滚动后再误报“未找到”。兄弟变体仍因 aligned=false 被拦截。
                    if (best && best.matchLevel<=1 && best.aligned){
                        let c=centerOf(best.el);
                        return Object.assign({clicked:true, text:(best.el.innerText||'').trim(), cx:c.cx, cy:c.cy, allTexts:allTextsSeen}, {fuzzy:!!best.fuzzy, matchLevel:best.matchLevel, aligned:true, scrollAttempts:attempt});
                    }
                    if(!holder || holder.scrollTop+holder.clientHeight>=holder.scrollHeight-2) break;
                    let before=holder.scrollTop;
                    let step=Math.max(120,Math.floor(holder.clientHeight*0.8));
                    holder.scrollTop=Math.min(holder.scrollTop+step,holder.scrollHeight-holder.clientHeight);
                    holder.dispatchEvent(new Event('scroll',{bubbles:true}));
                    if(holder.scrollTop===before) break;
                }
                return {clicked:false,error:holder?'虚拟列表遍历完成，未找到官方精确型号':'未找到型号列表滚动容器',allTexts:allTextsSeen};
            }
            return run();
        }
    """, {"keyword": keyword, "originalModel": original_model,
          "oppoBrand": oppo_brand, "pureModel": pure_model,
          "modelCx": (cx if cx > 0 else model_x),
          "modelCy": (cy if cy > 0 else model_y)})

    if not select_result.get("clicked"):
        allTexts = select_result.get("allTexts", [])
        log("MODEL", f"未找到匹配型号 '{keyword}'")
        if allTexts:
            log("MODEL", f"下拉框所有选项(前20): {allTexts[:20]}")
        page.keyboard.press("Escape")
        return {"success": False, "model_value": "", "error": f"未找到匹配型号 '{keyword}'"}

    selected_text = select_result.get("text", "")
    fuzzy = select_result.get("fuzzy", False)
    aligned = select_result.get("aligned", True)
    log("MODEL", f"{'模糊' if fuzzy else '精确'}{(not aligned) and ' [兄弟变体兜底-未对齐完整目标]' or ''}匹配选择: '{selected_text}'")
    if not aligned:
        log("MODEL", f"⚠ 型号容错兜底：选中 '{selected_text}' 与完整目标 '{original_model or keyword}' 后缀不一致（疑似平台下拉漏缩进导致兄弟变体先命中），终检会拦截此商品")
    if oppo_brand and select_result.get("optionDetails"):
        details = select_result.get("optionDetails", [])
        log("MODEL", f"OPPO 下拉选项明细（共{len(details)}个）: {details[:20]}")
        if select_result.get("oppoPrefixedPick"):
            log("MODEL", f"OPPO 未找到纯型号选项，当前点击的是带前缀选项 '{selected_text}'，后续尝试改写受控值为纯型号 '{pure_model}'")

    # 关键修复：用真实可信的鼠标点击（isTrusted=true）触发 rc-select 的 onClick/onChange，
    # 这样表单受控 value 才会真正被写成目标型号，而不是只更新下拉框的显示文本（合成 click 的坑）。
    cx = select_result.get("cx")
    cy = select_result.get("cy")
    # 坐标必须落在视口内才可信；rc-virtual-list 里未渲染/被隐藏的选项可能返回 (0,0) 或负值。
    viewport = page.viewport_size
    vw = viewport.get("width", 1920) if viewport else 1920
    vh = viewport.get("height", 1080) if viewport else 1080
    coord_valid = (cx is not None and cy is not None and cx > 0 and cy > 0 and cx < vw and cy < vh)

    # 如果首次坐标无效（常见于 rc-virtual-list 异步渲染），再专门等 150ms 重新测量一次。
    if not coord_valid and selected_text:
        retry_coords = page.evaluate("""
            async (params) => {
                let text = params.text;
                let selectors = [
                    'div.ecom-g-select-item', 'div[role="option"]',
                    '.ecom-g-select-dropdown-menu-item',
                    '.ecom-g-select-item-option-content',
                    'div.ant-select-item-option-content',
                    'div.rc-virtual-list-holder-inner > div'
                ];
                for (let sel of selectors) {
                    for (let el of document.querySelectorAll(sel)) {
                        if ((el.innerText || '').trim() === text) {
                            try { el.scrollIntoView({block:'center', inline:'nearest'}); } catch(e){}
                            await new Promise(r => setTimeout(r, 150));
                            let r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                return { cx: Math.round(r.left + r.width/2), cy: Math.round(r.top + r.height/2) };
                            }
                        }
                    }
                }
                return null;
            }
        """, {"text": selected_text})
        if retry_coords:
            cx = retry_coords.get("cx")
            cy = retry_coords.get("cy")
            coord_valid = (cx is not None and cy is not None and cx > 0 and cy > 0 and cx < vw and cy < vh)
            if coord_valid:
                log("MODEL", f"[坐标重测] 选项 '{selected_text}' 重测后坐标有效: x={cx}, y={cy}")
    if coord_valid:
        try:
            page.mouse.click(cx, cy)
            log("MODEL", f"[真实点击] 在选项中心 (x={cx}, y={cy}) 发起可信鼠标点击: '{selected_text}'")
        except Exception as e:
            log("MODEL", f"[真实点击] 失败({e})，放弃本次关键字: '{selected_text}'")
            page.keyboard.press("Escape")
            return {"success": False, "model_value": "", "error": f"真实点击选项失败: {e}"}
    else:
        # 拿不到有效坐标 = 选项未真正渲染到 DOM/视口内，合成 click 极大概率点错位置。
        # 以前这里用合成 click 兜底，导致点中"店铺保修"等无关下拉框并谎报成功，必须拒绝。
        log("MODEL", f"⚠ 未拿到有效选项中心坐标 (cx={cx}, cy={cy})，拒绝合成 click 兜底，视为选择失败: '{selected_text}'")
        page.keyboard.press("Escape")
        return {"success": False, "model_value": "", "error": f"未拿到有效选项坐标: cx={cx}, cy={cy}"}
    time.sleep(1)

    # 验证：坐标无关地读取型号框真实值——用"品牌文本→右侧同行型号框"重新定位并读值，
    # 不依赖坐标包含法（旧做法在跨商品坐标漂移/相邻 select 重叠时，会误读 '>85%'/'店铺保修'
    # 等无关框，导致正确选中的型号被判失败、继而退化关键字误选错误型号）。
    verify = page.evaluate("""
        (params) => {
            let brandCn = params.brandCn || '';
            let brandEn = (params.brandEn || '').toLowerCase();
            let modelX = params.modelX, modelY = params.modelY;
            let selects = document.querySelectorAll('.ecom-g-select');
            // 优先：品牌文本空间关系定位型号框，读其真实值（坐标无关）
            let brandBox = null;
            for (let s of selects) {
                let sel = s.querySelector('.ecom-g-select-selection-item');
                let text = sel ? (sel.title || sel.innerText || '').trim() : '';
                if (!text) continue;
                let lc = text.toLowerCase();
                if (text.includes(brandCn) || (brandEn && lc.includes(brandEn))) { brandBox = s; break; }
            }
            if (brandBox) {
                let br = brandBox.getBoundingClientRect();
                let bx = br.left, by = br.top;
                let sameRowRight = [];
                for (let s of selects) {
                    let r = s.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.left > bx && Math.abs(r.top - by) <= 12) sameRowRight.push(s);
                }
                sameRowRight.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                if (sameRowRight.length) {
                    let sel = sameRowRight[0].querySelector('.ecom-g-select-selection-item');
                    let val = sel ? (sel.title || sel.innerText || '').trim() : '';
                    if (val) return { verified: true, value: val, dist: 0, method: 'spatial' };
                }
            }
            // 兜底：坐标包含法（仅当品牌文本定位失败）
            let containing = null, best = null, bestDist = 999999;
            for (let s of selects) {
                let rect = s.getBoundingClientRect();
                let x = Math.round(rect.left), y = Math.round(rect.top);
                let sel = s.querySelector('.ecom-g-select-selection-item');
                let text = sel ? (sel.title || sel.innerText || '').trim() : '';
                if (modelX >= 0 && modelY >= 0 && rect.width > 0 && rect.height > 0 &&
                    x <= modelX && modelX <= x + rect.width && y <= modelY && modelY <= y + rect.height) {
                    if (text) containing = text;
                }
                if (text) {
                    if (modelX >= 0 && modelY >= 0) {
                        let dist = Math.abs(x - modelX) + Math.abs(y - modelY);
                        if (dist < bestDist) { bestDist = dist; best = text; }
                    } else if (!best) best = text;
                }
            }
            if (containing) return { verified: true, value: containing, dist: 0, method: 'coord' };
            if (best) return { verified: true, value: best, dist: bestDist, method: 'coord' };
            return { verified: false, value: '', dist: -1 };
        }
    """, {"modelX": model_cx, "modelY": model_cy, "brandCn": brand_cn, "brandEn": brand_en})

    if verify.get("verified"):
        log("MODEL", f"型号选择验证成功: '{verify['value']}'")

        # 关键修复：校验值必须落在目标型号框坐标内，或明显与期望型号相关。
        # 以前当点击失败（如坐标无效导致合成 click 点错）时，verify 会读到邻近的
        # "店铺保修" 等无关 select，代码却"回退使用点击值"，造成"日志显示选中 Reno12、
        # 实际型号框仍是 E2"的致命谎言。现在：如果校验值与点击选项明显不一致且不是
        # 目标型号框内的值，直接判定失败。
        model_value_out = verify["value"]
        dist = verify.get("dist", 999999)
        if selected_text and verify["value"] and \
           selected_text not in verify["value"] and verify["value"] not in selected_text:
            if dist == 0:
                # 坐标框内值与点击选项不同：可能是平台格式化差异，保留框内真实值
                log("MODEL", f"⚠ 校验值 '{verify['value']}' 与点击选项 '{selected_text}' 不同，但来自目标型号框坐标内，以框内真实值为准")
                model_value_out = verify["value"]
            else:
                log("MODEL", f"⚠ 校验值 '{verify['value']}' 与点击选项 '{selected_text}' 不一致且不是目标型号框内值，视为选择失败")
                page.keyboard.press("Escape")
                return {"success": False, "model_value": "", "error": f"校验值被其它 select 干扰: {verify['value']}"}

        # 二次确认：坐标无关地重读型号框真实值（同 verify 逻辑）
        time.sleep(0.8)
        verify2 = page.evaluate("""
            (params) => {
                let brandCn = params.brandCn || '';
                let brandEn = (params.brandEn || '').toLowerCase();
                let modelX = params.modelX, modelY = params.modelY;
                let selects = document.querySelectorAll('.ecom-g-select');
                let brandBox = null;
                for (let s of selects) {
                    let sel = s.querySelector('.ecom-g-select-selection-item');
                    let text = sel ? (sel.title || sel.innerText || '').trim() : '';
                    if (!text) continue;
                    let lc = text.toLowerCase();
                    if (text.includes(brandCn) || (brandEn && lc.includes(brandEn))) { brandBox = s; break; }
                }
                if (brandBox) {
                    let br = brandBox.getBoundingClientRect();
                    let bx = br.left, by = br.top;
                    let sameRowRight = [];
                    for (let s of selects) {
                        let r = s.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        if (r.left > bx && Math.abs(r.top - by) <= 12) sameRowRight.push(s);
                    }
                    sameRowRight.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                    if (sameRowRight.length) {
                        let sel = sameRowRight[0].querySelector('.ecom-g-select-selection-item');
                        let val = sel ? (sel.title || sel.innerText || '').trim() : '';
                        if (val) return { verified: true, value: val, dist: 0, method: 'spatial' };
                    }
                }
                let containing = null, best = null, bestDist = 999999;
                for (let s of selects) {
                    let rect = s.getBoundingClientRect();
                    let x = Math.round(rect.left), y = Math.round(rect.top);
                    let sel = s.querySelector('.ecom-g-select-selection-item');
                    let text = sel ? (sel.title || sel.innerText || '').trim() : '';
                    if (modelX >= 0 && modelY >= 0 && rect.width > 0 && rect.height > 0 &&
                        x <= modelX && modelX <= x + rect.width && y <= modelY && modelY <= y + rect.height) {
                        if (text) containing = text;
                    }
                    if (text) {
                        if (modelX >= 0 && modelY >= 0) {
                            let dist = Math.abs(x - modelX) + Math.abs(y - modelY);
                            if (dist < bestDist) { bestDist = dist; best = text; }
                        } else if (!best) best = text;
                    }
                }
                if (containing) return { verified: true, value: containing, dist: 0, method: 'coord' };
                if (best) return { verified: true, value: best, dist: bestDist, method: 'coord' };
                return { verified: false, value: '', dist: -1 };
            }
        """, {"modelX": model_cx, "modelY": model_cy, "brandCn": brand_cn, "brandEn": brand_en})

        # 二次确认同样禁止用 selected_text 兜底，只相信目标型号框坐标内的真实值。
        v2 = verify2.get("value") if verify2.get("verified") else ""
        v2_dist = verify2.get("dist", 999999) if verify2.get("verified") else -1
        if selected_text and v2 and selected_text not in v2 and v2 not in selected_text:
            if v2_dist == 0:
                log("MODEL", f"⚠ 二次确认值 '{v2}' 与点击选项 '{selected_text}' 不同，但来自目标型号框坐标内，以框内真实值为准")
            else:
                log("MODEL", f"⚠ 二次确认值 '{v2}' 与点击选项 '{selected_text}' 不一致且不是目标型号框内值，视为选择失败")
                page.keyboard.press("Escape")
                return {"success": False, "model_value": "", "error": f"二次确认被其它 select 干扰: {v2}"}
        if v2 and v2 == model_value_out:
            log("MODEL", f"型号选择二次确认稳定: '{v2}'")
            _ensure_title_suffix(page, category)
            return {"success": True, "model_value": model_value_out, "error": ""}
        else:
            log("MODEL", f"⚠ 型号选择后值回弹: 第一次 '{model_value_out}' -> 二次 '{v2}'，将用下一关键字重试")
            page.keyboard.press("Escape")
            return {"success": False, "model_value": "", "error": f"型号选择后值回弹: {v2}"}
    else:
        log("MODEL", "型号选择后未验证到值，视为选择失败")
        page.keyboard.press("Escape")
        return {"success": False, "model_value": "", "error": "型号选择后未验证到值"}


def _strip_brand_tokens(text: str, brand_cn: str, brand_en: str) -> str:
    """去掉文本中的品牌名（中英文），并把空白压缩为单个空格。

    用途：抖店型号下拉选项常带品牌前缀（如 "真我GT Pro"），而 parse_title
    解析出的 model 一般不含品牌（如 "GT Pro"）。型号选择校验前先剥离品牌名，
    才能正确判断：
      - "真我GT Pro" 与 "GT Pro" 是完整匹配（应接受）
      - "真我GT"    是 "GT Pro" 的截断（应拒绝，避免漏掉 Pro 后缀）
    """
    if not text:
        return text
    t = text
    for tok in (brand_cn, brand_en):
        if tok:
            # 大小写不敏感地剥离品牌 token：
            # 下拉选项常为全大写品牌前缀（如 "HUAWEI P60 Pro"），而解析出的 brand_en 可能是 "Huawei"，
            # 若区分大小写会漏剥，导致归一化后比对失败（华为/OPPO 等都会受影响）。
            t = re.sub(re.escape(tok), ' ', t, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', t).strip()


def _model_core_token(model_norm: str) -> str:
    """从归一化型号里提取"核心标识 token"：优先取带数字的最特异 token（如 Flip7/Fold8/W26/70X），
    用于型号匹配时区分'同家族不同型号'。拿不到则返回空串（调用方据此跳过此守卫）。"""
    if not model_norm:
        return ""
    # 归一化：转小写、折叠空白、去掉常见包裹符号，便于按 token 切分
    s = model_norm.lower()
    s = re.sub(r'[\s()（）/／\-—_]+', ' ', s).strip()
    if not s:
        return ""
    tokens = [t for t in s.split(' ') if t]
    if not tokens:
        return ""
    # 优先：包含数字的 token（型号号段最具区分度）
    for t in tokens:
        if re.search(r'\d', t):
            return t
    # 退化：取最后一个 token（通常是型号主体）
    return tokens[-1]


def _norm_model_loose(model: str) -> str:
    """型号宽松归一化：去空格、去括号、忽略大小写、折叠空白。

    用于终检容错：平台编辑页下拉常把规范命名里的空格/缩进吞掉（如 'Pura 90 Pro'
    显示为 'Pura90 Pro'），或大小写不一致，或括号写法不同（如 'nova 14 (活力版)'
    显示为 'nova 14 活力版'）。这类"内容一致仅排版不同"应放行，避免把正确商品误判拦截。
    仅折叠空格/大小写/括号，不改动任何字符顺序，因此 'Pura90 Pro' 与 'Pura 90 Pro'
    等价、'nova 14 (活力版)' 与 'nova 14 活力版' 等价，但 'Galaxy Z Flip7' 与
    'Galaxy Z Fold7' 仍不等价（差异 token 被保留）。
    """
    if not model:
        return ""
    s = model.lower()
    s = re.sub(r'\s+', '', s)
    s = s.replace('(', '').replace(')', '').replace('（', '').replace('）', '')
    return s


def _is_acceptable_model_match(selected: str, model: str, brand_cn: str, brand_en: str) -> tuple:
    """判断下拉选项 selected 是否可作为 model 的合法匹配。

    返回 (acceptable: bool, reason: str)。

    规则（均在剥离品牌前缀后比较，避免 "真我GT Pro" 因带品牌而击穿截断校验）：
      1) 归一化后完全相等 → 接受（含 "真我GT Pro" == "GT Pro"）
      2) model 归一化后是 selected 归一化后的前缀 → selected 比 model 更长，
         属于扩展匹配（如 model="GT", selected="真我GT Pro"）→ 拒绝
      3) selected 归一化后是 model 归一化后的前缀 → selected 比 model 更短，
         属于截断匹配（如 selected="真我GT", model="GT Pro"，丢失 Pro）→ 拒绝
      4) 其余（互不为前后缀）→ 接受（维持原有宽松行为）
    """
    sel_norm = _strip_brand_tokens(selected, brand_cn, brand_en)
    mod_norm = _strip_brand_tokens(model, brand_cn, brand_en)

    if sel_norm == mod_norm:
        return True, "型号完全匹配"

    if mod_norm.startswith(sel_norm) and len(mod_norm) > len(sel_norm):
        diff = mod_norm[len(sel_norm):].strip()
        if diff:
            return False, (f"选中型号 '{selected}'(归一化'{sel_norm}') 是 '{model}'(归一化"
                           f"'{mod_norm}') 的截断匹配，差异后缀: '{diff}'")

    if sel_norm.startswith(mod_norm) and len(sel_norm) > len(mod_norm):
        after = sel_norm[len(mod_norm):].strip()
        if after:
            return False, (f"选中型号 '{selected}'(归一化'{sel_norm}') 可能是 '{model}'(归一化"
                           f"'{mod_norm}') 的扩展匹配，后续: '{after}'")

    # 规则 4（兜底收紧）：互不为前后缀时，绝大多数是"同家族不同型号"（如 'Galaxy Z Flip7'
    # 误匹配 'Galaxy Z Fold8 Ultra'，仅共享 'Galaxy Z' 前缀）。除非选中项包含目标型号的
    # 核心标识 token（带数字的最特异 token，如 Flip7 / Fold8 / W26 / 70X），否则拒绝，
    # 避免把明显不同的型号当宽松匹配放过。核心 token 在选中项里则视为可接受变体。
    core = _model_core_token(mod_norm)
    if core and core not in sel_norm.lower():
        return False, (f"选中型号 '{selected}'(归一化'{sel_norm}') 与目标 '{model}'(归一化"
                       f"'{mod_norm}') 互不为前后缀，且缺失核心标识 '{core}'，疑似不同型号，拒绝")
    return True, "型号宽松匹配"


def _model_has_upgrade_field(model: str) -> bool:
    """型号是否带 Pro/Ultra/Max 等升级字段（作为独立词出现，避免误判如 apro 等）。"""
    if not model:
        return False
    return bool(re.search(r'(?:^|[^a-z])(pro|ultra|max)(?:$|[^a-z])', model, re.IGNORECASE))


def _selected_retains_upgrade(selected: str, model: str, brand_cn: str, brand_en: str) -> bool:
    """selected（剥离品牌前缀后）是否仍包含 model 的全部升级字段(Pro/Ultra/Max)。

    用于全局拦截"丢失升级字段的截断匹配"：例如 model='K90 Pro Max' 却匹配到
    selected='K90 Pro'（丢失 Max），即使 _is_acceptable_model_match 因边界情况放行，
    这里也强制拒绝，避免把 Pro Max 机型误选成 Pro 基础款。所有品牌通用。
    """
    mod_norm = _strip_brand_tokens(model, brand_cn, brand_en)
    sel_norm = _strip_brand_tokens(selected, brand_cn, brand_en)
    if not mod_norm:
        return True
    for field in ("pro", "ultra", "max"):
        pat = r'(?:^|[^a-z])' + field + r'(?:$|[^a-z])'
        if re.search(pat, mod_norm, re.IGNORECASE) and not re.search(pat, sel_norm, re.IGNORECASE):
            return False
    return True


def _model_has_suffix_field(model: str) -> bool:
    """型号是否带 Pro/Ultra/Max/+ 等后缀/升级字段。

    用于 OPPO 钉死逻辑和发布前终检：
      - 目标型号不带后缀时（如 Find X7），实际选中值也绝不能带后缀，
        防止平台把 Find X7 自动"升级"成 Find X7 Pro。
      - 目标型号本身带后缀时（如 Find X7 Pro），保留原逻辑，不降级。

    判定字段：
      - Pro / Ultra / Max（独立词）
      - + 号
      - 单字母后缀 i / z / x / s：紧跟数字且在末尾（如 Find X7s, Reno8 Z, Note 12i）
      - 中文版本后缀：至尊版 / 竞速版 / 活力版 / 元气版
    """
    if not model:
        return False
    # Pro / Ultra / Max（独立词，避免误伤 apro 等）
    if re.search(r'(?:^|[^a-z])(pro|ultra|max)(?:$|[^a-z])', model, re.IGNORECASE):
        return True
    # 中文版本后缀
    if re.search(r'至尊版|竞速版|活力版|元气版', model):
        return True
    # + 号
    if '+' in model:
        return True
    # 单字母后缀 i/z/x/s：紧跟在数字后面（允许空格），且位于字符串末尾
    # 例如：Find X7s, Reno8 Z, Note 12i
    if re.search(r'\d\s*[izxs]$', model, re.IGNORECASE):
        return True
    return False


def _is_oppo_brand(brand_cn: str, brand_en: str) -> bool:
    """判断是否为 OPPO 品牌（中英文任意一种命中即算）。"""
    if not brand_cn and not brand_en:
        return False
    for tok in (brand_cn, brand_en):
        if tok and tok.strip().upper() == "OPPO":
            return True
    return False


def extract_oppo_pure_model(full_title: str, parsed_model: str = "") -> str:
    """OPPO 专用：从草稿全名剔除 成色/品牌名/商品类目，得到纯型号。

    例: 【95新】OPPO OPPO Find X9s Pro 二手手机 -> Find X9s Pro

    严格只移除"明确的成色/品牌/类目词"，绝不删除型号关键字：
    保留 Pro/Ultra/Max/+/z/i/s/x/至尊版/活力版/竞速版/元气版 等。
    若剔除后为空或异常，回退 parsed_model（parse_title 已正确解析的型号）。
    """
    if not full_title:
        return parsed_model
    t = full_title.strip()
    # 1) 成色：移除【..】方括号内容（多为成色/营销前缀）
    t = re.sub(r'【[^】]*】', ' ', t)
    # 2) 成色：常见成色词（独立词，避免误删型号）
    for cond in ('99新', '95新', '9新', '8成新', '9成新', '充新'):
        t = re.sub(r'\s*' + re.escape(cond) + r'\s*', ' ', t)
    # 3) 品牌名 OPPO（大小写不敏感，全部出现，含重复 "OPPO OPPO"）
    t = re.sub(r'\bOPPO\b', ' ', t, flags=re.IGNORECASE)
    # 二次兜底：去掉无词边界的 "OPPO"（如 "OPPOFind" 连写），品牌不可能出现在型号中间
    t = re.sub(r'(?i)OPPO', ' ', t)
    # 4) 商品类目/通用词（仅移除明确类目词，不碰型号关键字）
    cat_words = ['二手手机', '二手', '手机', '国行', '正品', '官方', '原装',
                 '套装', '单品', '批发', '零售', '拆机', '整机', '配件',
                 '3C数码', '3C', '数码', '通讯', '智能', '旗舰']
    for w in cat_words:
        if w in t:
            t = t.replace(w, ' ')
    # 4.5) 补两个 SEO_KEYWORD_PATTERNS 里已有、但本函数独立维护的 cat_words 没有
    # 的噪声词："严选优品"卖点词、"-Z"/"机-Z"内部渠道尾标——曾导致 OPPO 强制写入
    # 拿到 "Find X9 -Z"/"Find X8s 严选优品 机-Z" 这种带尾巴的脏文本去匹配下拉框，
    # 永远匹配不上。只搬这两条，不整体复用 SEO_KEYWORD_PATTERNS/_strip_title_noise：
    # 那边还有 \b5G\b/\b4G\b 等模式，对 OPPO A3 (5G) 这类"5G 是型号一部分"的机型
    # 会被误删（该场景在通用路径靠 _5G_MODEL_PROTECT 事后恢复，本函数没有那套机制）。
    # 放在 cat_words 循环之后：先让"二手手机"作为整体被替换掉，避免"机"字被
    # -Z 尾标模式提前吞掉后，"二手手机"退化成"二手手"这种残缺片段匹配不上。
    t = re.sub(r'\s*严选优品\s*', ' ', t)
    t = re.sub(r'\s*(?:机\s*)?-[Zz]\s*$', '', t)
    # 5) 压缩空白
    t = re.sub(r'\s+', ' ', t).strip()
    # 6) 兜底：剔除后为空或过短，回退解析型号
    if not t or len(t) < 2:
        return parsed_model
    return t


# 最近一次成功定位的型号下拉框坐标（由 select_model 写入），供发布前终检复用，
# 避免再次依赖品牌/型号标签去猜哪个 select 是型号框。
SELECTED_MODEL_XY: dict = {"x": -1, "y": -1}


def _rewrite_oppo_model_value(page, model_x: int, model_y: int, pure_model: str, old_value: str = "") -> bool:
    """OPPO 专用修复：把型号下拉框（.ecom-g-select）的受控值直接改成纯型号。

    背景：OPPO 的型号下拉选项（或草稿箱预填）常是 "OPPO Reno15 Pro" 这种带品牌前缀的值，
    而抖店"型号"标品库只认纯型号（如 "Reno15 Pro"）。通用校验 _is_acceptable_model_match
    在剥离前缀后能匹配通过，但下拉框受控 value 仍是带前缀的值，平台发布校验会拒绝。

    根因（关键）：
      .ecom-g-select 是 React 受控组件，平台读取的是 React state 中的 value，而非 DOM 显示文本。
      此前的 DOM 改写（selection-item 文本 / input.value）并不会改变提交时真正使用的 value。

    本轮修正（针对日志里出现 E2 / 受控值变空的问题）：
      不再"调到一个不抛错的 onChange 就返回"，而是对每个候选句柄：
        1) 先用函数签名过滤掉明显的事件处理器（单参数 e/event）；
        2) 调用后等待 React 渲染，再读真正的受控值；
        3) 只有受控值确实变成纯型号，才算成功；
        4) 如果调用后值被改成空/E2 等异常值，继续尝试下一个候选；
        5) 全部失败后，尝试把值恢复到 old_value，避免留下脏状态。
      仅针对 OPPO 调用；其它品牌逻辑完全不变。
    """
    try:
        res = page.evaluate(r"""
            async (params) => {
                let modelX = params.modelX, modelY = params.modelY, pure = params.pure, oldVal = params.oldVal || '';

                function locateSelect(){
                    // 优先使用 pin 标记的元素（不受滚动影响）
                    let pinned = document.querySelector('[data-wb-pin-model="1"]');
                    if(pinned){
                        let r=pinned.getBoundingClientRect();
                        if(r.width > 0 && r.height > 0) return {best:pinned, bestDist:0};
                    }
                    let selects = document.querySelectorAll('.ecom-g-select');
                    if(oldVal){
                        for(let s of selects){
                            let item=s.querySelector('.ecom-g-select-selection-item');
                            let t=item ? ((item.title || item.innerText || '').trim()) : '';
                            if(t && t === oldVal) return {best:s, bestDist:0};
                        }
                    }
                    let best=null, bestDist=999999;
                    for(let s of selects){
                        let r=s.getBoundingClientRect();
                        let cx=Math.round(r.left + r.width / 2), cy=Math.round(r.top + r.height / 2);
                        let d=Math.abs(cx - modelX) + Math.abs(cy - modelY);
                        if(d < bestDist){ bestDist=d; best=s; }
                    }
                    return {best, bestDist};
                }
                let loc = locateSelect(), best = loc.best;
                if(!best || loc.bestDist > 300) return {ok:false, reason:'select-not-found', dist:loc.bestDist};

                function getFiber(el){
                    for(let k in el){
                        if(k.startsWith('__reactInternalInstance$') || k.startsWith('__reactFiber$')) return el[k];
                    }
                    return null;
                }
                let fiber = getFiber(best);
                if(!fiber) return {ok:false, reason:'no-fiber', dist:loc.bestDist};

                function typeName(cur){
                    if(!cur || !cur.type) return '';
                    let t = cur.type;
                    return (t.name || '') + (t.displayName || '');
                }
                function isSelectFiber(cur){
                    if(!cur) return false;
                    // 1) 按组件名（大小写不敏感，兼容 EcomGSelect / 压缩名）
                    if(/select/i.test(typeName(cur))) return true;
                    // 2) 按 rc-select 实例方法判断：class 组件的 stateNode 常带 setValue / onInputChange / fireChange
                    let sn = cur.stateNode;
                    if(sn && typeof sn === 'object'){
                        if(typeof sn.setValue === 'function'
                           || typeof sn.onInputChange === 'function'
                           || typeof sn.fireChange === 'function'
                           || typeof sn.setUncontrolledState === 'function') return true;
                    }
                    return false;
                }

                // 找到真正的 Select 组件 fiber（名字含 Select 且通常带 onChange/value）
                let selectFiber = null, cur = fiber;
                while(cur){
                    if(isSelectFiber(cur)){ selectFiber = cur; break; }
                    cur = cur.return;
                }
                // 按名字找不到时，退而求其次取最外层带 onChange 的 fiber
                if(!selectFiber){
                    cur = fiber;
                    while(cur){
                        if(typeof (cur.memoizedProps || {}).onChange === 'function'){ selectFiber = cur; break; }
                        cur = cur.return;
                    }
                }

                // 读取当前受控值与 DOM 显示（先关闭下拉框，确保 selection-item 渲染）
                function readValue(){
                    try{
                        let inp = best.querySelector('input');
                        if(inp) inp.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}));
                        document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}));
                    }catch(e){}
                    let item = best.querySelector('.ecom-g-select-selection-item');
                    let domText = item ? ((item.title || item.innerText || '').trim()) : '';
                    let reactValue = '__undefined__';
                    let startFiber = selectFiber || fiber;
                    cur = startFiber;
                    while(cur){
                        let p = cur.memoizedProps || {};
                        if(typeof p.onChange === 'function'){
                            let v = p.value;
                            if(v === undefined && p.selectedValue !== undefined) v = p.selectedValue;
                            if(v === undefined && p.defaultValue !== undefined) v = p.defaultValue;
                            if(v !== undefined){
                                reactValue = Array.isArray(v) ? v.join(',') : String(v == null ? '' : v);
                                break;
                            }
                        }
                        cur = cur.return;
                    }
                    return {domText, reactValue};
                }

                // 优先：直接调用 rc-select 实例方法（绕过 onChange 签名不匹配问题）。
                // rc-select 的 setValue/onInputChange/fireChange 内部会触发 onChange，
                // 从而把纯型号写回 Form 受控 state（而非仅改 DOM 显示），受控值可在后续
                // 重渲染（如 Step15 关闭规格开关）中保持，真正"钉死"型号。
                if(selectFiber && selectFiber.stateNode){
                    let sn = selectFiber.stateNode;
                    let instMethods = ['setValue', 'onInputChange', 'fireChange'];
                    for(let m of instMethods){
                        if(typeof sn[m] === 'function'){
                            try{
                                try{ sn[m](pure, {value: pure, label: pure}); }
                                catch(e){ sn[m](pure); }
                                await new Promise(r => setTimeout(r, 300));
                                let rv = readValue();
                                if(rv.reactValue === pure || rv.domText === pure){
                                    return {ok:true, reason:'rc-select-instance:' + m, reactValue:rv.reactValue, domText:rv.domText, dist:loc.bestDist};
                                }
                            }catch(e){}
                        }
                    }
                }

                // 通过函数签名排除明显的事件处理器（e/event/_e）
                function isValueHandler(fn){
                    try{
                        let s = fn.toString();
                        let m = s.match(/\(([^)]*)\)/);
                        if(!m) return true;
                        let args = m[1].split(',').map(a => a.trim()).filter(Boolean);
                        if(args.length === 1 && /^(e|event|_e|ev)$/.test(args[0])) return false;
                    }catch(e){}
                    return true;
                }

                // 构建候选句柄：Select 组件的 onChange 优先；同时向下遍历 best fiber 的整棵子树
                // (child/sibling) 与向上遍历 return 链，避免 onChange 落在嵌套/兄弟 fiber 上导致 "no-handler"。
                // 事件处理器形态（单参数 e）的 onChange 也兜底收集，但放到最后尝试，避免误调。
                let candidates = [];
                let fallback = [];
                function addOnChange(fn, reason){
                    if(typeof fn !== 'function') return;
                    if(isValueHandler(fn)) candidates.push({reason: reason, fn: fn});
                    else fallback.push({reason: reason + '(event-shape)', fn: fn});
                }
                function dedupe(list){
                    let seen = new Set(), out = [];
                    for(let c of list){ if(!seen.has(c.fn)){ seen.add(c.fn); out.push(c); } }
                    return out;
                }
                if(selectFiber){
                    let p = selectFiber.memoizedProps || {};
                    if(typeof p.onChange === 'function') addOnChange(p.onChange, 'select-onChange');
                }
                // 向下遍历整棵子树
                (function walkDown(node){
                    if(!node) return;
                    let p = node.memoizedProps || {};
                    if(typeof p.onChange === 'function') addOnChange(p.onChange, 'subtree-' + typeName(node));
                    if(node.child) walkDown(node.child);
                    if(node.sibling) walkDown(node.sibling);
                })(fiber);
                // 向上遍历 return 链
                cur = fiber.return;
                while(cur){
                    let p = cur.memoizedProps || {};
                    if(typeof p.onChange === 'function') addOnChange(p.onChange, 'up-' + typeName(cur));
                    cur = cur.return;
                }
                candidates = dedupe(candidates);
                fallback = dedupe(fallback);
                for(let c of fallback){ if(candidates.indexOf(c) === -1) candidates.push(c); }

                // 构造两种调用参数：受控组件常用 value 直接传；事件形态 handler 需要 e.target.value
                function makeEvent(val){
                    let t = { value: val };
                    return {
                        target: t, currentTarget: t, nativeEvent: { target: t },
                        persist: function(){}, preventDefault: function(){}, stopPropagation: function(){},
                        isDefaultPrevented: function(){ return false; },
                        isPropagationStopped: function(){ return false; },
                        type: 'change', bubbles: true, cancelable: true
                    };
                }

                // 依次尝试每个候选：先 value 形态，若抛事件类错误再试 event 形态
                let lastReason = 'no-handler';
                let changed = false;
                for(let c of candidates){
                    let strategies = [
                        {name:'value', arg:pure},
                        {name:'value+option', arg:[pure, {value:pure, label:pure}]},
                        {name:'event', arg:makeEvent(pure)}
                    ];
                    for(let strat of strategies){
                        try{
                            c.fn(strat.arg);
                            await new Promise(r => setTimeout(r, 250));
                            let r = readValue();
                            if(r.reactValue === pure || r.domText === pure){
                                return {ok:true, reason:c.reason + '/' + strat.name, reactValue:r.reactValue, domText:r.domText, dist:loc.bestDist};
                            }
                            if(!changed && r.reactValue !== oldVal && r.reactValue !== '__undefined__' && r.reactValue !== ''){
                                changed = true;
                                lastReason = 'changed-to-' + r.reactValue + '-by-' + c.reason + '/' + strat.name;
                            }
                        }catch(e){
                            lastReason = c.reason + '/' + strat.name + ':' + e.message;
                        }
                    }
                }

                // 兜底：如果 Select 在 antd/抖店 Form 里，尝试直接 form.setFieldsValue
                async function tryFormSetValue(){
                    // 向上遍历 return 链，深度扫描所有可能藏有 form 实例的对象。
                    // antd v5 Form 实例常通过 FormContext.Provider 的 value、props.form、
                    // stateNode、hook memoizedState 等传递；组件名压缩后不一定叫 FormItem。
                    function isFormLike(obj){
                        return obj && typeof obj === 'object'
                            && typeof obj.setFieldsValue === 'function'
                            && typeof obj.getFieldsValue === 'function';
                    }
                    function deepFindForm(obj, seen, depth){
                        seen = seen || new Set();
                        if(depth === undefined) depth = 0;
                        if(depth > 6 || !obj || typeof obj !== 'object' || seen.has(obj)) return null;
                        seen.add(obj);
                        if(isFormLike(obj)) return obj;
                        if(Array.isArray(obj)){
                            for(let i=0;i<obj.length;i++){
                                let f = deepFindForm(obj[i], seen, depth+1);
                                if(f) return f;
                            }
                        }else{
                            for(let k of Object.keys(obj)){
                                if(k === 'children' || k === '_owner' || k.startsWith('__react')) continue;
                                let f = deepFindForm(obj[k], seen, depth+1);
                                if(f) return f;
                            }
                        }
                        return null;
                    }
                    let formInstance = null;
                    let fieldName = null;
                    let cur2 = fiber;
                    while(cur2){
                        let p = cur2.memoizedProps || {};
                        let sn = cur2.stateNode;
                        let ms = cur2.memoizedState;
                        // 字段名：最近的非空字符串 name（Form.Item 的 name）
                        if(fieldName === null && typeof p.name === 'string' && p.name){
                            fieldName = p.name;
                        }
                        // form 实例探测（深度扫描避免遗漏被包装的对象）
                        if(!formInstance) formInstance = deepFindForm(p.form);
                        if(!formInstance) formInstance = deepFindForm(sn);
                        if(!formInstance) formInstance = deepFindForm(p.value);
                        if(!formInstance) formInstance = deepFindForm(ms);
                        // hook 链表
                        let hook = ms;
                        while(hook){
                            if(!formInstance) formInstance = deepFindForm(hook.memoizedState);
                            hook = hook.next;
                        }
                        cur2 = cur2.return;
                    }
                    if(!formInstance) return {ok:false, reason:'form-instance-not-found'};

                    // 2) 用 getFieldsValue 自动发现包含 oldVal 的字段路径，避免字段名探测失败。
                    function findFieldPath(obj, target, path){
                        path = path || [];
                        if(obj === target) return path;
                        if(typeof obj === 'string' && target && obj.indexOf(target) >= 0) return path;
                        if(!obj || typeof obj !== 'object') return null;
                        if(Array.isArray(obj)){
                            for(let i=0;i<obj.length;i++){
                                let res = findFieldPath(obj[i], target, path.concat(i));
                                if(res) return res;
                            }
                        }else{
                            let keys = Object.keys(obj);
                            for(let k of keys){
                                let res = findFieldPath(obj[k], target, path.concat(k));
                                if(res) return res;
                            }
                        }
                        return null;
                    }
                    let allValues = {};
                    try{ allValues = formInstance.getFieldsValue(true); }catch(e){}
                    let discoveredPath = null;
                    if(oldVal){
                        discoveredPath = findFieldPath(allValues, oldVal);
                    }
                    if(!discoveredPath && fieldName === null){
                        return {ok:false, reason:'form-no-field-name'};
                    }
                    let targetPath = discoveredPath || (Array.isArray(fieldName) ? fieldName : [fieldName]);

                    // 3) 构造 setFieldsValue 更新对象
                    let update = {};
                    let ref = update;
                    for(let i=0;i<targetPath.length-1;i++){
                        ref[targetPath[i]] = {};
                        ref = ref[targetPath[i]];
                    }
                    ref[targetPath[targetPath.length-1]] = pure;

                    // 4) 尝试写入：优先 setFieldValue（单字段），其次 setFieldsValue
                    try{
                        if(discoveredPath && typeof formInstance.setFieldValue === 'function'){
                            formInstance.setFieldValue(targetPath, pure);
                        }else{
                            formInstance.setFieldsValue(update);
                        }
                        await new Promise(r => setTimeout(r, 400));
                        let r = readValue();
                        if(r.reactValue === pure || r.domText === pure){
                            return {ok:true, reason:'form-setFieldsValue:' + JSON.stringify(targetPath), reactValue:r.reactValue, domText:r.domText, dist:loc.bestDist};
                        }
                        return {ok:false, reason:'form-setFieldsValue-no-verify'};
                    }catch(e){
                        return {ok:false, reason:'form-setFieldsValue-error:' + e.message};
                    }
                }
                let formRes = await tryFormSetValue();
                if(formRes.ok) return formRes;
                if(formRes.reason !== 'form-no-field-name'){
                    lastReason = formRes.reason;
                }

                // 全部失败：尽量恢复到 oldVal，避免页面出现 E2/空值等脏状态
                if(candidates.length && oldVal){
                    try{
                        candidates[0].fn(oldVal);
                        await new Promise(r => setTimeout(r, 250));
                        let r = readValue();
                        if(r.reactValue === oldVal || r.domText === oldVal){
                            return {ok:false, reason:'restore-ok-after-fail:' + lastReason, dist:loc.bestDist};
                        }
                    }catch(e){}
                }
                return {ok:false, reason:lastReason, dist:loc.bestDist};
            }
        """, {"modelX": model_x, "modelY": model_y, "pure": pure_model, "oldVal": old_value})
    except Exception as e:
        log("MODEL", f"OPPO 专用修复 JS 执行异常: {e}")
        res = {"ok": False, "reason": str(e)}

    if res.get("ok"):
        log("MODEL", f"OPPO 专用修复：已把受控值改为纯型号 '{pure_model}' (方式={res.get('reason')}, React={res.get('reactValue')}, DOM={res.get('domText')}, dist={res.get('dist')})")
        return True
    else:
        log("MODEL", f"OPPO 专用修复：未真正改成纯型号 (reason={res.get('reason')}, dist={res.get('dist')})；保持原值 '{old_value}'，避免脏状态")
        return False



def force_write_oppo_model(page, model_x: int, model_y: int, pure_model: str) -> bool:
    """OPPO 专用：按用户最简逻辑写型号——去掉品牌名，剩下的就是型号。

    用户铁律（2026-08-07）：
      - OPPO 机型：去掉品牌名(OPPO)，剩下的就是型号；
      - 只加 大小写 / 中英文(品牌) / 缩进(空格) 容错，不做多余步骤；
      - 一步到位：对就是对(归一化相等即成功)，错就是错(不相等即失败，不重试不兜底)。

    实现（单步）：
      1) 幂等闸门：型号框当前值(剥品牌+归一化)已等于目标 -> 直接成功，绝不重写；
      2) 真实键盘：点型号框(校验相邻标签为"型号") -> 清空 -> 输入纯型号；
      3) 在下拉选项里找"剥品牌+归一化"等于目标的选项，找到就点它(一步选中)；
      4) 验证：型号框实际值(剥品牌+归一化)等于目标 -> 成功；否则失败(错就是错)。
    平台型号下拉只提供带品牌前缀的选项(如 OPPO Reno14)，选中后框显示 OPPO Reno14，
    但剥品牌归一化 == Reno14 == 目标，视为正确(对就是对)。不自由文本、不盲 Enter、
    不多层 JS、不重试循环。
    """
    if not pure_model:
        return False

    BRAND_CN, BRAND_EN = "OPPO", "OPPO"
    target_norm = _norm_model_loose(_strip_brand_tokens(pure_model, BRAND_CN, BRAND_EN).lower())

    def _read_box_val():
        return page.evaluate(r"""
            (params) => {
                let pinned = document.querySelector('[data-wb-pin-model="1"]');
                let best = pinned;
                if (!best) {
                    let selects = document.querySelectorAll('.ecom-g-select');
                    let bestDist = 1e9, x = params.x, y = params.y;
                    for (let s of selects) {
                        let r = s.getBoundingClientRect();
                        let cx = r.left + r.width/2, cy = r.top + r.height/2;
                        let d = Math.abs(cx - x) + Math.abs(cy - y);
                        if (d < bestDist) { bestDist = d; best = s; }
                    }
                }
                if (!best) return '';
                let item = best.querySelector('.ecom-g-select-selection-item');
                return item ? (item.title || item.innerText || '').trim() : '';
            }
        """, {"x": model_x, "y": model_y})

    # 1) 幂等闸门：已是正确值直接成功，绝不重写，避免把已对的型号弄坏。
    # 修复（2026-08-27）：本函数文档明确说"平台型号下拉只提供带品牌前缀的选项
    # (如 OPPO Reno14)……但剥品牌归一化 == 目标，视为正确"，可这里原来比较 cur
    # 时没有剥品牌前缀（只对 target_norm 剥了），导致草稿框里其实已经是正确值
    # "OPPO Reno15c" 时，闸门死活判不过，还是去清空重写——而这个型号平台压根
    # 没有对应的"裸型号"选项可选，清空后就再也选不回来了，白白把好值搞坏。
    cur = _read_box_val()
    cur_norm = _norm_model_loose(_strip_brand_tokens(cur, BRAND_CN, BRAND_EN).lower()) if cur else ""
    if cur_norm and cur_norm == target_norm:
        log("MODEL", f"OPPO 强制写入幂等闸门：型号框值 '{cur}' 剥品牌归一化 == 目标 '{pure_model}'，跳过重写，直接成功")
        return True

    PIN_SELECTOR = '[data-wb-pin-model="1"]'
    try:
        # 2) 真实键盘单步：定位型号框(校验相邻标签为"型号") -> 清空 -> 输入纯型号
        box = page.evaluate(r"""
            (params) => {
                let x=params.x, y=params.y;
                let pinned = document.querySelector('""" + PIN_SELECTOR + r"""');
                let best = pinned; let source='pinned';
                if(!best){
                    let selects=document.querySelectorAll('.ecom-g-select'); let bestDist=1e9;
                    for(let s of selects){ let r=s.getBoundingClientRect(); let cx=r.left+r.width/2, cy=r.top+r.height/2; let d=Math.abs(cx-x)+Math.abs(cy-y); if(d<bestDist){bestDist=d; best=s;} }
                    source='coords';
                }
                if(!best) return null;
                function hasModelLabel(s){ let sr=s.getBoundingClientRect(); let ls=[].slice.call(document.querySelectorAll('.ecom-g-form-item-label,label,[class*="form-item-label"],[class*="field-label"],[class*="label"]')); for(let l of ls){ if(l.closest('.ecom-g-select')) continue; let t=(l.innerText||'').trim().replace(/[\*\s\:\：]/g,''); if(t!=='型号') continue; let lr=l.getBoundingClientRect(); if(lr.right>sr.right+50) continue; if(Math.abs((lr.top+lr.height/2)-(sr.top+sr.height/2))<80) return true; } return false; }
                let trig = best.querySelector('.ecom-g-select-selector') || best; let r=trig.getBoundingClientRect();
                return {cx:Math.round(r.left+r.width/2), cy:Math.round(r.top+r.height/2), labelOk:hasModelLabel(best), source:source};
            }
        """, {"x": model_x, "y": model_y})
        if not box or not box.get("labelOk"):
            log("MODEL", f"OPPO 强制写入：型号框定位失败(labelOk={box.get('labelOk') if box else None})，返回失败等待重新定位")
            return False
        cx, cy = box["cx"], box["cy"]
        page.mouse.click(cx, cy)
        time.sleep(0.4)
        # 清空：JS 置空 + Ctrl+A Delete + Ctrl+A Backspace
        page.evaluate(r"""
            (params) => {
                let pinned=document.querySelector('[data-wb-pin-model="1"]'); let best=pinned;
                if(!best){ let selects=document.querySelectorAll('.ecom-g-select'); let bestDist=1e9,x=params.x,y=params.y; for(let s of selects){ let r=s.getBoundingClientRect(); let cx=r.left+r.width/2, cy=r.top+r.height/2; let d=Math.abs(cx-x)+Math.abs(cy-y); if(d<bestDist){bestDist=d; best=s;} } }
                if(!best) return; let input=best.querySelector('input'); if(input){ try{ let proto=Object.getPrototypeOf(input); let desc=Object.getOwnPropertyDescriptor(proto,'value'); let setter=(desc&&desc.set)||Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set; setter.call(input,''); input.dispatchEvent(new Event('input',{bubbles:true})); input.dispatchEvent(new Event('change',{bubbles:true})); }catch(e){} }
            }
        """, {"x": model_x, "y": model_y})
        time.sleep(0.2)
        page.keyboard.press("Control+a"); time.sleep(0.15); page.keyboard.press("Delete"); time.sleep(0.15)
        page.keyboard.press("Control+a"); time.sleep(0.1); page.keyboard.press("Backspace"); time.sleep(0.2)
        page.keyboard.type(pure_model, delay=30)

        # 3) 在下拉选项里找"归一化后等于纯型号"的项，点它(一步选中，不盲 Enter)。
        # 注意：必须找不带 OPPO 品牌前缀的裸型号选项(如 'Reno14' / 'Reno 14')；
        # 若点选带前缀项 'OPPO Reno14'，平台发布校验会拒绝('不在最新可选范围')。
        # 带轮询重试（2026-08-27 补充）：切完品牌/输完关键字后，该属性的可选项列表
        # 有时要等平台按新品牌异步刷新完才会出现目标型号，之前只等 0.6s 扫一次，
        # 扫到的是还没刷新完的旧/默认占位选项（如 'E2'/'A6'/'双核'/'mini' 这类跟任何
        # 品牌都不相关的示例值），导致"下拉无匹配选项"误判为真失败。改成轮询：
        # 最多等 3.5s，每 300ms 重新扫一次，一旦刷新出目标选项立刻点选。
        chosen = None
        poll_deadline = time.time() + 3.5
        while time.time() < poll_deadline:
            chosen = page.evaluate(r"""
                (params) => {
                    let pureNorm = params.pureNorm;
                    function norm(t){ return (t || '').toLowerCase().replace(/\s+/g,''); }
                    let sels=['.ecom-g-select-dropdown .ecom-g-select-item-option','.ecom-g-select-dropdown-menu-item','.ecom-g-select-item-option','div.ant-select-item-option','div.rc-virtual-list-holder-inner > div'];
                    let opts=[]; for(let sel of sels){ try{ opts.push(...document.querySelectorAll(sel)); }catch(e){} }
                    for(let o of opts){ let t=(o.innerText||o.title||'').trim(); if(!t) continue; if(norm(t)===pureNorm){ try{o.scrollIntoView({block:'nearest'});}catch(e){} try{o.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));}catch(e){} try{o.click();}catch(e){} return t; } }
                    return null;
                }
            """, {"pureNorm": target_norm})
            if chosen:
                break
            time.sleep(0.3)
        if not chosen:
            log("MODEL", f"OPPO 强制写入：轮询 3.5s 后下拉仍无匹配选项(目标归一化='{target_norm}')，错就是错，返回失败")
            return False
        log("MODEL", f"OPPO 强制写入：已点选匹配选项 '{chosen}'")
        time.sleep(0.6)

        # 4) 验证：型号框实际值剥品牌前缀后归一化 == 目标 -> 成功；否则失败(错就是错)。
        # 同幂等闸门的修复：点中的选项本身可能带 OPPO 前缀（如 'OPPO Reno14'），
        # 剥品牌后才等于目标，不剥的话这里会误判成功为失败。
        final = _read_box_val()
        final_norm = _norm_model_loose(_strip_brand_tokens(final, BRAND_CN, BRAND_EN).lower()) if final else ""
        if final_norm and final_norm == target_norm:
            log("MODEL", f"OPPO 强制写入：成功，型号框='{final}' (剥品牌归一化 == 目标 '{pure_model}')")
            return True
        log("MODEL", f"OPPO 强制写入：最终值 '{final}' 归一化不等于目标 '{pure_model}'，错就是错，返回失败")
        return False
    except Exception as e:
        log("MODEL", f"OPPO 强制写入 异常: {e}")
        return False


def _pin_and_locate_model_box(page, hint_x: int = -1, hint_y: int = -1,
                               brand_cn: str = "", brand_en: str = "") -> dict:
    """锁定基础信息页并把品牌/型号行滚到视口固定位置，重新定位型号框，返回新鲜坐标。

    用户要求（2026-08-06）：不要靠选中文本猜哪个是品牌/型号框——OPPO 的型号框显示
    "OPPO Find X7" 也含 "OPPO"，会被误当成品牌框，结果定位到成色框（x=926）。
    改为通过页面标签文本锁定：
      1) 优先直接找 "型号" 标签，其右侧/下方对应的下拉框就是型号框；
      2) 备用：找 "品牌" 标签，品牌框右侧同行的第一个下拉框就是型号框。
    只有标签法失败时才回退到旧的"品牌值文本匹配"。

    关键增强（2026-08-07）：
      - 找到型号框后，在 DOM 元素上打标记 data-wb-pin-model="1"，后续 force_write/guard
        可以直接按标记定位，不再受滚动导致坐标漂移影响。
      - 标签匹配更宽松：支持 "型号"、"*型号"、"型号："、"型号 *" 等变体，并通过
        label.for -> control -> 最近 select 的关联链定位，比单纯坐标更稳。
      - 如果按坐标定位后发现相邻 label 不是"型号"，立即用标签法修正，避免误点成色/
        保修/电池健康度等框。

    返回字段：ok, x, y, w, h, cx, cy, brandX, brandY, method, selector, debug
    """
    # 1) 切回基础信息页（已在该页则点击无副作用）
    page.evaluate("""
        () => {
            let tabs = document.querySelectorAll('[role=tab]');
            for (let t of tabs) {
                let txt = t.innerText || '';
                if (txt.includes('商品信息') || txt.includes('基础信息')
                    || txt.includes('商品') || txt.includes('基础')) {
                    t.click();
                    break;
                }
            }
        }
    """)
    time.sleep(1.0)

    # 2~4) 先用标签法精确定位；失败再回退旧逻辑
    res = page.evaluate(r"""
        (params) => {
            // 清理旧标记，避免残留
            document.querySelectorAll('[data-wb-pin-model]').forEach(el => el.removeAttribute('data-wb-pin-model'));

            let brandCn = params.brandCn || '';
            let brandEn = (params.brandEn || '').toLowerCase();
            let hintX = params.hintX, hintY = params.hintY;
            let selects = document.querySelectorAll('.ecom-g-select');

            // ===== 工具函数 =====
            function vOverlap(a, b) {
                return Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
            }
            function rightNeighbors(src, maxDy) {
                maxDy = maxDy || 60;
                let sr = src.getBoundingClientRect();
                let list = [];
                for (let s of selects) {
                    if (s === src) continue;
                    let r = s.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.left > sr.left) {
                        let ov = vOverlap(sr, r);
                        if (ov > 2 || Math.abs(r.top - sr.top) <= maxDy) {
                            list.push({ el: s, r: r, x: r.left, y: r.top });
                        }
                    }
                }
                list.sort((a, b) => a.x - b.x);
                return list;
            }
            function normalizeLabelText(t) {
                return (t || '').replace(/[\*\s\:\：\(\)（）]/g, '').trim();
            }
            function isModelLabel(text) {
                let n = normalizeLabelText(text);
                if (n !== '型号') return false;
                // 排除 CPU型号 / 处理器型号 / 芯片型号
                if (/cpu|处理器|芯片/i.test(text)) return false;
                return true;
            }
            function isBrandLabel(text) {
                return normalizeLabelText(text) === '品牌';
            }

            // 收集页面标签元素
            let labelSelectors = [
                '.ecom-g-form-item-label',
                'label',
                '[class*="form-item-label"]',
                '[class*="field-label"]',
                '.ecom-g-col .ecom-g-form-item-required',
                '.ecom-g-form-item-required',
                '[class*="label"]'
            ];
            let labelEls = [];
            for (let sel of labelSelectors) {
                try { labelEls.push(...document.querySelectorAll(sel)); } catch (e) {}
            }
            let seen = new Set();
            let labels = [];
            for (let el of labelEls) {
                if (seen.has(el)) continue;
                seen.add(el);
                if (el.closest('.ecom-g-select')) continue;
                let text = (el.innerText || el.textContent || '').trim();
                if (!text) continue;
                let r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                labels.push({ el: el, text: text, rect: r });
            }

            let modelLabels = labels.filter(l => isModelLabel(l.text));
            let brandLabels = labels.filter(l => isBrandLabel(l.text));
            // 兜底：包含"型号"但不是 CPU 等
            let modelLikeLabels = labels.filter(l =>
                l.text.includes('型号') && !/cpu|处理器|芯片/i.test(l.text)
                && !modelLabels.includes(l)
            );

            let debug = { labels: labels.map(l => ({ text: l.text, x: Math.round(l.rect.left), y: Math.round(l.rect.top) })) };
            let method = '';
            let brandBox = null, modelBox = null;

            // 通过 label.for 找到对应 control，再取最近 select
            function selectForLabel(lbl) {
                // 方法1: label.for -> control
                let forId = lbl.el.getAttribute('for');
                if (forId) {
                    let control = document.getElementById(forId);
                    if (control) {
                        let s = control.closest('.ecom-g-select');
                        if (s) return s;
                    }
                }
                // 方法2: label 父级 form-item 内找"位于标签下方/右侧、水平最近"的 select。
                // 关键修复：品牌与型号常在同一行容器，querySelector 取第一个会误拿到品牌框；
                // 这里要求 select 在标签下方(同行仅 brand 会被排除)且水平中心最近，才能命中型号框。
                let item = lbl.el.closest('.ecom-g-form-item, [class*="form-item"], [class*="field"]');
                if (item) {
                    let lr = lbl.rect;
                    let cands = item.querySelectorAll('.ecom-g-select');
                    let best = null, bestScore = 1e9;
                    for (let s of cands) {
                        let sr = s.getBoundingClientRect();
                        if (sr.width <= 0 || sr.height <= 0) continue;
                        // 型号框在标签下方（row 布局 label 与 select 不同行）；同行框(如品牌)会被排除
                        if (sr.top < lr.top - 8) continue;
                        let dxc = Math.abs((sr.left + sr.width / 2) - (lr.left + lr.width / 2));
                        let dyc = Math.abs((sr.top + sr.height / 2) - (lr.top + lr.height / 2));
                        let score = dxc + dyc * 1.5;
                        if (score < bestScore) { bestScore = score; best = s; }
                    }
                    if (best) return best;
                }
                // 方法3: label 右侧/下方最近的 select（优先右侧）
                let r = lbl.rect;
                let best = null, bestDist = 1e9;
                for (let s of selects) {
                    let sr = s.getBoundingClientRect();
                    if (sr.width <= 0 || sr.height <= 0) continue;
                    // 必须在 label 右侧或下方不远
                    let dx = sr.left - r.right;
                    let dy = Math.abs((sr.top + sr.height / 2) - (r.top + r.height / 2));
                    // 只考虑右侧或略偏下，不考虑左侧
                    if (sr.left < r.left - 20) continue;
                    let dist = (dx < 0 ? 0 : dx) + dy * 2;
                    if (dist < bestDist) { bestDist = dist; best = s; }
                }
                return best;
            }

            // 方法 B0（首选）：品牌框（品牌值匹配）-> 右侧同行第一个下拉框 = 型号框。
            // 比标签关联法更可靠：品牌值是确定已知的，品牌框精确定位后，其右侧第一个
            // 下拉框就是型号框，且不会因"型号"标签与品牌框同容器而误取品牌框。
            // 2026-08-07 修复：品牌框常显示为联合串（如 'Huawei/华为' / 'Redmi/红米' /
            // 'Samsung/三星'），不等于纯品牌名；用"包含品牌名"匹配以命中联合串。
            // 同时排除含空格者：品牌值无空格、型号值必含空格（如 '三星 W2018 典藏尊铂版'），
            // 避免把型号框误认成品牌候选（沿用第1次修复：三星双框皆含"三星"的误判）。
            if ((brandCn || brandEn) && !modelBox) {
                let bCandidates = [];
                for (let s of selects) {
                    let sel = s.querySelector('.ecom-g-select-selection-item');
                    let text = sel ? (sel.title || sel.innerText || '').trim() : '';
                    if (!text) continue;
                    let lc = text.toLowerCase();
                    let isBrand = (brandCn && (text === brandCn || text.includes(brandCn)))
                               || (brandEn && (lc === brandEn.toLowerCase() || lc.includes(brandEn.toLowerCase())));
                    // 品牌值可以是 honor/荣耀这类联合串，但不应含型号数字。
                    // 仅用“无空格”会把“荣耀300”、“荣耀X50”误当品牌框。
                    if (isBrand && !/\s/.test(text) && !/[0-9]/.test(text)) {
                        bCandidates.push(s);
                    }
                }
                if (bCandidates.length) {
                    // 抗污染：多个候选时优先离 hint(新鲜坐标)最近者
                    let best = bCandidates[0], bestScore = 1e9;
                    for (let b of bCandidates) {
                        let br = b.getBoundingClientRect();
                        let score = (hintX >= 0 && hintY >= 0)
                            ? (Math.abs(br.left - hintX) + Math.abs(br.top - hintY)) : 0;
                        if (score < bestScore) { bestScore = score; best = b; }
                    }
                    let neighbors = rightNeighbors(best, 80);
                    if (neighbors.length) {
                        brandBox = best; modelBox = neighbors[0].el; method = 'brand-value-neighbor';
                    }
                }
            }

            // 方法 A：直接锁定 "型号" 标签（仅当 B0 未命中时才回退到此）
            if (!modelBox) {
                for (let ml of modelLabels) {
                    let s = selectForLabel(ml);
                    if (s) {
                        modelBox = s;
                        method = 'model-label';
                        // 顺带找同行品牌框（用于 brandX/brandY 返回）
                        let sr = s.getBoundingClientRect();
                        let sameRow = [];
                        for (let cand of selects) {
                            if (cand === s) continue;
                            let cr = cand.getBoundingClientRect();
                            if (cr.left < sr.left && (vOverlap(sr, cr) > 2 || Math.abs(cr.top - sr.top) <= 60)) {
                                sameRow.push({ el: cand, x: cr.left });
                            }
                        }
                        sameRow.sort((a, b) => b.x - a.x);
                        if (sameRow.length) brandBox = sameRow[0].el;
                        break;
                    }
                }
            }

            // 方法 B：锁定 "品牌" 标签，取右侧同行第一个下拉框作为型号框
            if (!modelBox && brandLabels.length) {
                for (let bl of brandLabels) {
                    let s = selectForLabel(bl);
                    if (s) {
                        brandBox = s;
                        let neighbors = rightNeighbors(s, 80);
                        if (neighbors.length) {
                            modelBox = neighbors[0].el;
                            method = 'brand-label-neighbor';
                            break;
                        }
                    }
                }
            }

            // 方法 C：包含"型号"的标签兜底
            if (!modelBox && modelLikeLabels.length) {
                for (let ml of modelLikeLabels) {
                    let s = selectForLabel(ml);
                    if (s) {
                        modelBox = s;
                        method = 'model-like-label';
                        break;
                    }
                }
            }

            // ===== 兜底：旧逻辑（用品牌选中文本匹配） =====
            if (!modelBox && (brandCn || brandEn)) {
                let brandCandidates = [];
                for (let s of selects) {
                    let sel = s.querySelector('.ecom-g-select-selection-item');
                    let text = sel ? (sel.title || sel.innerText || '').trim() : '';
                    if (!text) continue;
                    let lc = text.toLowerCase();
                    // 要求选中文本"等于"品牌名，不能只是"包含"
                    if (text === brandCn || text === brandEn ||
                        (brandEn && lc === brandEn.toLowerCase())) {
                        brandCandidates.push(s);
                    } else if (text.includes(brandCn) || (brandEn && lc.includes(brandEn.toLowerCase()))) {
                        if (!/\s/.test(text) && !/[0-9]/.test(text)) {
                            brandCandidates.push({ el: s, loose: true });
                        }
                    }
                }
                let strict = brandCandidates.filter(c => !c.loose);
                if (!strict.length && brandCandidates.length) strict = brandCandidates;
                let bestScore = 1e9;
                for (let item of strict) {
                    let b = item.el || item;
                    let br = b.getBoundingClientRect();
                    let neighbors = rightNeighbors(b);
                    if (!neighbors.length) continue;
                    let score = (hintX >= 0 && hintY >= 0)
                        ? (Math.abs(br.left - hintX) + Math.abs(br.top - hintY)) : 0;
                    debug.fallbackCandidates = debug.fallbackCandidates || [];
                    debug.fallbackCandidates.push({
                        bx: Math.round(br.left), by: Math.round(br.top),
                        firstRightX: Math.round(neighbors[0].x),
                        firstRightY: Math.round(neighbors[0].y),
                        score: score
                    });
                    if (score < bestScore) { bestScore = score; brandBox = b; modelBox = neighbors[0].el; method = 'fallback-value-match'; }
                }
            }

            if (!modelBox) return { ok: false, debug: debug, reason: 'model-box-not-found' };

            // ===== 校验：modelBox 旁边是否真的有"型号"标签 =====
            // 这是用户要求的"检索字段修正"：如果按坐标/旧逻辑找到的框旁边不是"型号"，
            // 而是"成色"/"保修"/"电池健康度"等，立即修正为真正的型号框。
            function labelForSelect(s) {
                let sr = s.getBoundingClientRect();
                let best = null, bestScore = 1e9;
                for (let l of labels) {
                    let lr = l.rect;
                    // label 必须在 select 左侧或正上方
                    if (lr.right > sr.right) continue;
                    let dx = sr.left - lr.right;
                    if (dx < -30) continue;  // label 在 select 右侧太远则忽略
                    let dy = Math.abs((lr.top + lr.height / 2) - (sr.top + sr.height / 2));
                    let score = Math.max(0, dx) + dy * 2;
                    if (score < bestScore) { bestScore = score; best = l; }
                }
                return best;
            }
            let nearbyLabel = labelForSelect(modelBox);
            let labelOk = nearbyLabel && isModelLabel(nearbyLabel.text);
            if (!labelOk && modelLabels.length) {
                // 强制修正：用真正的"型号"标签重新定位
                for (let ml of modelLabels) {
                    let s = selectForLabel(ml);
                    if (s && s !== modelBox) {
                        debug.correction = { from: method, oldBox: { x: Math.round(modelBox.getBoundingClientRect().left), y: Math.round(modelBox.getBoundingClientRect().top) }, to: 'model-label-correction' };
                        modelBox = s;
                        method = 'model-label-correction';
                        // 重新找品牌框
                        let sr = s.getBoundingClientRect();
                        let sameRow = [];
                        for (let cand of selects) {
                            if (cand === s) continue;
                            let cr = cand.getBoundingClientRect();
                            if (cr.left < sr.left && (vOverlap(sr, cr) > 2 || Math.abs(cr.top - sr.top) <= 60)) {
                                sameRow.push({ el: cand, x: cr.left });
                            }
                        }
                        sameRow.sort((a, b) => b.x - a.x);
                        if (sameRow.length) brandBox = sameRow[0].el;
                        break;
                    }
                }
            }

            // 终检兜底：若最终型号框的相邻标签仍不是"型号"（说明定位到了成色/保修/电池健康度等框），
            // 且页面上确实存在真正的"型号"标签（有 ground truth），则拒绝定位，绝不给错误框打标记，
            // 交由上层重试/重 pin，避免把错误框当型号框导致后续误读型号。
            let finalNearby = labelForSelect(modelBox);
            let finalLabelOk = finalNearby && isModelLabel(finalNearby.text);
            // 关键放宽（参考 OPPO 钉死方案）：若型号框是通过"品牌值→右侧第一个下拉框"
            // (brand-value-neighbor) 可靠锁定的，则直接信任该框，不再因 label 探针(layout 差异
            // 导致"型号"标签未被捕获/不在左侧)而误拒。品牌值是确定已知的，其右侧第一个下拉框
            // 必为型号框，这比脆弱的标签文本校验更稳，能让 pin 返回 ok 以正确锁定型号框、
            // 供下游搜索框定位。仅当 method 非 brand-value-neighbor 且页面确有"型号"标签(ground
            // truth)时才保留严格拒斥，避免把成色/保修框当型号框。
            if (!finalLabelOk && method !== 'brand-value-neighbor' && modelLabels.length) {
                return { ok: false, debug: debug, reason: 'model-box-label-mismatch(not-model-box)' };
            }

            // PROBE 给出的品牌/型号行已可见。此处 scrollIntoView 会在
            // 点击前改变 y 坐标，Redmi 页面上会把鼠标点击移到遮罩层。

            let mb = modelBox.getBoundingClientRect();
            let mw = mb.width, mh = mb.height;
            let trigger = modelBox.querySelector('.ecom-g-select-selector');
            let tr = trigger ? trigger.getBoundingClientRect() : mb;
            let cx = Math.round(tr.left + tr.width / 2);
            let cy = Math.round(tr.top + tr.height / 2);
            let ariaControls = trigger ? (trigger.getAttribute('aria-controls') || trigger.getAttribute('aria-owns') || '') : '';

            let br = brandBox ? brandBox.getBoundingClientRect() : mb;
            let bx = Math.round(br.left), by = Math.round(br.top);

            // 硬性约束：型号框必须是品牌框的"紧邻右列"
            let COLUMN_MAX = 260;
            if (brandBox) {
                let gap = mb.left - br.left;
                if (gap <= 0 || gap > COLUMN_MAX) {
                    return { ok: false, debug: debug,
                             reason: 'model-box-not-immediate-right(gap=' + Math.round(gap) + ')',
                             brandX: bx, brandY: by };
                }
            }

            // hint 是调用前的“品牌框”坐标，仅用于帮助识别品牌，绝不能反向覆盖型号框。
            // scrollIntoView 或平台弹窗关闭会使新坐标与旧 hint 大幅偏离，这是正常的视口重排。
            // 旧逻辑在偏离时把距 hint 最近的 select（即品牌框）误当型号框，
            // 导致实际点击错位。现在始终信任已通过“品牌右侧紧邻列”约束的 modelBox。
            let sanity = 'ok';
            if (hintX >= 0 && hintY >= 0) {
                let dx = Math.abs(Math.round(mb.left) - hintX);
                let dy = Math.abs(Math.round(mb.top) - hintY);
                if (dx > 250 || dy > 120) {
                    sanity = 'viewport-reflow-accepted';
                }
            }

            // 打标记：让后续 force_write / guard 可以直接按标记找到这个元素，不再受滚动影响
            try { modelBox.setAttribute('data-wb-pin-model', '1'); } catch (e) {}

            return {
                ok: true,
                x: Math.round(mb.left), y: Math.round(mb.top),
                w: Math.round(mw), h: Math.round(mh),
                cx: cx, cy: cy, ariaControls: ariaControls,
                brandX: bx, brandY: by,
                method: method,
                sanity: sanity,
                selector: '[data-wb-pin-model="1"]',
                debug: debug
            };
        }
    """, {"hintX": hint_x, "hintY": hint_y, "brandCn": brand_cn, "brandEn": brand_en})

    if res.get("debug"):
        log("MODEL", f"[pin 调试] method={res.get('method')}, 型号框(x={res.get('x')},y={res.get('y')},cx={res.get('cx')},cy={res.get('cy')}), 品牌框(x={res.get('brandX')},y={res.get('brandY')}), sanity={res.get('sanity','-')}, reason={res.get('reason','ok')}")
    return res


def select_model(page, parsed: dict, select_positions: list, category: str = "", brand_y: int = -1, brand_x: int = -1) -> dict:
    """选择型号：定位型号下拉框 → 按优先级尝试多个搜索关键字 → 选择匹配项 → 验证"""
    log("MODEL", "开始选择型号...")
    plan = resolve_brand_model_plan(parsed)
    model = plan["source_model"]
    brand_normalized = plan["target_brand_value"]
    brand_cn = plan["target_brand_cn"]

    if not model:
        log("MODEL", "标题中未解析到型号，跳过型号选择")
        return {"success": False, "model_value": "", "error": "型号为空"}

    # 构建搜索关键字候选列表（按优先级排序）
    # 王师傅要求：直接输入商品标题里的型号关键词，不带品牌名，快速搜索
    # 关键修复：优先使用完整型号，避免截断导致误匹配（如 "X50i" → "X50" 误匹配到 "X50"）
    brand_en = plan["target_brand_en"]
    disp_cn, disp_en = plan["target_brand_cn"], plan["target_brand_en"]
    is_redmi = plan["is_redmi"]

    # ===== 关键字候选生成（用户要求顺序：纯型号优先 → 容错 → 最后才品牌+型号）=====
    # 关键修复：先剥离品牌前缀得到"纯型号"，再在纯型号上做拆分级联；品牌+型号组合仅对纯型号施加，
    # 绝不对已含品牌的 model 再套品牌（否则产生 "华为 华为 nova 14 (活力版)" 这类双品牌非法候选）。
    search_keywords = []
    pure_keywords = []   # 纯型号级联（不含品牌），作为首选候选

    # 1) 纯型号：剥离品牌前缀（中英文，大小写不敏感），得到真正的型号核心
    pure_model = _strip_brand_tokens(model, brand_cn, brand_en)
    if not pure_model:
        pure_model = model
    pure_parts = pure_model.split()
    if ' ' in pure_model:
        # 有空格的型号（如 "K90 Pro Max" / "nova 14 (活力版)"），从长到短拆分
        # 例如 "K90 Pro Max" → ["K90 Pro Max", "K90 Pro", "K90"]
        for i in range(len(pure_parts)):
            kw = " ".join(pure_parts[:len(pure_parts)-i])
            if kw and kw not in pure_keywords:
                pure_keywords.append(kw)
        # 补充：整型号去空格版本（如 "Note 17 Pro" → "Note17Pro"），紧跟在完整
        # 型号之后尝试。实测发现（REDMI Note 17 Pro）：型号下拉框的输入过滤是按
        # 字面子串比对，若平台选项文本连写不带空格，输入带空格的关键字会导致
        # 过滤结果为空、下拉框里根本渲染不出该选项——哪怕手动清空重输"17"这种
        # 短数字关键字反而能筛出来。findBest() 里的 norm() 只在选项已经被过滤
        # 渲染出来之后才生效，救不了"过滤阶段就是空列表"的情况，所以要在关键字
        # 侧也补一个去空格变体兜底。
        glued = pure_model.replace(" ", "")
        if glued and glued not in pure_keywords:
            pure_keywords.insert(1 if pure_keywords else 0, glued)
    else:
        # 无空格的型号（如 "X50i"、"X60GT"），不截断！直接使用完整型号
        # 截断会导致 "X50i" → "X50" 后搜索，误匹配到 "X50" 而不是 "X50i"
        pure_keywords.append(pure_model)
        # 对于无空格但较长的型号（如 "Note15Pro"），尝试按大写字母拆分
        if len(pure_model) > 6:
            # 简单按数字/字母边界拆分，如 "X60Pro" → "X60 Pro"
            spaced = re.sub(r'([a-z])([A-Z])', r'\1 \2', pure_model)
            spaced = re.sub(r'([0-9])([a-zA-Z])', r'\1 \2', spaced)
            spaced = re.sub(r'([a-zA-Z])([0-9])', r'\1 \2', spaced)
            if spaced != pure_model and spaced not in pure_keywords:
                pure_keywords.append(spaced)
                # 拆分的也做从长到短
                spaced_parts = spaced.split()
                for i in range(1, len(spaced_parts)):
                    short = " ".join(spaced_parts[:len(spaced_parts)-i])
                    if short and short not in pure_keywords:
                        pure_keywords.append(short)

    search_keywords.extend(pure_keywords)

    # 3) 品牌+型号组合（最后尝试）：仅对纯型号施加品牌前缀；并剔除"品牌名出现≥2次"的非法候选
    #    （理论上 pure_keywords 已无品牌，不会产生双品牌；_brand_appear_ge2 为兜底安全网）。
    #    OPPO 品牌用户明确要求不要带 OPPO 前缀搜索；Redmi 选项本身不带 Xiaomi/小米 前缀，也跳过。
    def _brand_appear_ge2(s: str) -> bool:
        cnt = 0
        if brand_cn:
            cnt += s.count(brand_cn)
        if brand_en and brand_en.lower() != (brand_cn or "").lower():
            cnt += s.lower().count(brand_en.lower())
        return cnt >= 2

    if not _is_oppo_brand(brand_cn, brand_en) and not is_redmi:
        # 先英文品牌前缀，再中文品牌前缀（与历史顺序一致）
        for mk in pure_keywords:
            if brand_en:
                combo = f"{brand_en} {mk}"
                if combo not in search_keywords and not _brand_appear_ge2(combo):
                    search_keywords.append(combo)
        for mk in pure_keywords:
            if brand_cn and brand_cn != brand_en:
                combo = f"{brand_cn} {mk}"
                if combo not in search_keywords and not _brand_appear_ge2(combo):
                    search_keywords.append(combo)

    log("MODEL", f"搜索关键字候选(纯型号优先, 品牌+型号置后): {search_keywords}")

    # 型号强制映射覆盖：解析型号（可能含品牌前缀、括号写法不一）在覆盖表中时，
    # 把"下拉框精确选项文本"作为精确命中关键字（带品牌前缀，如 HUAWEI nova 14 (活力版)），
    # 作为第一关键字尝试。强制映射来自已确认的业务规则/平台枚举，比通用截断词更可靠，
    # 可避免 Note 14 先浪费时间尝试 Note/Note14 等不完整关键字。
    _ov_key, exact = _resolve_model_override(model, brand_cn, brand_en)
    if _ov_key is not None:
        if exact in search_keywords:
            search_keywords.remove(exact)
        search_keywords.insert(0, exact)
        # 兜底：若下拉中不存在该带前缀精确选项，退回去品牌名(且无括号)的纯型号（如 nova 14 活力版）
        _fallback = _norm_model_loose(_strip_brand_tokens(exact, brand_cn, brand_en))
        if _fallback and _fallback != exact and _fallback not in search_keywords:
            _idx = search_keywords.index(exact) + 1 if exact in search_keywords else 1
            search_keywords.insert(_idx, _fallback)
        log("MODEL", f"【强制映射】型号 '{model}' 覆盖为下拉框精确选项 '{exact}'（兜底 '{_fallback}'），关键字候选: {search_keywords}")

    # Once the official API supplied a unique option value, truncated keywords
    # add latency and can only expose sibling variants. Search the complete
    # official name (and its brand-stripped formatting alias) once each.
    if parsed.get("official_model_value"):
        strict_keywords = []
        for candidate in (model, pure_model):
            if candidate and candidate not in strict_keywords:
                strict_keywords.append(candidate)
        search_keywords = strict_keywords
        log("MODEL", f"API 官方型号严格模式，仅尝试完整关键字: {search_keywords}")

    # 这里只记录 placeholder。不能遍历所有已选 select 后直接判定型号成功：
    # 页面还有品牌、成色、保修等字段，必须先按标签/品牌右侧同行定位型号框。
    for p in select_positions:
        if p["hasPlaceholder"] and p["placeholderText"] == "请选择":
            log("MODEL", f"发现 placeholder 下拉框 (y={p['y']})，将按品牌空间关系精确定位型号框")
            break

    # 查找型号下拉框：
    # 已知品牌坐标时，直接取品牌同一行、右边最近的那个下拉框作为型号框
    #（不再要求它显示"请选择"或包含目标型号文字；平台默认占位如 E2 会导致旧逻辑漏掉真型号框，
    # 反而去选下面另一个 placeholder 属性框）。
    # 品牌坐标未知时，回退到 placeholder / selectedText 包含型号文字 的匹配。
    model_select_info = page.evaluate("""
        (params) => {
            let modelKeyword = params.modelKeyword;
            let brandX = params.brandX;
            let brandY = params.brandY;
            const Y_TOLERANCE = 8; // 同行允许像素误差
            let selects = document.querySelectorAll('.ecom-g-select');

            // 第1优先级：品牌坐标已知时，取品牌右侧、同一行的第一个下拉框（型号框）
            if (brandX >= 0 && brandY >= 0) {
                let sameRowRight = [];
                for (let s of selects) {
                    let rect = s.getBoundingClientRect();
                    let x = Math.round(rect.left);
                    let y = Math.round(rect.top);
                    if (y < 0 || y > window.innerHeight) continue;
                    if (x > brandX && Math.abs(y - brandY) <= Y_TOLERANCE) {
                        sameRowRight.push({ el: s, x: x, y: y });
                    }
                }
                if (sameRowRight.length > 0) {
                    sameRowRight.sort((a, b) => a.x - b.x);
                    let s = sameRowRight[0];
                    let ph = s.el.querySelector('.ecom-g-select-selection-placeholder');
                    let sel = s.el.querySelector('.ecom-g-select-selection-item');
                    let selectedText = sel ? (sel.title || sel.innerText || '').trim() : '';
                    let placeholderText = ph ? ph.innerText.trim() : '';
                    return [{
                        x: s.x, y: s.y,
                        type: 'spatial_model',
                        selectedText: selectedText,
                        placeholderText: placeholderText
                    }];
                }
            }

            // 第2优先级：placeholder / 已包含型号文字（品牌坐标未知时的兜底）
            let candidates = [];
            for (let s of selects) {
                let rect = s.getBoundingClientRect();
                let x = Math.round(rect.left);
                let y = Math.round(rect.top);
                if (y < 0 || y > window.innerHeight) continue;
                let ph = s.querySelector('.ecom-g-select-selection-placeholder');
                let sel = s.querySelector('.ecom-g-select-selection-item');
                let selectedText = sel ? (sel.title || sel.innerText || '').trim() : '';
                let placeholderText = ph ? ph.innerText.trim() : '';
                if (placeholderText === '请选择') {
                    candidates.push({ x: x, y: y, type: 'placeholder', selectedText: selectedText, placeholderText: placeholderText });
                } else if (selectedText.includes(modelKeyword)) {
                    candidates.push({ x: x, y: y, type: 'already_selected', selectedText: selectedText, placeholderText: placeholderText });
                }
            }
            return candidates;
        }
    """, {"modelKeyword": model, "brandX": brand_x, "brandY": brand_y})

    if not model_select_info:
        log("MODEL", "未找到型号下拉框!")
        return {"success": False, "model_value": "", "error": "未找到型号下拉框"}

    # ============ OPPO 机型逻辑（用户钉死方案：从草稿全名反推纯型号并强制写入）============
    # 实测 OPPO 型号下拉选项里根本没有纯型号（选项是一堆 E2/A6/双核… 乱码，
    # 或只有带 OPPO 前缀项），无论如何"选"都选不上。改用用户方案：
    #   1) 从草稿全名剔除 成色/品牌名/商品类目 → 得到纯型号（如 Find X9s Pro）；
    #   2) 把纯型号真实写入型号字段（绕开下拉选项），并在 Step15 后再次钉死（refix）。
    if _is_oppo_brand(brand_cn, brand_en) and model:
        full_title = parsed.get("raw", "")
        pure = extract_oppo_pure_model(full_title, model)
        log("MODEL", f"OPPO 钉死逻辑：从草稿全名反推纯型号: 全名='{full_title}' -> 纯型号='{pure}' (解析型号='{model}')")
        if pure:
            # 闸门：型号框当前显示值若已是纯型号（如 'Find X9s Pro'，无 OPPO 前缀），
            # 直接确认成功，绝不调用 force_write / 打开下拉框——否则会把已正确的型号清空成 E2。
            current_val = next((mi.get("selectedText", "") for mi in model_select_info if mi.get("selectedText")), "")
            # 严格判定：只有【原始显示值完全等于纯型号】才说明不带任何 OPPO 前缀、已是正确值。
            # 不能用"归一化相等"判断已正确：如 'OPPO Find X8s+' 剥离 OPPO 后归一化 == 'find x8s+'
            # 与目标纯型号归一化一致，但真实显示值仍带 'OPPO ' 前缀，平台会拒绝
            # （'属性值"OPPO Find X8s+"不在"型号"的最新可选范围'）。凡带前缀的值都必须强制重写剥离。
            if current_val and current_val == pure:
                log("MODEL", f"OPPO 型号框当前已是纯型号 '{current_val}'，与目标纯型号 '{pure}' 完全一致，无需重写，直接确认")
                SELECTED_MODEL_XY["x"] = model_select_info[0].get("x", brand_x) if model_select_info else brand_x
                SELECTED_MODEL_XY["y"] = model_select_info[0].get("y", brand_y) if model_select_info else brand_y
                return {"success": True, "model_value": pure, "error": ""}
            # 取得型号框新鲜坐标（优先 pin；失败则用下拉定位坐标兜底）。
            # hint 传 model_select_info 新鲜型号坐标（而非可能陈旧的品牌坐标），避免跨商品污染。
            fresh_mx = model_select_info[0].get("x", brand_x) if model_select_info else brand_x
            fresh_my = model_select_info[0].get("y", brand_y) if model_select_info else brand_y
            pin = _pin_and_locate_model_box(page, fresh_mx, fresh_my, disp_cn, disp_en)
            if pin.get("ok"):
                # 用触发器中心坐标更准，避免用左上角导致 force_write 距离计算偏差
                mx, my = pin.get("cx", pin.get("x", -1)), pin.get("cy", pin.get("y", -1))
                SELECTED_MODEL_XY["selector"] = pin.get("selector", "")
                log("MODEL", f"OPPO 钉死逻辑：pin 成功，method={pin.get('method')}, 将用标记定位 force_write")
            elif model_select_info:
                mx, my = model_select_info[0].get("x", brand_x), model_select_info[0].get("y", brand_y)
            else:
                mx, my = brand_x, brand_y
            SELECTED_MODEL_XY["x"] = mx
            SELECTED_MODEL_XY["y"] = my
            ok = force_write_oppo_model(page, mx, my, pure)
            if not ok:
                log("MODEL", "OPPO 钉死逻辑：强制写入首次未确认成功，尝试重新 pin 后再试")
                repin = _pin_and_locate_model_box(page, -1, -1, disp_cn, disp_en)
                if repin.get("ok"):
                    mx, my = repin.get("cx", repin.get("x", -1)), repin.get("cy", repin.get("y", -1))
                    SELECTED_MODEL_XY["x"] = mx
                    SELECTED_MODEL_XY["y"] = my
                    SELECTED_MODEL_XY["selector"] = repin.get("selector", "")
                    log("MODEL", f"OPPO 钉死逻辑：重新 pin 成功，method={repin.get('method')}, 再次强制写入")
                    ok = force_write_oppo_model(page, mx, my, pure)
                else:
                    log("MODEL", "OPPO 钉死逻辑：重新 pin 失败，用原坐标再试一次")
                    ok = force_write_oppo_model(page, mx, my, pure)
            if ok:
                log("MODEL", f"OPPO 钉死逻辑：已把纯型号 '{pure}' 真实写入型号字段")
                return {"success": True, "model_value": pure, "error": ""}
            # 用户明确禁止 OPPO 回退到下拉框搜索（绝大部分情况搜不到，还会误点成色/保修框）。
            # 强制写入失败即视为型号选择失败，由上层流程兜底保存草稿，避免继续破坏页面。
            log("MODEL", f"OPPO 钉死逻辑：强制写入失败，不再回退下拉搜索，型号 '{pure}' 写入失败")
            return {"success": False, "model_value": "", "error": f"OPPO 强制写入纯型号 '{pure}' 失败"}
        else:
            log("MODEL", "OPPO 反推纯型号为空，无法强制写入")
            return {"success": False, "model_value": "", "error": "OPPO 反推纯型号为空"}

    for info in model_select_info:
        if info["type"] == "spatial_model":
            log("MODEL", f"按品牌空间关系定位到型号框 (x={info['x']}, y={info['y']}, selected='{info.get('selectedText','')}')，将走关键字搜索")
            # 草稿中型号已经正确时直接通过。不要为了“重新选一次”强行打开
            # 下拉框：抖店的规格升级弹窗可能让已有值的下拉框暂时无法展开，
            # 这不应把原本正确的型号误判为失败。仍使用严格型号比较，不接受截断值。
            selected_now = info.get("selectedText", "")
            if selected_now:
                if (_ov_key is not None and exact and
                        _norm_model_loose(selected_now).casefold() ==
                        _norm_model_loose(exact).casefold()):
                    log("MODEL", f"型号框当前值 '{selected_now}' 已等于强制映射 '{exact}'，直接通过，不再打开或滚动下拉框")
                    return {"success": True, "model_value": selected_now, "error": ""}
                acceptable_now, reason_now = _is_acceptable_model_match(
                    selected_now, model, brand_cn, brand_en
                )
                if acceptable_now and (
                    not _model_has_upgrade_field(model)
                    or _selected_retains_upgrade(selected_now, model, brand_cn, brand_en)
                ):
                    log("MODEL", f"型号已正确选中，直接通过: '{selected_now}'")
                    return {"success": True, "model_value": selected_now, "error": ""}
                log("MODEL", f"当前已选型号与目标不一致，继续搜索: {reason_now}")
        elif info["type"] == "already_selected":
            # 非 OPPO 品牌：已选中则直接返回
            if not _is_oppo_brand(brand_cn, brand_en):
                log("MODEL", f"型号已选: '{info['selectedText']}'")
                return {"success": True, "model_value": info["selectedText"], "error": ""}

    # 目标型号下拉框坐标（用于精确定位，避免同一 y 轴上误点到品牌框）
    target_select = model_select_info[0] if model_select_info else {}
    model_x = target_select.get("x", -1)
    model_y = target_select.get("y", brand_y)
    log("MODEL", f"目标型号下拉框坐标: x={model_x}, y={model_y}")

    # ---- 坐标锁定：切回基础信息页 + 把型号行滚到视口固定位置 + 重新定位型号框 ----
    # 消除"页面滚动导致旧坐标失效、误点/误搜到错误下拉框（如 CPU/芯片列表）"的问题。
    # 关键修复：hint 必须传"品牌框"坐标(brand_x, brand_y)，而不是型号框坐标。
    # 这样 _pin_and_locate_model_box 才能正确定位品牌框、再把型号框定为它右侧同行框，
    # 从而"直接点击型号框中心"，绝不会误点到左侧的品牌框（同高同行、仅 x 位移不同）。
    pin = _pin_and_locate_model_box(page, brand_x, brand_y, disp_cn, disp_en)
    if pin.get("ok"):
        model_x = pin.get("cx", pin["x"])
        model_y = pin.get("cy", pin["y"])
        log("MODEL", f"[坐标锁定] 品牌框(x={pin.get('brandX')},y={pin.get('brandY')}) → 型号框中心: x={model_x}, y={model_y}（型号框严格在品牌框右侧，不会误点品牌框）")
    else:
        log("MODEL", f"[坐标锁定] 未能重新定位型号框（hint=品牌框坐标 brand_x={brand_x}, brand_y={brand_y}），沿用定位坐标 x={model_x}, y={model_y}")

    # 记录坐标供发布前终检复用
    SELECTED_MODEL_XY["x"] = model_x
    SELECTED_MODEL_XY["y"] = model_y

    # 对齐用纯型号（剥离品牌+去括号+去空格，全小写）：传给选择匹配逻辑，
    # 使"纯型号选项"(如 nova 14 活力版) 能正确对齐命中，不被误判为兄弟变体拒绝。
    _pure_align = _norm_model_loose(_strip_brand_tokens(model, brand_cn, brand_en))

    # 依次尝试每个搜索关键字
    for keyword in search_keywords:
        log("MODEL", f"准备尝试搜索关键字: '{keyword}'")
        # 数据表中的精确映射统一走 DOM 锁定路径，禁止使用随滚动失效的旧坐标。
        # 该机制覆盖 Pura X、K70 至尊、Note 14 等全部映射项，不再写型号特判。
        exact_target = plan["target_model_exact"]
        if exact_target and keyword.casefold() == exact_target.casefold():
            exact_dom_keyword = exact_target
            exact_dom_norm = _norm_model_loose(exact_dom_keyword).casefold()
            exact_dom_label = plan["target_model_override_key"] or exact_dom_keyword
            try:
                repin = _pin_and_locate_model_box(page, brand_x, brand_y, disp_cn, disp_en)
                if repin.get("ok"):
                    pura_box = page.locator('[data-wb-pin-model="1"]')
                    pura_trigger = pura_box.locator('.ecom-g-select-selector')
                    if pura_trigger.count() == 0:
                        pura_trigger = pura_box
                    pura_trigger.first.click(timeout=3000, force=True)
                    time.sleep(0.5)
                    pura_input = pura_box.locator('input').first
                    if pura_input.count() and pura_input.is_visible():
                        pura_input.fill(exact_dom_keyword)
                        log("MODEL", f"{exact_dom_label} 专项：已在钉死的型号框 DOM 内输入 '{exact_dom_keyword}'（无坐标点击）")
                        time.sleep(0.8)
                        controls_id = (pura_input.get_attribute("aria-controls") or
                                       pura_trigger.first.get_attribute("aria-controls") or
                                       pura_trigger.first.get_attribute("aria-owns") or "")
                        if controls_id:
                            pura_listbox = page.locator(f'[id="{controls_id}"]')
                            pura_option = pura_listbox.locator('[role="option"]').filter(
                                has_text=re.compile(re.escape(exact_dom_keyword), re.I)
                            )
                            log("MODEL", f"精确映射：已锁定型号框关联弹层 aria-controls='{controls_id}'")
                        else:
                            pura_option = page.locator('[role="listbox"] [role="option"]').filter(
                                has_text=re.compile(re.escape(exact_dom_keyword), re.I)
                            )
                            log("MODEL", "精确映射：型号框无 aria-controls，仅在可见 listbox 内查找")
                        for option_index in range(pura_option.count()):
                            option = pura_option.nth(option_index)
                            if option.is_visible():
                                option.click(timeout=3000)
                                time.sleep(0.8)
                                actual_pura = page.locator(
                                    '[data-wb-pin-model="1"] .ecom-g-select-selection-item'
                                ).first
                                actual_text = ((actual_pura.get_attribute("title") or actual_pura.inner_text())
                                               if actual_pura.count() else "").strip()
                                if _norm_model_loose(actual_text).casefold() == exact_dom_norm:
                                    log("MODEL", f"{exact_dom_label} 专项 DOM 选择并验证成功: '{actual_text}'")
                                    return {"success": True, "model_value": actual_text,
                                            "error": "", "matchLevel": 0}
                                log("MODEL", f"{exact_dom_label} 专项 DOM 点击后值不正确: '{actual_text}'")
                                break

                        # rc-select 的可见选项有时没有 role=option，而是 listbox 的虚拟列表
                        # 兄弟节点。仍从当前 input 的 aria-controls 出发限定弹层，再取精确文本
                        # 所属的最小选项容器；这里只返回已确认选项的中心，不猜型号框坐标。
                        option_point = page.evaluate(r"""
                            (params) => {
                                const norm=s=>(s||'').toLowerCase().replace(/[\s()（）]/g,'');
                                let lb=params.controlsId ? document.getElementById(params.controlsId) : null;
                                let root=lb;
                                for(let i=0;i<5&&root&&root.parentElement;i++,root=root.parentElement){
                                    let matches=[...root.querySelectorAll(
                                      '[role="option"],.ecom-g-select-item,.ecom-g-select-item-option,'+
                                      '.ecom-g-select-item-option-content,.rc-virtual-list-holder-inner>div'
                                    )].filter(el=>{
                                      let r=el.getBoundingClientRect(), st=getComputedStyle(el);
                                      return r.width>0&&r.height>0&&st.display!=='none'&&st.visibility!=='hidden'&&
                                             norm(el.innerText)===params.target;
                                    });
                                    if(matches.length){
                                      matches.sort((a,b)=>{let ar=a.getBoundingClientRect(),br=b.getBoundingClientRect();return (ar.width*ar.height)-(br.width*br.height);});
                                      let el=matches[0].closest('[role="option"],.ecom-g-select-item,.ecom-g-select-item-option')||matches[0];
                                      let r=el.getBoundingClientRect();
                                      return {cx:Math.round(r.left+r.width/2),cy:Math.round(r.top+r.height/2),text:(el.innerText||'').trim()};
                                    }
                                }
                                return null;
                            }
                        """, {"controlsId": controls_id, "target": exact_dom_norm})
                        if option_point:
                            page.mouse.click(option_point["cx"], option_point["cy"])
                            time.sleep(0.8)
                            actual_pura = page.locator('[data-wb-pin-model="1"] .ecom-g-select-selection-item').first
                            actual_text = ((actual_pura.get_attribute("title") or actual_pura.inner_text())
                                           if actual_pura.count() else "").strip()
                            if _norm_model_loose(actual_text).casefold() == exact_dom_norm:
                                log("MODEL", f"{exact_dom_label} 专项关联弹层选择并验证成功: '{actual_text}'")
                                return {"success": True, "model_value": actual_text,
                                        "error": "", "matchLevel": 0}
                    else:
                        log("MODEL", "精确映射：钉死型号框内未找到可见 input，拒绝坐标回退")
                else:
                    log("MODEL", "精确映射：型号框 DOM pin 失败，拒绝坐标回退")
            except Exception as pura_dom_exc:
                log("MODEL", f"精确映射 DOM 路径失败: {pura_dom_exc}")
            return {"success": False, "model_value": "",
                    "error": "精确映射目标框 DOM 输入/选择失败，已禁止坐标漂移回退"}
        result = _try_select_model_with_keyword(page, keyword, category, brand_y, original_model=model,
                                               model_x=model_x, model_y=model_y,
                                               oppo_brand=_is_oppo_brand(brand_cn, brand_en), pure_model=_pure_align,
                                               brand_cn=disp_cn, brand_en=disp_en, brand_x=brand_x)
        if result["success"]:
            selected = result.get("model_value", "")
            match_level = result.get("matchLevel", 0)
            # 强制映射来自已确认的平台官方枚举。当前关键字与映射值一致，且下拉选择
            # 已在 _try_select_model_with_keyword 中完成两次稳定性验证时，应直接接受。
            # 否则 Redmi Note 14 -> Note 14 5G 会被下面通用的“扩展后缀”保护误拒绝，
            # 明明精确选中后又继续搜索并最终报失败。
            if _ov_key is not None and keyword == exact and selected:
                log("MODEL", f"强制映射精确选中并验证通过: '{selected}'（目标 '{model}'），直接接受")
                return result
            # 关键修复：选中后严格验证，避免截断导致的误匹配
            # 例如原始型号 "X50i"，但选中了 "X50" → 需要继续尝试更精确的关键字
            # 例如原始型号 "Mate 70 Pro+"，但选中了 "Mate 70" → 截断匹配，需拒绝
            if model and selected:
                # 校验门：先在归一化（剥离品牌前缀）后比较，避免 "真我GT Pro" 因带品牌
                # 而击穿截断校验，导致 "GT" 被误认为 "GT Pro" 的合法匹配而漏掉 Pro 后缀
                acceptable, reason = _is_acceptable_model_match(selected, model, brand_cn, brand_en)
                if acceptable:
                    # ===== 全局：带 Pro/Ultra/Max 升级字段的型号，禁止接受丢失这些字段的截断匹配 =====
                    # 例如 model="K90 Pro Max" 却匹配到 selected="K90 Pro"（丢失 Max），不能接受，
                    # 必须 continue 尝试更精确（更长）的关键字，避免把 Pro Max 机型误选成 Pro 基础款。
                    # 对所有品牌生效（OPPO 的纯型号改写在下方专用分支继续处理）。
                    if _model_has_upgrade_field(model) and not _selected_retains_upgrade(selected, model, brand_cn, brand_en):
                        log("MODEL", f"型号 '{model}' 含升级字段(Pro/Ultra/Max)，但选中值 '{selected}' 丢失了这些字段，跳过截断匹配")
                        continue
                    # ===== OPPO 专用（钉死）：下拉选项带 "OPPO " 前缀（如 "OPPO Reno14 Pro"），
                    # 选中后去掉 OPPO 即为纯型号，直接接受（无需寻找不存在的纯型号选项）。=====
                    if _is_oppo_brand(brand_cn, brand_en):
                        pure = _strip_brand_tokens(selected, brand_cn, brand_en)
                        if not pure:
                            pure = model
                        result["model_value"] = pure
                        if selected != pure:
                            _rewrite_oppo_model_value(page, model_x, model_y, pure, old_value=selected)
                        log("MODEL", f"OPPO 选中(带前缀)'{selected}' -> 去除OPPO -> 纯型号='{pure}'")
                    final_model = result.get("model_value", selected)
                    log("MODEL", f"搜索成功，选中型号: '{final_model}' (匹配层级: {match_level})")
                    return result
                else:
                    log("MODEL", f"⚠ {reason}；继续尝试下一个更精确的关键字...")
                    continue
            # selected 为空等异常分支：维持原行为接受
            log("MODEL", f"搜索成功，选中型号: '{selected}' (匹配层级: {match_level})")
            return result
        log("MODEL", f"关键字 '{keyword}' 未匹配，继续下一个...")

    # 所有关键字都失败
    log("MODEL", f"所有搜索关键字均未匹配: {search_keywords}")
    return {"success": False, "model_value": "", "error": f"未找到匹配型号，已尝试: {search_keywords}"}


def ensure_brand_model(page, parsed: dict, category: str = "") -> dict:
    """确保品牌和型号正确：验证品牌，不匹配时主动修改；搜索并选择型号"""
    log("CHECK", "===== 开始品牌/型号检查 =====")

    plan = resolve_brand_model_plan(parsed)
    log("CHECK", f"目标方案: 品牌='{plan['target_brand_value']}', 型号='{plan['target_model_exact'] or plan['source_model']}'")
    select_positions = probe_select_positions(page)

    # === 品牌验证与修改 ===
    brand_result = verify_brand(page, parsed, select_positions)
    brand_changed = False
    if brand_result["matched"]:
        log("CHECK", f"品牌 OK: '{brand_result['brand_value']}'")
    else:
        log("CHECK", f"品牌不匹配: 标题='{parsed.get('brand_cn', '')}', 下拉框='{brand_result['brand_value']}'")
        if brand_result["brand_value"] and brand_result["brand_y"] >= 0:
            # 主动修改品牌下拉框
            change_result = change_brand(page, parsed, brand_result["brand_y"])
            if change_result["success"]:
                brand_result["brand_value"] = change_result["brand_value"]
                brand_result["matched"] = _brand_value_matches_plan(plan, change_result["brand_value"])
                brand_changed = True
                log("CHECK", f"品牌已修改为: '{change_result['brand_value']}'")
            else:
                log("CHECK", f"品牌修改失败: {change_result.get('error', '')}")
        elif not brand_result["brand_value"]:
            log("CHECK", "⚠ 品牌下拉框为空，可能需要手动选择")

    # 品牌变化会触发型号选项异步刷新和整页重排；必须等待并重新获取品牌/型号坐标。
    if brand_changed:
        time.sleep(2.5)
        select_positions = probe_select_positions(page)
        refreshed_brand = verify_brand(page, parsed, select_positions)
        if refreshed_brand.get("matched"):
            brand_result = refreshed_brand
            log("CHECK", f"品牌联动刷新后复核通过: '{brand_result['brand_value']}'")
        else:
            brand_result["matched"] = False
            log("CHECK", f"品牌联动刷新后复核失败: '{refreshed_brand.get('brand_value', '')}'")
    elif not brand_result["matched"]:
        select_positions = probe_select_positions(page)

    # === 型号选择 ===
    model_result = select_model(page, parsed, select_positions, category,
                                brand_result.get("brand_y", -1), brand_result.get("brand_x", -1))
    if model_result["success"]:
        log("CHECK", f"型号 OK: '{model_result['model_value']}'")
    else:
        log("CHECK", f"型号选择失败: {model_result.get('error', '')}")

    # A populated select is not necessarily the correct brand/model.
    brand_ok = bool(brand_result.get("matched"))
    model_ok = bool(model_result.get("success")) and bool(model_result.get("model_value"))

    log("CHECK", f"===== 最终检查: 品牌={'OK' if brand_ok else 'FAIL'}, 型号={'OK' if model_ok else 'FAIL'} =====")

    if not brand_ok or not model_ok:
        log("CHECK", "⚠ 品牌/型号不完整，必须转人工；禁止删图、保存和发布")

    return {
        "brand_ok": brand_ok,
        "model_ok": model_ok,
        "brand_value": brand_result["brand_value"],
        "brand_y": brand_result["brand_y"],
        "model_value": model_result.get("model_value", ""),
        "all_ok": brand_ok and model_ok
    }


def final_check(page, parsed: dict, category: str, brand_value: str = "", model_value: str = "") -> bool:
    """回看检查：验证商品标题是否规范，品牌和型号是否对应
    
    参数:
        brand_value: ensure_brand_model 返回的品牌下拉框值（避免重新遍历DOM误判）
        model_value: ensure_brand_model 返回的型号下拉框值（避免重新遍历DOM误判）
    """
    log("VERIFY", "===== 回看检查 =====")

    all_ok = True

    title_state = page.evaluate("""
        () => {
            let input = document.getElementById('pg-title-input');
            if (!input) return { value: '', prefix: '' };
            let wrapper = input.closest('.ecom-g-input-group');
            let prefix = '';
            if (wrapper) {
                let addon = wrapper.querySelector('.ecom-g-input-group-addon');
                if (addon) prefix = addon.innerText.trim();
            }
            return { value: input.value, prefix: prefix };
        }
    """)
    input_value = title_state.get("value", "")
    prefix_value = title_state.get("prefix", "")
    full_title = f"{prefix_value} {input_value}".strip() if prefix_value else input_value
    log("VERIFY", f"完整标题: '{full_title}'")

    if category and category not in full_title:
        log("VERIFY", f"⚠ 类目'{category}'不在标题中（跳过修复，不修改标题）")
        all_ok = False
    else:
        log("VERIFY", f"✓ 类目'{category}'在标题中")

    plan = resolve_brand_model_plan(parsed)
    # 标题验证使用标题中真实业务型号；型号框验证使用平台目标型号。两者不能
    # 混用（例如标题 K70 至尊版，平台枚举 K70至尊）。
    model = plan["title_model"]
    model_in_title, model_match_kind = _model_appears_in_title(model, full_title)
    if model and not model_in_title:
        log("VERIFY", f"⚠ 型号'{model}'不在标题中")
        all_ok = False
    else:
        if model_match_kind == "plus_omitted":
            model_no_plus = model.rstrip('+')
            log("VERIFY", f"✓ 型号'{model}'在标题中（平台省略了+号，显示为'{model_no_plus}'）")
        elif model and model not in full_title:
            log("VERIFY", f"✓ 型号'{model}'与标题格式化匹配（忽略空格/大小写/分隔符）")
        else:
            log("VERIFY", f"✓ 型号'{model}'在标题中")

    # 使用传入的品牌值（避免重新遍历DOM导致误判成色等为品牌）
    brand_cn = plan["target_brand_cn"]
    if not brand_value:
        # 如果没有传入，尝试从DOM获取（fallback）
        brand_value = page.evaluate("""
            () => {
                let selects = document.querySelectorAll('.ecom-g-select');
                for (let s of selects) {
                    let sel = s.querySelector('.ecom-g-select-selection-item');
                    if (sel && sel.innerText.trim()) {
                        let rect = s.getBoundingClientRect();
                        let y = Math.round(rect.top);
                        if (y > 0 && y < window.innerHeight) {
                            return sel.title || sel.innerText.trim();
                        }
                    }
                }
                return '';
            }
        """)
    if brand_cn and _brand_value_matches_plan(plan, brand_value):
        log("VERIFY", f"✓ 品牌: '{brand_value}' 与 '{brand_cn}' 匹配")
    else:
        log("VERIFY", f"⚠ 品牌不匹配: 期望含'{brand_cn}', 实际='{brand_value}'")
        all_ok = False

    # 使用传入的型号值
    if not model_value:
        # fallback：从DOM获取与品牌同y坐标的select
        model_value = page.evaluate("""
            (brandVal) => {
                let selects = document.querySelectorAll('.ecom-g-select');
                let results = [];
                for (let s of selects) {
                    let sel = s.querySelector('.ecom-g-select-selection-item');
                    if (!sel) continue;
                    let text = (sel.title || sel.innerText || '').trim();
                    if (!text || text === brandVal) continue;
                    let rect = s.getBoundingClientRect();
                    let y = Math.round(rect.top);
                    if (y > 0 && y < window.innerHeight) {
                        results.push({ text: text, y: y });
                    }
                }
                let brandY = 0;
                for (let s of selects) {
                    let sel = s.querySelector('.ecom-g-select-selection-item');
                    if (sel && (sel.title || sel.innerText || '').trim() === brandVal) {
                        brandY = Math.round(s.getBoundingClientRect().top);
                        break;
                    }
                }
                for (let r of results) {
                    if (r.y === brandY) return r.text;
                }
                return results.length > 0 ? results[0].text : '';
            }
        """, brand_value)
    if model and model in model_value:
        log("VERIFY", f"✓ 型号: '{model_value}' 包含 '{model}'")
    elif model_value:
        log("VERIFY", f"✓ 型号已选: '{model_value}'")
    else:
        log("VERIFY", f"⚠ 型号为空")
        all_ok = False

    if not brand_value:
        log("VERIFY", "⚠ 品牌下拉框为空!")
        all_ok = False
    if not model_value:
        log("VERIFY", "⚠ 型号下拉框为空!")
        all_ok = False

    if all_ok:
        log("VERIFY", "===== 回看检查全部通过 ✓ =====")
    else:
        log("VERIFY", "===== 回看检查发现问题，已尝试修复 =====")

    return all_ok


def dismiss_popup(page, label: str = "") -> bool:
    """检测并关闭平台弹窗"""
    tag = f"POPUP{f'({label})' if label else ''}"

    dismissed = page.evaluate("""
        () => {
            let buttons = document.querySelectorAll('button, .ant-btn, [role=button]');
            for (let btn of buttons) {
                if (btn.innerText.trim() === '我知道了') {
                    let rect = btn.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        btn.click();
                        return 'clicked: ' + btn.innerText.trim();
                    }
                }
            }
            let dialogs = document.querySelectorAll('[role=dialog], .ant-modal, .ecom-modal, .doudian-modal');
            for (let d of dialogs) {
                if (d.offsetHeight > 0) {
                    let btn = d.querySelector('button, .ant-btn-primary');
                    if (btn) {
                        btn.click();
                        return 'clicked dialog btn: ' + btn.innerText.trim();
                    }
                }
            }
            let modals = document.querySelectorAll('.ant-modal-wrap, .ecom-modal-wrap, [class*="modal"]');
            for (let m of modals) {
                if (m.offsetHeight > 0 && m.innerText.includes('升级商品规格')) {
                    let btn = m.querySelector('button');
                    if (btn) {
                        btn.click();
                        return 'clicked modal btn: ' + btn.innerText.trim();
                    }
                }
            }
            return 'no popup';
        }
    """)

    if "clicked" in dismissed:
        log(tag, f"已关闭弹窗: {dismissed}")
        time.sleep(0.5)
        return True
    else:
        log(tag, "未检测到弹窗")
        return False


def _dismiss_blocking_overlay_before_save(page) -> dict:
    """Close only non-affirming modal/drawer actions before saving a draft."""
    result = page.evaluate(r"""
        () => {
            const visible = el => { const r=el.getBoundingClientRect(); return r.width>0&&r.height>0; };
            const overlays=[...document.querySelectorAll(
                '[role="dialog"], .ant-modal-wrap, .ecom-g-modal-wrap, [class*="drawer"], [class*="modal-wrap"], [id$="__func_preview__"]'
            )].filter(visible);
            const safeWords=['取消','关闭','暂不','稍后','我知道了','知道了'];
            for(const overlay of overlays.reverse()){
                const text=(overlay.innerText||'').trim().slice(0,500);
                const buttons=[...overlay.querySelectorAll('button,[role="button"]')].filter(visible);
                for(const word of safeWords){
                    const button=buttons.find(b=>{
                        const label=(b.innerText||b.getAttribute('aria-label')||'').trim();
                        return label===word||label.includes(word);
                    });
                    if(button){ button.click(); return {found:true,closed:true,action:word,text}; }
                }
                const close=overlay.querySelector(
                    '[aria-label="Close"],[aria-label="关闭"],.ant-modal-close,.ecom-g-modal-close,'+
                    '[class*="drawer-close"],[class*="modal-close"]'
                );
                if(close&&visible(close)){ close.click(); return {found:true,closed:true,action:'close-icon',text}; }
                // 属性补全面板（id 后缀 __func_preview__）的关闭按钮没有稳定文本/aria，
                // 只允许点击该浮层右上角的小型按钮，绝不点击确认/提交区域。
                if((overlay.id||'').endsWith('__func_preview__')){
                    const or=overlay.getBoundingClientRect();
                    const topRight=buttons.filter(b=>{
                        const r=b.getBoundingClientRect();
                        return r.width>0&&r.width<=72&&r.height>0&&r.height<=72&&
                               r.top<=or.top+96&&r.right>=or.right-120;
                    }).sort((a,b)=>b.getBoundingClientRect().right-a.getBoundingClientRect().right)[0];
                    if(topRight){ topRight.click(); return {found:true,closed:true,action:'func-preview-top-right-close',text}; }
                }
                return {found:true,closed:false,action:'',text};
            }
            return {found:false,closed:false,action:'',text:''};
        }
    """)
    if result.get("found") and not result.get("closed"):
        try:
            page.keyboard.press("Escape")
            time.sleep(0.4)
            result["action"] = "Escape"
        except Exception:
            pass
    if result.get("found"):
        log("STEP10", "保存前安全清浮层: closed=%s action=%s text=%r" % (
            result.get("closed"), result.get("action"), result.get("text", "")[:180]))
        time.sleep(0.6)
    return result


# ========== 图片管理 ==========

def _image_dimensions(path: str) -> tuple:
    """Read PNG/JPEG dimensions with the standard library; return (0, 0) on failure."""
    try:
        with open(path, "rb") as image_file:
            head = image_file.read(24)
            if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
                return struct.unpack(">II", head[16:24])
            if head[:2] != b"\xff\xd8":
                return (0, 0)
            image_file.seek(2)
            while True:
                marker_start = image_file.read(1)
                if not marker_start:
                    break
                if marker_start != b"\xff":
                    continue
                marker = image_file.read(1)
                while marker == b"\xff":
                    marker = image_file.read(1)
                if not marker or marker in (b"\xd8", b"\xd9"):
                    continue
                length_bytes = image_file.read(2)
                if len(length_bytes) != 2:
                    break
                length = struct.unpack(">H", length_bytes)[0]
                if marker[0] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                                 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    data = image_file.read(5)
                    if len(data) == 5:
                        height, width = struct.unpack(">HH", data[1:5])
                        return (width, height)
                    break
                image_file.seek(max(0, length - 2), 1)
    except (OSError, ValueError, struct.error):
        pass
    return (0, 0)


def _is_valid_main_area_image(path: str) -> bool:
    """DouDian 1:1 main area requires a square image of at least 600x600."""
    width, height = _image_dimensions(path)
    return width >= 600 and height >= 600 and width == height

def delete_all_images(page) -> bool:
    """逐个删除图文信息中的所有可删除图片"""
    log("DELETE", "开始删除原有图片")

    deleted_count = 0

    while True:
        has_shanchu = page.evaluate("""
            () => {
                let w = document.querySelectorAll('.index-module_imgWrapper__xOFF7')[0];
                if (!w) return false;
                let uses = w.querySelectorAll('use');
                for (let u of uses) {
                    let href = u.getAttribute('href') || u.getAttribute('xlink:href') || '';
                    if (href.includes('shanchu')) return true;
                }
                return false;
            }
        """)

        count = page.evaluate("""
            () => document.querySelectorAll('.index-module_imgWrapper__xOFF7').length
        """)

        if not has_shanchu:
            log("DELETE", f"第一张图片无删除按钮，停止删除 (当前 {count} 张)")
            break

        log("DELETE", f"当前图片数量: {count}")

        img_wrapper = page.locator('.index-module_imgWrapper__xOFF7').first
        img_wrapper.hover()
        time.sleep(0.8)

        # 使用 evaluate 点击删除按钮（支持 href 和 xlink:href 两种属性）
        page.evaluate("""
            () => {
                let w = document.querySelectorAll('.index-module_imgWrapper__xOFF7')[0];
                if (!w) return;
                let uses = w.querySelectorAll('use');
                for (let u of uses) {
                    let href = u.getAttribute('href') || u.getAttribute('xlink:href') || '';
                    if (href.includes('shanchu')) {
                        // 尝试点击可点击的父元素（SVG元素没有click方法，需用dispatchEvent）
                        let target = u.closest('svg') || u.closest('button') || u.closest('[class*="icon"]') || u.parentElement;
                        if (target) {
                            target.dispatchEvent(new Event('click', {bubbles: true}));
                        }
                        return;
                    }
                }
            }
        """)
        time.sleep(1)

        new_count = page.evaluate("""
            () => document.querySelectorAll('.index-module_imgWrapper__xOFF7').length
        """)

        if new_count < count:
            deleted_count += 1
            log("DELETE", f"删除成功! {count} → {new_count} (已删除 {deleted_count} 张)")
        else:
            log("DELETE", f"删除未生效，重试 ({new_count})")

    log("DELETE", f"删除完成，共删除 {deleted_count} 张，剩余 {count} 张")
    return True


def delete_detail_images(page) -> bool:
    """
    逐个删除商详图区域中的所有图片。
    商详图位于主图下方、价格库存上方。
    使用 .styles_imgWrapper__dqiHn 定位图片wrapper，
    悬停后点击底部控件栏中的 .styles_iconDelete__y_88a 删除图标。
    """
    log("DELETE_DETAIL", "开始删除原有商详图")

    # 先检查商详图区域是否存在
    detail_count = page.evaluate("""
        () => document.querySelectorAll('.styles_imgWrapper__dqiHn').length
    """)
    log("DELETE_DETAIL", f"初始商详图数量: {detail_count}")

    if detail_count == 0:
        log("DELETE_DETAIL", "没有商详图，无需删除")
        return True

    deleted_count = 0

    while True:
        # 检测第一张商详图是否有删除图标
        has_delete = page.evaluate("""
            () => {
                let wrapper = document.querySelector('.styles_imgWrapper__dqiHn');
                if (!wrapper) return false;
                let deleteIcon = wrapper.querySelector('.styles_iconDelete__y_88a');
                return deleteIcon !== null;
            }
        """)

        count = page.evaluate("""
            () => document.querySelectorAll('.styles_imgWrapper__dqiHn').length
        """)

        if not has_delete or count == 0:
            log("DELETE_DETAIL", f"无可删除的商详图，停止 (当前 {count} 张)")
            break

        log("DELETE_DETAIL", f"当前商详图数量: {count}")

        # 悬浮第一张商详图，显示删除按钮
        first_wrapper = page.locator('.styles_imgWrapper__dqiHn').first
        first_wrapper.hover()
        time.sleep(0.8)

        # 点击删除图标
        delete_icon = page.locator('.styles_imgWrapper__dqiHn').first.locator('.styles_iconDelete__y_88a')
        delete_icon.click()
        time.sleep(1.5)

        # 验证删除结果
        new_count = page.evaluate("""
            () => document.querySelectorAll('.styles_imgWrapper__dqiHn').length
        """)

        if new_count < count:
            deleted_count += 1
            log("DELETE_DETAIL", f"删除成功! {count} → {new_count} (已删除 {deleted_count} 张)")
        else:
            log("DELETE_DETAIL", f"删除未生效，重试 ({new_count})")
            # 可能删除成功但DOM未更新，再等一会
            time.sleep(1)
            new_count_retry = page.evaluate("""
                () => document.querySelectorAll('.styles_imgWrapper__dqiHn').length
            """)
            if new_count_retry < count:
                deleted_count += 1
                log("DELETE_DETAIL", f"重试后删除成功! {count} → {new_count_retry} (已删除 {deleted_count} 张)")
            else:
                log("DELETE_DETAIL", "重试后仍未生效，跳过此张继续")
                break

    log("DELETE_DETAIL", f"商详图删除完成，共删除 {deleted_count} 张，剩余 {count} 张")
    return True


def upload_images(page, parsed: dict) -> bool:
    """
    上传商品图片 — 只上传到 1:1 区域带星号(*)的位置。
    在 delete_all_images() 之后调用。

    页面结构:
      - 1:1 主图区域（上方）: "商品正面图 上传主图*"(第1张) + "上传辅助图*"(第2张起)
      - 3:4 主图区域（下方）: 同结构，但全局不上传，由平台从 1:1 智能裁剪或留空

    规则:
      - 只上传到 1:1 区域的前 5 个 input（固定结构）
      - 第1个 → 主图
      - 第2-5个 → 白底辅助图（按顺序填充）
      - 3:4 区域全局跳过（不依赖店铺 disabled 状态，一律按位置跳过最下方的 3:4 区）
      - 每张图上传后手动触发 change 事件

    返回 True 如果上传成功。
    """
    log("UPLOAD", "===== 开始上传商品图片 =====")

    # ===== 准备文件路径 =====
    main_path = get_main_image_path(parsed)
    aux_paths = get_aux_image_paths(parsed)

    log("UPLOAD", f"图片目录: {get_image_dir(parsed)}")
    log("UPLOAD", f"主图: {main_path} ({'存在' if os.path.isfile(main_path) else '不存在!'})")
    log("UPLOAD", f"白底图: {len(aux_paths)} 张")

    # 检查文件存在性
    if not os.path.isfile(main_path):
        log("UPLOAD", f"主图文件不存在，终止上传: {main_path}")
        return False

    main_width, main_height = _image_dimensions(main_path)
    if not _is_valid_main_area_image(main_path):
        log("UPLOAD", f"主图尺寸不合规: {main_width}x{main_height}，必须严格1:1且至少600x600")
        return False

    valid_aux_paths = [p for p in aux_paths
                       if os.path.isfile(p) and _is_valid_main_area_image(p)]
    for p in aux_paths:
        if not os.path.isfile(p):
            log("UPLOAD", f"白底图缺失: {p}")
        elif not _is_valid_main_area_image(p):
            width, height = _image_dimensions(p)
            log("UPLOAD", f"白底图尺寸不合规，跳过: {os.path.basename(p)} ({width}x{height})，要求严格1:1且至少600x600")
    if not valid_aux_paths:
        log("UPLOAD", "无有效白底图文件，仅上传主图")

    # ===== 探测上传区域 input 分布 =====
    input_map = page.evaluate(r"""
        () => {
            let inputs = document.querySelectorAll('input[type="file"]');
            let result = [];
            for (let inp of inputs) {
                let rect = inp.getBoundingClientRect();
                let y = Math.round(rect.top + rect.height / 2);

                // 标签文本：优先 <label> 直接包裹；否则向上查找最近的带文字容器
                // （部分主图区 slot 的 file input 并不被 <label> 包裹，导致旧逻辑 label 取空、漏识别）
                let label = inp.closest('label');
                let labelText = '';
                if (label) {
                    labelText = (label.innerText || '').trim();
                } else {
                    let p = inp.parentElement;
                    for (let k = 0; k < 5 && p; k++) {
                        let t = (p.innerText || '').trim();
                        if (t) { labelText = t.slice(0, 40); break; }
                        p = p.parentElement;
                    }
                }

                let hasStar = false;
                let scope = label || inp.parentElement;
                if (scope) {
                    let txt = (scope.innerText || '') + ((scope.innerHTML) || '');
                    hasStar = txt.includes('*') || txt.includes('ecom-g-form-item-required');
                }

                // 是否禁用：3:4 主图区在平台侧禁用，其 input 多为 disabled，不应作为 1:1 上传目标
                let disabled = inp.disabled === true
                    || (inp.closest('[class*="disabled"]') !== null)
                    || (inp.closest('[aria-disabled="true"]') !== null);

                result.push({
                    y: y,
                    label: labelText,
                    hasStar: hasStar,
                    disabled: disabled,
                    index: result.length
                });
            }
            return result;
        }
    """)
    log("UPLOAD", f"探测到 {len(input_map)} 个 file input:")
    for item in input_map:
        log("UPLOAD", f"  [{item['index']}] y={item['y']} star={item['hasStar']} disabled={item.get('disabled')} label='{item['label'][:40]}'")

    # ===== 语义定位上传目标 =====
    # 不使用 DOM 顺序、input[0] 或 y 坐标。只有唯一且处于可见 1:1 语义容器内的
    # “商品正面图/上传主图” input 才能成为主图目标；重复候选本身就是异常。
    semantic = page.evaluate(r"""
        () => {
            const inputs=[...document.querySelectorAll('input[type="file"]')];
            inputs.forEach(i=>{
                i.removeAttribute('data-wb-main-upload-target');
                i.removeAttribute('data-wb-aux-upload-target');
            });
            function inspect(inp){
                let label=inp.closest('label'), nearestVisible=null, semanticText='';
                let p=label||inp.parentElement;
                for(let depth=0;depth<10&&p;depth++,p=p.parentElement){
                    let text=(p.innerText||'').trim();
                    let r=p.getBoundingClientRect(), st=getComputedStyle(p);
                    if(!semanticText&&text) semanticText=text.slice(0,160);
                    if(!nearestVisible&&r.width>0&&r.height>0&&st.display!=='none'&&st.visibility!=='hidden'){
                        nearestVisible=p;
                    }
                }
                let context=''; p=nearestVisible;
                for(let depth=0;depth<4&&p;depth++,p=p.parentElement){ context+=' '+(p.innerText||''); }
                let required=(semanticText.includes('*')||!!inp.closest(
                    '.ecom-g-form-item-required,[class*="required"],[aria-required="true"]'));
                let disabled=inp.disabled||!!inp.closest('[aria-disabled="true"],[class*="disabled"]');
                let threeFour=/3\s*[:：]\s*4/.test(context);
                return {inp, text:semanticText, context:context.slice(0,500), required, disabled, threeFour,
                        active:!!nearestVisible&&!disabled};
            }
            const rows=inputs.map(inspect);
            const allMains=rows.filter(r=>r.active&&!r.threeFour&&
                r.text.includes('商品正面图')&&r.text.includes('上传主图'));
            // `required/star` is diagnostic only.  The platform removed that
            // marker from the main-image input after adding new required phone
            // properties, while its semantic identity remained exactly
            // “商品正面图/上传主图”.  Requiring the marker therefore rejects the
            // one correct input.  Safety still comes from active + non-3:4 +
            // exact semantic text + unique candidate; duplicates remain fatal.
            const mains=allMains;
            let aux=[], auxRoot=null;
            // 从唯一必填主图向上寻找最小结构容器：该容器必须只含一个主图语义
            // input，并包含辅助图语义 input。这样辅助图也不依赖 DOM 全局顺序。
            if(mains.length===1){
                let root=mains[0].inp.parentElement;
                for(let depth=0;depth<10&&root;depth++,root=root.parentElement){
                    let inside=rows.filter(r=>root.contains(r.inp));
                    let insideMains=inside.filter(r=>r.active&&r.text.includes('商品正面图')&&r.text.includes('上传主图'));
                    let insideAux=inside.filter(r=>r.active&&r.text.includes('上传辅助图'));
                    if(insideMains.length===1&&insideAux.length){ aux=insideAux; auxRoot=root; break; }
                }
            }
            if(mains.length===1) mains[0].inp.setAttribute('data-wb-main-upload-target','1');
            // auxRoot 是同时包含唯一主图 input 与辅助图 input 的最小完整字段组。
            // 抖店上传后 preview wrapper 会成为 input 的兄弟节点，因此不能把
            // “只包含一个 input 的小父节点”当成主图语义边界。
            if(auxRoot) auxRoot.setAttribute('data-wb-main-image-field','1');
            if(auxRoot) auxRoot.setAttribute('data-wb-aux-upload-group','1');
            aux.slice(0,4).forEach((r,i)=>r.inp.setAttribute('data-wb-aux-upload-target',String(i+1)));
            return {
                mainCount:mains.length, auxCount:aux.length,
                mainDebug:allMains.map(r=>({required:r.required,text:r.text,context:r.context.slice(0,180)})),
                rejectedMain:allMains.filter(r=>!mains.includes(r))
                    .map(r=>({required:r.required,disabled:r.disabled,threeFour:r.threeFour,text:r.text}))
            };
        }
    """)
    log("UPLOAD", "语义定位: 主图候选=%s, 1:1辅助图候选=%s, 排除候选=%s" %
        (semantic.get("mainCount"), semantic.get("auxCount"), semantic.get("rejectedMain")))
    if not unique_main_candidate(semantic.get("mainCount") or 0):
        log("UPLOAD", "❌ 主图语义候选不唯一，拒绝按 DOM 顺序猜测并转人工: %s" %
            semantic.get("mainDebug"))
        return False
    main_target = {"semantic": True, "label": "商品正面图/上传主图"}
    aux_targets = [
        {"semantic": True, "position": i + 1, "label": "上传辅助图"}
        for i in range(min(4, int(semantic.get("auxCount") or 0)))
    ]

    # ===== 上传主图 =====
    if main_target:
        try:
            log("UPLOAD", "--- 按唯一语义目标上传主图 → 商品正面图/上传主图 ---")
            semantic_main = page.locator('input[type="file"][data-wb-main-upload-target="1"]')
            if semantic_main.count() != 1:
                log("UPLOAD", "主图语义标记已丢失或重复，拒绝上传")
                return False
            # 在只上传主图、尚未上传任何辅助图的时间窗口建立因果证据。
            # 页面全局 delta 只作诊断；权威判定只看“商品正面图”语义槽位。
            page.evaluate(r"""
                () => {
                    const validSrc = img => {
                        const src=img.currentSrc||img.getAttribute('src')||'';
                        const r=img.getBoundingClientRect();
                        return src&&!src.startsWith('data:image/svg')&&r.width>=30&&r.height>=30 ? src : '';
                    };
                    const field=document.querySelector('[data-wb-main-image-field="1"]');
                    const mainInput=document.querySelector(
                        'input[type="file"][data-wb-main-upload-target="1"]');
                    const fieldInputs=field?[...field.querySelectorAll('input[type="file"]')]:[];
                    const previews=(scope=document)=>[...scope.querySelectorAll('.index-module_imgWrapper__xOFF7')]
                        .map(w=>({w,srcs:[...w.querySelectorAll('img')].map(validSrc).filter(Boolean)}))
                        .filter(x=>x.srcs.length);
                    window.__wbMainUploadEvidence={
                        before:new Set(previews().flatMap(x=>x.srcs)), input:mainInput,
                        beforeAll:new Set([...document.querySelectorAll('img')].map(img=>
                            img.currentSrc||img.getAttribute('src')||'').filter(Boolean)),
                        mainInputFieldIndex:fieldInputs.indexOf(mainInput),
                        deltaCount:0,
                        field:field, initialCandidateCount:1, src:'', wrapper:null,
                        ready:false, expectedSrcs:[], uploadDeltas:[]
                    };
                    document.querySelectorAll('[data-wb-main-preview-bound]').forEach(
                        e=>e.removeAttribute('data-wb-main-preview-bound'));
                }
            """)
            # Resolve the visible trigger before set_input_files.  React may
            # replace the marked input immediately after direct injection, so
            # resolving it afterwards is too late and loses the semantic link.
            pre_injection_trigger = page.evaluate(r"""
                () => {
                    const input=document.querySelector(
                        'input[type="file"][data-wb-main-upload-target="1"]');
                    if(!input) return null;
                    const visible=el=>{
                        const r=el.getBoundingClientRect(),s=getComputedStyle(el);
                        return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';
                    };
                    const candidates=[];
                    const label=input.closest('label');
                    if(label&&visible(label)) candidates.push(label);
                    if(input.id){
                        const escaped=(window.CSS&&CSS.escape)?CSS.escape(input.id):input.id;
                        const external=document.querySelector('label[for="'+escaped+'"]');
                        if(external&&visible(external)) candidates.push(external);
                    }
                    for(let p=input.parentElement,depth=0;p&&depth<8;depth++,p=p.parentElement){
                        const text=(p.innerText||'').trim();
                        const fileCount=p.querySelectorAll('input[type="file"]').length;
                        if(fileCount===1&&visible(p)&&text.includes('上传主图')) candidates.push(p);
                        for(const el of p.querySelectorAll('button,[role="button"],label,span,div')){
                            const childText=(el.innerText||'').trim();
                            const ownsOne=el.querySelectorAll('input[type="file"]').length<=1;
                            if(visible(el)&&ownsOne&&childText.includes('上传主图')) candidates.push(el);
                        }
                        if(candidates.length) break;
                    }
                    if(!candidates.length) return null;
                    const unique=[...new Set(candidates)];
                    unique.sort((a,b)=>{
                        const ar=a.getBoundingClientRect(),br=b.getBoundingClientRect();
                        return ar.width*ar.height-br.width*br.height;
                    });
                    const el=unique[0],r=el.getBoundingClientRect();
                    document.querySelectorAll('[data-wb-main-upload-trigger]').forEach(
                        node=>node.removeAttribute('data-wb-main-upload-trigger'));
                    el.setAttribute('data-wb-main-upload-trigger','1');
                    return {found:true,cx:Math.round(r.left+r.width/2),cy:Math.round(r.top+r.height/2),
                            text:(el.innerText||'').trim().slice(0,80),tag:el.tagName};
                }
            """)
            log("UPLOAD", "主图注入前语义触发器探针: %s" % pre_injection_trigger)
            used_pre_injection_chooser = False
            if pre_injection_trigger and pre_injection_trigger.get("found"):
                try:
                    semantic_trigger = page.locator('[data-wb-main-upload-trigger="1"]')
                    if semantic_trigger.count() != 1:
                        raise Exception("注入前主图触发器标记丢失或重复")
                    semantic_trigger.scroll_into_view_if_needed(timeout=5000)
                    time.sleep(0.3)
                    with page.expect_file_chooser(timeout=8000) as chooser_info:
                        semantic_trigger.click(timeout=5000)
                    chooser_info.value.set_files(main_path)
                    # The crop editor is mounted asynchronously after the
                    # trusted file chooser resolves.  A fixed sub-second sleep
                    # races React and was observed to miss the dialog even
                    # though it appeared a few seconds later.  Poll for this
                    # exact editor title; do not click a page-global Upload.
                    editor_confirm = {"found": False, "count": 0}
                    editor_deadline = time.time() + 12
                    while time.time() < editor_deadline:
                        editor_confirm = page.evaluate(r"""
                        () => {
                            const visible=el=>{
                                const r=el.getBoundingClientRect(),s=getComputedStyle(el);
                                return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';
                            };
                            const titles=[...document.querySelectorAll('body *')].filter(el=>
                                visible(el)&&(el.innerText||'').trim()==='AI素材工具');
                            if(!titles.length) return {found:false,count:0};
                            let roots=[];
                            for(const title of titles){
                                for(let p=title.parentElement,depth=0;p&&depth<10;depth++,p=p.parentElement){
                                    const buttons=[...p.querySelectorAll('button,[role="button"]')].filter(btn=>
                                        visible(btn)&&(btn.innerText||'').trim()==='上传');
                                    if(buttons.length){ roots.push({p,buttons}); break; }
                                }
                            }
                            const buttons=[...new Set(roots.flatMap(x=>x.buttons))];
                            if(buttons.length!==1) return {found:true,count:buttons.length};
                            const button=buttons[0],r=button.getBoundingClientRect();
                            document.querySelectorAll('[data-wb-ai-editor-confirm]').forEach(
                                node=>node.removeAttribute('data-wb-ai-editor-confirm'));
                            button.setAttribute('data-wb-ai-editor-confirm','1');
                            return {found:true,count:1,cx:Math.round(r.left+r.width/2),
                                cy:Math.round(r.top+r.height/2),text:(button.innerText||'').trim(),
                                disabled:!!button.disabled};
                        }
                        """)
                        if editor_confirm.get("found"):
                            break
                        time.sleep(0.25)
                    if editor_confirm.get("found"):
                        if editor_confirm.get("count") != 1:
                            raise Exception("AI素材工具上传确认按钮不唯一: %s" % editor_confirm)
                        if editor_confirm.get("disabled"):
                            raise Exception("AI素材工具上传确认按钮仍为禁用状态")
                        log("UPLOAD", "检测到 AI素材工具裁剪确认页，点击唯一‘上传’按钮")
                        confirm_button = page.locator('[data-wb-ai-editor-confirm="1"]')
                        if confirm_button.count() != 1:
                            raise Exception("AI素材工具上传确认按钮标记丢失或重复")
                        confirm_button.click(timeout=8000)
                        try:
                            page.get_by_text("AI素材工具", exact=True).wait_for(
                                state="hidden", timeout=15000)
                        except Exception as confirm_wait_exc:
                            raise Exception("AI素材工具点击上传后未关闭: %s" % confirm_wait_exc)
                        time.sleep(1.0)
                    used_pre_injection_chooser = True
                    log("UPLOAD", "已通过唯一语义触发器的真实文件选择器注入主图")
                except Exception as pre_chooser_exc:
                    log("UPLOAD", "主图真实文件选择器未成功，回退同一语义 input 直接注入: %s" % pre_chooser_exc)

            if not used_pre_injection_chooser:
                semantic_main.set_input_files(main_path)
            input_file_probe = page.evaluate("""
                () => {
                    const input=document.querySelector(
                        'input[type="file"][data-wb-main-upload-target="1"]');
                    return input ? {
                        files: input.files ? input.files.length : -1,
                        accept: input.getAttribute('accept') || '',
                        disabled: !!input.disabled,
                        connected: !!input.isConnected
                    } : {files:-1, accept:'', disabled:false, connected:false};
                }
            """)
            log("UPLOAD", "主图已注入(method=%s)，input文件探针: %s" %
                ("semantic-filechooser" if used_pre_injection_chooser else "semantic-input", input_file_probe))

            if (not used_pre_injection_chooser and
                    needs_filechooser_fallback(input_file_probe.get("files", -1))):
                # The current uploader may immediately reset programmatically
                # assigned FileList values.  Click the visible trigger belonging
                # to this exact semantic input and use the browser's trusted file
                # chooser.  The trigger is resolved from the unique marked input;
                # no global "first upload button" or coordinate fallback exists.
                post_injection_trigger = page.evaluate(r"""
                    () => {
                        const input=document.querySelector(
                            'input[type="file"][data-wb-main-upload-target="1"]');
                        if(!input) return null;
                        const visible=el=>{
                            const r=el.getBoundingClientRect(),s=getComputedStyle(el);
                            return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';
                        };
                        let candidates=[];
                        const label=input.closest('label');
                        if(label&&visible(label)) candidates.push(label);
                        if(input.id){
                            const escaped=(window.CSS&&CSS.escape)?CSS.escape(input.id):input.id;
                            const external=document.querySelector('label[for="'+escaped+'"]');
                            if(external&&visible(external)) candidates.push(external);
                        }
                        const debug=[];
                        for(let p=input.parentElement,depth=0;p&&depth<8;depth++,p=p.parentElement){
                            const ownText=(p.innerText||'').trim();
                            const fileCount=p.querySelectorAll('input[type="file"]').length;
                            const rect=p.getBoundingClientRect();
                            debug.push({depth,tag:p.tagName,className:String(p.className||'').slice(0,100),
                                text:ownText.slice(0,100),fileCount,
                                width:Math.round(rect.width),height:Math.round(rect.height)});
                            // Some upload controls render their text directly on
                            // the one-input wrapper (not on a child span/label).
                            // It remains safe only while this wrapper owns exactly
                            // the one semantic input resolved above.
                            if(fileCount===1&&visible(p)&&ownText.includes('上传主图')) candidates.push(p);
                            for(const el of p.querySelectorAll('button,[role="button"],label,span,div')){
                                const text=(el.innerText||'').trim();
                                const ownsOne=el.querySelectorAll('input[type="file"]').length<=1;
                                if(visible(el)&&ownsOne&&(text==='上传主图'||text.includes('上传主图'))){
                                    candidates.push(el);
                                }
                            }
                            if(candidates.length) break;
                        }
                        if(!candidates.length) return {found:false,debug,
                            input:{id:input.id||'',className:String(input.className||'').slice(0,120)}};
                        candidates=[...new Set(candidates)];
                        candidates.sort((a,b)=>{
                            const ar=a.getBoundingClientRect(),br=b.getBoundingClientRect();
                            return ar.width*ar.height-br.width*br.height;
                        });
                        const el=candidates[0],r=el.getBoundingClientRect();
                        return {found:true,cx:Math.round(r.left+r.width/2),cy:Math.round(r.top+r.height/2),
                                text:(el.innerText||'').trim().slice(0,80),tag:el.tagName,debug};
                    }
                """)
                trigger_point = pre_injection_trigger or post_injection_trigger
                if not trigger_point or not trigger_point.get("found"):
                    log("UPLOAD", "❌ 主图 input 被平台清空，且无法从该语义 input 解析可见上传触发器: %s" % trigger_point)
                    return False
                try:
                    log("UPLOAD", "主图 input 被平台清空，改用同一语义槽位的真实文件选择器: %s" % trigger_point)
                    with page.expect_file_chooser(timeout=8000) as chooser_info:
                        page.mouse.click(trigger_point["cx"], trigger_point["cy"])
                    chooser_info.value.set_files(main_path)
                    time.sleep(0.5)
                    log("UPLOAD", "主图真实文件选择器已注入文件")
                except Exception as chooser_exc:
                    log("UPLOAD", "❌ 主图真实文件选择器失败: %s" % chooser_exc)
                    return False

            # 触发 change 事件
            page.evaluate("""
                () => {
                    let inputs = document.querySelectorAll('input[type="file"]');
                    for (let inp of inputs) {
                        if (inp.files && inp.files.length > 0) {
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                    }
                }
            """)
            # 图片素材服务偶尔需要数秒完成上传、审核并生成 CDN 预览。
            # 固定等待 3 秒会把“仍在处理”误判成“没有落图”。最多轮询 12 秒；
            # 通过条件仍然是唯一语义字段内出现本次新增预览，绝不使用全局总数兜底。
            causal = {}
            causal_wait_started = time.time()
            while time.time() - causal_wait_started < 12:
                time.sleep(1)
                causal = page.evaluate(r"""
                () => {
                    const ev=window.__wbMainUploadEvidence;
                    if(!ev) return {deltaCount:0,bound:false,reason:'缺少上传前快照'};
                    const candidates=[];
                    for(const w of document.querySelectorAll('.index-module_imgWrapper__xOFF7')){
                        const srcs=[...w.querySelectorAll('img')].map(img=>{
                            const src=img.currentSrc||img.getAttribute('src')||'';
                            const r=img.getBoundingClientRect();
                            return src&&!src.startsWith('data:image/svg')&&r.width>=30&&r.height>=30 ? src : '';
                        }).filter(Boolean);
                        const added=srcs.filter(src=>!ev.before.has(src));
                        if(added.length) candidates.push({w,src:added[0],added:added.length});
                    }
                    ev.deltaCount=candidates.length;
                    const field=(ev.field&&ev.field.isConnected)?ev.field:
                        document.querySelector('[data-wb-main-image-field="1"]');
                    // 只接受本次主图 input 操作后、在完整商品正面图字段内出现的
                    // 新预览。页面其他区域的联动预览不会影响这一判定。
                    const fieldCandidates=field?candidates.filter(c=>field.contains(c.w)):[];
                    const fieldWrappers=field?[...field.querySelectorAll(
                        '.index-module_imgWrapper__xOFF7')]:[];
                    const previewFieldIndex=fieldCandidates.length?
                        fieldWrappers.indexOf(fieldCandidates[0].w):-1;
                    const relation=(wrapper=>{
                        const inp=ev.input;
                        if(!inp||!wrapper) return {reason:'input或wrapper不存在'};
                        const chain=node=>{const a=[];for(let p=node;p;p=p.parentElement)a.push(p);return a;};
                        const ic=chain(inp), wc=chain(wrapper), lca=ic.find(n=>wc.includes(n));
                        if(!lca) return {reason:'没有共同祖先'};
                        const branch=(node,ancestor)=>{
                            let p=node,last=node,depth=0;
                            while(p&&p!==ancestor){last=p;p=p.parentElement;depth++;}
                            return {depth,index:[...ancestor.children].indexOf(last),
                                tag:last.tagName||'',className:String(last.className||'').slice(0,100)};
                        };
                        return {reason:'ok',lcaTag:lca.tagName,lcaClass:String(lca.className||'').slice(0,140),
                            lcaText:(lca.innerText||'').trim().slice(0,100),
                            inputBranch:branch(inp,lca),previewBranch:branch(wrapper,lca),
                            fileCount:lca.querySelectorAll('input[type="file"]').length,
                            wrapperCount:lca.querySelectorAll('.index-module_imgWrapper__xOFF7').length};
                    })(fieldCandidates.length?fieldCandidates[0].w:(candidates.length?candidates[0].w:null));
                    if(fieldCandidates.length){
                        const picked=fieldCandidates[0];
                        ev.wrapper=picked.w; ev.src=picked.src; ev.ready=true;
                        ev.expectedSrcs=ev.src?[ev.src]:[]; ev.uploadDeltas=[candidates.length];
                        picked.w.setAttribute('data-wb-main-preview-bound','1');
                    }
                    ev.previewFieldIndex=previewFieldIndex;
                    ev.fieldDeltaCount=fieldCandidates.length;
                    ev.slotBound=(Number(ev.mainInputFieldIndex)===0&&
                        previewFieldIndex===Number(ev.mainInputFieldIndex)&&
                        fieldCandidates.length===1);
                    return {deltaCount:candidates.length,bound:ev.ready,
                            fieldDeltaCount:fieldCandidates.length,
                            mainInputFieldIndex:Number(ev.mainInputFieldIndex),
                            previewFieldIndex:previewFieldIndex,
                            slotBound:!!ev.slotBound,
                            fieldInputCount:field?[...field.querySelectorAll(
                                'input[type="file"]')].length:0,
                            fieldWrapperCount:fieldWrappers.length,
                            relation:relation,
                            src:ev.src ? ev.src.slice(0,120) : '',
                            reason:ev.ready?'商品正面图字段产生有效预览':'商品正面图字段没有新增有效预览'};
                }
                """)
                if causal.get("bound"):
                    break
            causal["waitSeconds"] = round(time.time() - causal_wait_started, 1)
            log("UPLOAD", "主图因果绑定: globalDelta=%s fieldDelta=%s bound=%s reason=%s src=%s" %
                (causal.get("deltaCount"), causal.get("fieldDeltaCount"), causal.get("bound"),
                 causal.get("reason"), causal.get("src")))
            log("UPLOAD", "主图input/预览DOM归属探针: %s" % causal.get("relation"))
            log("UPLOAD", "主图槽位序号探针: inputIndex=%s previewIndex=%s fieldInputs=%s fieldWrappers=%s" %
                (causal.get("mainInputFieldIndex"), causal.get("previewFieldIndex"),
                 causal.get("fieldInputCount"), causal.get("fieldWrapperCount")))
            if not causal.get("bound"):
                generic_probe = page.evaluate(r"""
                    () => {
                        const ev=window.__wbMainUploadEvidence||{};
                        const before=ev.beforeAll||new Set();
                        const validImage=img=>{
                            const src=img.currentSrc||img.getAttribute('src')||'';
                            const r=img.getBoundingClientRect(),st=getComputedStyle(img);
                            return src&&!src.startsWith('data:image/svg')&&r.width>=20&&r.height>=20&&
                                st.display!=='none'&&st.visibility!=='hidden' ?
                                {rawSrc:src,src:src.slice(0,160),width:Math.round(r.width),height:Math.round(r.height),
                                 x:Math.round(r.left),y:Math.round(r.top),className:String(img.className||'').slice(0,120)}:null;
                        };
                        const all=[...document.querySelectorAll('img')].map(validImage).filter(Boolean);
                        const added=all.filter(x=>!before.has(x.rawSrc)).map(x=>{
                            const copy={...x}; delete copy.rawSrc; return copy;
                        });
                        const inputs=[...document.querySelectorAll('input[type="file"]')];
                        const semantic=inputs.filter(inp=>{
                            let p=inp.closest('label')||inp.parentElement,text='';
                            for(let i=0;i<10&&p;i++,p=p.parentElement){
                                const t=(p.innerText||'').trim(); if(!text&&t) text=t.slice(0,160);
                            }
                            return text.includes('商品正面图')&&text.includes('上传主图');
                        });
                        const ancestors=[];
                        if(semantic.length===1){
                            for(let p=semantic[0].parentElement,depth=0;p&&depth<8;depth++,p=p.parentElement){
                                const r=p.getBoundingClientRect();
                                const imgs=[...p.querySelectorAll('img')].map(validImage).filter(Boolean);
                                let backgrounds=0;
                                for(const el of p.querySelectorAll('*')){
                                    const er=el.getBoundingClientRect(),bg=getComputedStyle(el).backgroundImage||'';
                                    if(bg.includes('url(')&&er.width>=20&&er.height>=20) backgrounds++;
                                }
                                ancestors.push({depth,tag:p.tagName,className:String(p.className||'').slice(0,140),
                                    text:(p.innerText||'').trim().slice(0,120),
                                    files:p.querySelectorAll('input[type="file"]').length,
                                    images:imgs.length,backgrounds,width:Math.round(r.width),height:Math.round(r.height)});
                            }
                        }
                        return {semanticCount:semantic.length,visibleImageCount:all.length,
                            genericAdded:added.slice(0,20),ancestors};
                    }
                """)
                log("UPLOAD", "主图失败通用DOM探针（仅诊断，不参与成功判定）: %s" % generic_probe)
                upload_feedback = page.evaluate(r"""
                    () => {
                        const wanted=/失败|违规|不符合|重试|上传|图片|网络|尺寸|审核/i;
                        return [...document.querySelectorAll(
                            '[role="alert"],.ecom-g-message,.ant-message,[class*="toast"],[class*="message"]'
                        )].filter(el=>{
                            const r=el.getBoundingClientRect(),st=getComputedStyle(el);
                            return r.width>0&&r.height>0&&st.display!=='none'&&st.visibility!=='hidden';
                        }).map(el=>(el.innerText||'').trim()).filter(t=>t&&wanted.test(t)).slice(0,10);
                    }
                """)
                log("UPLOAD", "主图上传失败页面反馈（仅诊断，不参与成功判定）: %s" % upload_feedback)
                try:
                    failure_path = os.path.join(
                        WORK_DIR, "panel_data", "upload_failure_%s.png" %
                        str(os.environ.get("DP_TASK_ID") or "task"))
                    page.screenshot(path=failure_path, full_page=False)
                    log("UPLOAD", "主图失败截图: %s" % failure_path)
                except Exception as screenshot_exc:
                    log("UPLOAD", "主图失败截图未生成: %s" % screenshot_exc)
                log("UPLOAD", "❌ 商品正面图语义槽位未产生有效预览，拒绝继续上传辅助图")
                return False
            log("UPLOAD", "主图上传完成，等待 3s")

        except Exception as e:
            log("UPLOAD", f"主图上传异常: {e}")
            return False
    else:
        log("UPLOAD", "未找到主图目标位置!")
        return False

    # ===== 上传白底辅助图 =====
    if not valid_aux_paths:
        log("UPLOAD", "无白底图，跳过辅助图上传")
        log("UPLOAD", "===== 图片上传完成 (仅主图) =====")
        return True

    if not aux_targets:
        log("UPLOAD", "无唯一语义辅助图目标位置")
        log("UPLOAD", f"有 {len(valid_aux_paths)} 张白底图待上传但位置不足，跳过")
        log("UPLOAD", "===== 图片上传完成 (仅主图) =====")
        return True

    try:
        upload_count = min(len(valid_aux_paths), len(aux_targets))
        uploaded_aux_count = 0
        log("UPLOAD", f"--- 上传白底图 ({len(valid_aux_paths)} 张图 → {upload_count} 个位置) ---")
        for i in range(upload_count):
            aux_path = valid_aux_paths[i]
            try:
                live = page.evaluate(r"""
                    () => {
                        document.querySelectorAll('[data-wb-current-aux-target]').forEach(
                            e=>e.removeAttribute('data-wb-current-aux-target'));
                        const root=document.querySelector('[data-wb-aux-upload-group="1"]');
                        if(!root) return {count:0,reason:'辅助图语义组因重绘丢失'};
                        const rows=[...root.querySelectorAll('input[type="file"]')].filter(inp=>{
                            let p=inp.closest('label')||inp.parentElement,text='';
                            for(let d=0;d<8&&p;d++,p=p.parentElement){
                                const t=(p.innerText||'').trim(); if(t){text=t.slice(0,120);break;}
                            }
                            return text.includes('上传辅助图')&&!inp.disabled;
                        });
                        if(!rows.length) return {count:0,reason:'没有剩余辅助图空槽位'};
                        rows[0].setAttribute('data-wb-current-aux-target','1');
                        return {count:rows.length,reason:'ok'};
                    }
                """)
                target_input = page.locator('input[type="file"][data-wb-current-aux-target="1"]')
                if target_input.count() != 1:
                    raise RuntimeError("辅助图实时语义目标丢失或重复: %s" % live)
                before_srcs = page.evaluate(r"""() => {
                    const read=scope=>[...new Set([...scope.querySelectorAll(
                        '.index-module_imgWrapper__xOFF7 img')]
                        .map(i=>i.currentSrc||i.src||'').filter(Boolean))];
                    const group=document.querySelector('[data-wb-aux-upload-group="1"]');
                    return {global:read(document),group:group?read(group):[]};
                }""")
                target_input.set_input_files(aux_path)
                time.sleep(1.5)
                delta = page.evaluate(r"""
                    (before) => {
                        const read=scope=>[...new Set([...scope.querySelectorAll(
                            '.index-module_imgWrapper__xOFF7 img')]
                            .map(i=>i.currentSrc||i.src||'').filter(Boolean))];
                        const group=document.querySelector('[data-wb-aux-upload-group="1"]');
                        const globalAdded=read(document).filter(src=>!before.global.includes(src));
                        const groupAdded=group?read(group).filter(src=>!before.group.includes(src)):[];
                        const ev=window.__wbMainUploadEvidence;
                        if(ev){
                            ev.uploadDeltas.push(groupAdded.length);
                            if(groupAdded.length===1) ev.expectedSrcs.push(groupAdded[0]);
                        }
                        return {count:groupAdded.length,globalCount:globalAdded.length,
                            src:groupAdded.length===1?groupAdded[0].slice(0,120):''};
                    }
                """, before_srcs)
                if not semantic_aux_preview_present(delta.get("count", 0)):
                    raise RuntimeError("辅助图语义组未产生唯一新增预览: %s" % delta)
                uploaded_aux_count += 1
                log("UPLOAD", f"白底图 {i+1}/{upload_count}: {os.path.basename(aux_path)} → 当前第一个空语义槽位，组内新增=1，全局新增={delta.get('globalCount')}")
            except Exception as e:
                log("UPLOAD", f"白底图 {i+1} 注入失败: {e}")

        # 触发所有 change 事件
        page.evaluate("""
            () => {
                let inputs = document.querySelectorAll('input[type="file"]');
                for (let inp of inputs) {
                    if (inp.files && inp.files.length > 0) {
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                }
            }
        """)

        time.sleep(5)
        log("UPLOAD", "白底图上传完成，等待 5s")

        if upload_count < len(valid_aux_paths):
            log("UPLOAD", f"白底图有 {len(valid_aux_paths) - upload_count} 张因位置不足未上传")

    except Exception as e:
        log("UPLOAD", f"白底图上传异常: {e}")
        return False

    log("UPLOAD", "===== 图片上传流程完成 =====")
    log("UPLOAD", f"主图: 1 张 | 白底辅助图: {uploaded_aux_count} 张 | 3:4 区域: 留空")
    return True


def _expected_main_image_total(parsed: dict) -> tuple:
    """返回主图区应落图数量及有效辅助图数量，供页面校验与压力测试共用。"""
    valid_aux_count = len([
        path for path in get_aux_image_paths(parsed)
        if os.path.isfile(path) and _is_valid_main_area_image(path)
    ])
    return max(4, 1 + min(4, valid_aux_count)), valid_aux_count


def retry_missing_aux_images(page, parsed: dict) -> bool:
    """只补传本次会话缺失的辅助图；绝不重跑主图和整套上传流程。"""
    valid_aux_paths = [
        path for path in get_aux_image_paths(parsed)
        if os.path.isfile(path) and _is_valid_main_area_image(path)
    ]
    evidence = page.evaluate("""() => {
        const ev=window.__wbMainUploadEvidence||{};
        return {confirmed: (ev.expectedSrcs||[]).length, deltas: ev.uploadDeltas||[]};
    }""")
    confirmed = evidence.get("confirmed", 0)
    missing = missing_aux_indices(len(valid_aux_paths), evidence.get("deltas", []))
    if not missing:
        log("UPLOAD_RETRY", "本次会话没有缺失的辅助图，无需补传")
        return True

    log("UPLOAD_RETRY", "仅补传缺失辅助图: %s（已确认会话图片=%s，原始上传delta=%s）" %
        ([i + 1 for i in missing], confirmed, evidence.get("deltas", [])))
    for aux_index in missing:
        aux_path = valid_aux_paths[aux_index]
        live = page.evaluate(r"""
            () => {
                document.querySelectorAll('[data-wb-current-aux-target]').forEach(
                    e=>e.removeAttribute('data-wb-current-aux-target'));
                const root=document.querySelector('[data-wb-aux-upload-group="1"]');
                if(!root) return {count:0,reason:'辅助图语义组因重绘丢失'};
                const rows=[...root.querySelectorAll('input[type="file"]')].filter(inp=>{
                    let p=inp.closest('label')||inp.parentElement,text='';
                    for(let d=0;d<8&&p;d++,p=p.parentElement){
                        const t=(p.innerText||'').trim(); if(t){text=t.slice(0,120);break;}
                    }
                    return text.includes('上传辅助图')&&!inp.disabled;
                });
                if(!rows.length) return {count:0,reason:'没有剩余辅助图空槽位'};
                rows[0].setAttribute('data-wb-current-aux-target','1');
                return {count:rows.length,reason:'ok'};
            }
        """)
        target = page.locator('input[type="file"][data-wb-current-aux-target="1"]')
        if target.count() != 1:
            log("UPLOAD_RETRY", "缺失辅助图无法定位当前第一个空槽位: %s" % live)
            return False
        before_srcs = page.evaluate(r"""() => {
            const read=scope=>[...new Set([...scope.querySelectorAll(
                '.index-module_imgWrapper__xOFF7 img')]
                .map(i=>i.currentSrc||i.src||'').filter(Boolean))];
            const group=document.querySelector('[data-wb-aux-upload-group="1"]');
            return {global:read(document),group:group?read(group):[]};
        }""")
        target.set_input_files(aux_path)
        time.sleep(1.5)
        delta = page.evaluate(r"""
            (before) => {
                const read=scope=>[...new Set([...scope.querySelectorAll(
                    '.index-module_imgWrapper__xOFF7 img')]
                    .map(i=>i.currentSrc||i.src||'').filter(Boolean))];
                const group=document.querySelector('[data-wb-aux-upload-group="1"]');
                const globalAdded=read(document).filter(src=>!before.global.includes(src));
                const groupAdded=group?read(group).filter(src=>!before.group.includes(src)):[];
                const ev=window.__wbMainUploadEvidence;
                if(ev){
                    ev.uploadDeltas.push(groupAdded.length);
                    if(groupAdded.length===1) ev.expectedSrcs.push(groupAdded[0]);
                }
                return {count:groupAdded.length,globalCount:globalAdded.length,
                    src:groupAdded.length===1?groupAdded[0].slice(0,120):''};
            }
        """, before_srcs)
        if not semantic_aux_preview_present(delta.get("count", 0)):
            log("UPLOAD_RETRY", "缺失辅助图语义组没有产生唯一新增预览: %s" % delta)
            return False
        log("UPLOAD_RETRY", "辅助图%s已补到当前第一个空槽位: %s" %
            (aux_index + 1, os.path.basename(aux_path)))
    time.sleep(3)
    return True


def verify_upload(page, parsed: dict) -> bool:
    """
    验证图片上传结果。
    检查: 必填主图槽位确实产生预览 + 页面图片总数。
    返回 True 如果验证通过。
    """
    log("VERIFY_UPLOAD", "===== 验证上传结果 =====")

    result = page.evaluate(r"""
        () => {
            let wrappers = document.querySelectorAll('.index-module_imgWrapper__xOFF7');
            let totalImages = 0;
            for (let w of wrappers) {
                if (w.querySelector('img[src]')) totalImages++;
            }

            // 抖店上传完成后会把预览 wrapper 与 file input 拆成兄弟节点，
            // 因而不能强制要求预览必须位于 input 的父容器内。主图区 wrapper
            // 按页面顺序排列，第一个就是 1:1 商品正面图；单独验证它，既不会
            // 被后续辅助图凑总数蒙混，也兼容上传后 React 拆分 DOM 的结构。
            let firstWrapper = wrappers.length ? wrappers[0] : null;
            let firstWrapperImageCount = 0;
            if (firstWrapper) {
                for (let img of firstWrapper.querySelectorAll('img')) {
                    let src = img.currentSrc || img.getAttribute('src') || '';
                    if (src && !src.startsWith('data:image/svg')) firstWrapperImageCount++;
                }
                if (!firstWrapperImageCount) {
                    for (let el of firstWrapper.querySelectorAll('*')) {
                        let bg = getComputedStyle(el).backgroundImage || '';
                        if (bg.includes('url(')) firstWrapperImageCount++;
                    }
                }
            }

            let inputs = document.querySelectorAll('input[type="file"]');
            let filesCount = 0;
            for (let inp of inputs) {
                if (inp.files && inp.files.length > 0) filesCount++;
            }

            // 精确验证必填“商品正面图”槽位，而不是把辅助图、3:4 图或旧残留图
            // 算成主图。优先使用 upload_images 打下的标记；标记因 React 重绘
            // 丢失时，再从带“商品正面图/上传主图”语义的 file input 中恢复定位。
            let mainInput = document.querySelector('input[type="file"][data-wb-main-upload-target="1"]');
            let semanticMainCandidates = Array.from(inputs).filter(inp => {
                    let label=inp.closest('label'), p=label||inp.parentElement;
                    let semanticText='', nearestVisible=null;
                    for(let i=0;i<10&&p;i++,p=p.parentElement){
                        let text=(p.innerText||'').trim(), r=p.getBoundingClientRect(), st=getComputedStyle(p);
                        if(!semanticText&&text) semanticText=text.slice(0,160);
                        if(!nearestVisible&&r.width>0&&r.height>0&&st.display!=='none'&&st.visibility!=='hidden') nearestVisible=p;
                    }
                    let context=''; p=nearestVisible;
                    for(let j=0;j<4&&p;j++,p=p.parentElement) context+=' '+(p.innerText||'');
                    // 只检查该 input 最近的自身语义，不能爬到包含整组控件的父容器后
                    // 把四个辅助图 input 也误认成“商品正面图”。
                    return semanticText.includes('商品正面图')&&semanticText.includes('上传主图')&&
                           !/3\s*[:：]\s*4/.test(context);
                });
            if (!mainInput && semanticMainCandidates.length === 1) {
                mainInput = semanticMainCandidates[0];
            }

            function previewInfo(scope) {
                let imageCount = 0;
                for (let img of scope.querySelectorAll('img')) {
                    let src = img.currentSrc || img.getAttribute('src') || '';
                    let r = img.getBoundingClientRect();
                    if (src && !src.startsWith('data:image/svg') && r.width >= 30 && r.height >= 30) imageCount++;
                }
                let backgroundCount = 0;
                for (let el of scope.querySelectorAll('*')) {
                    let bg = getComputedStyle(el).backgroundImage || '';
                    let r = el.getBoundingClientRect();
                    if (bg.includes('url(') && r.width >= 30 && r.height >= 30) backgroundCount++;
                }
                return { imageCount, backgroundCount };
            }

            let main = { found: false, present: false, imageCount: 0, backgroundCount: 0,
                         scopeText: '', scopeClass: '', reason: '未定位到必填主图输入框' };
            if (mainInput) {
                let p = mainInput.parentElement;
                let fallback = null;
                // 选择“只包含一个 file input”的最小语义容器，避免爬到包含整组
                // 主图+辅助图的父容器后再次发生总数误判。
                for (let depth = 0; depth < 8 && p; depth++, p = p.parentElement) {
                    let text = (p.innerText || '').trim();
                    let fileCount = p.querySelectorAll('input[type="file"]').length;
                    if (!fallback && fileCount === 1) fallback = p;
                    if (fileCount === 1 && (text.includes('商品正面图') || text.includes('上传主图'))) {
                        fallback = p;
                        break;
                    }
                }
                if (fallback) {
                    let pi = previewInfo(fallback);
                    main = {
                        found: true,
                        present: (pi.imageCount + pi.backgroundCount) > 0,
                        imageCount: pi.imageCount,
                        backgroundCount: pi.backgroundCount,
                        scopeText: (fallback.innerText || '').trim().slice(0, 100),
                        scopeClass: String(fallback.className || '').slice(0, 160),
                        reason: (pi.imageCount + pi.backgroundCount) > 0 ? '主图槽位存在有效预览' : '主图槽位没有有效预览'
                    };
                } else {
                    main.reason = '已定位主图输入框，但无法隔离其独立槽位';
                }
            }

            return {
                totalImages: totalImages,
                wrappersCount: wrappers.length,
                filesCount: filesCount,
                main: main,
                semanticMainCandidateCount: semanticMainCandidates.length,
                causalMain: (() => {
                    const ev=window.__wbMainUploadEvidence||{};
                    let wrapper=(ev.wrapper&&ev.wrapper.isConnected)?ev.wrapper:
                        document.querySelector('[data-wb-main-preview-bound="1"]');
                    // React 若重建 wrapper，可凭本次主图上传产生的唯一 URL 恢复绑定；
                    // URL 来自因果快照，不是页面位置或“第一张图”。
                    if(!wrapper&&ev.src){
                        const matches=[...document.querySelectorAll('.index-module_imgWrapper__xOFF7')]
                            .filter(w=>[...w.querySelectorAll('img')].some(
                                img=>(img.currentSrc||img.getAttribute('src')||'')===ev.src));
                        if(matches.length===1) wrapper=matches[0];
                    }
                    const pi=wrapper?previewInfo(wrapper):{imageCount:0,backgroundCount:0};
                    return {deltaCount:Number(ev.deltaCount||0), found:!!wrapper,
                            present:(pi.imageCount+pi.backgroundCount)>0,
                            slotBound:!!ev.slotBound,
                            mainInputFieldIndex:Number(ev.mainInputFieldIndex),
                            previewFieldIndex:Number(ev.previewFieldIndex),
                            fieldDeltaCount:Number(ev.fieldDeltaCount||0),
                            imageCount:pi.imageCount, backgroundCount:pi.backgroundCount,
                            src:ev.src||''};
                })(),
                sessionUploads: (() => {
                    const ev=window.__wbMainUploadEvidence||{};
                    const expected=[...new Set(ev.expectedSrcs||[])];
                    const current=new Set([...document.querySelectorAll('.index-module_imgWrapper__xOFF7 img')]
                        .map(i=>i.currentSrc||i.src||'').filter(Boolean));
                    return {expectedCount:expected.length,
                            presentCount:expected.filter(src=>current.has(src)).length,
                            deltas:ev.uploadDeltas||[]};
                })(),
                firstWrapper: {
                    found: !!firstWrapper,
                    present: firstWrapperImageCount > 0,
                    imageCount: firstWrapperImageCount
                }
            };
        }
    """)

    total = result.get("totalImages", 0)
    wrappers_count = result.get("wrappersCount", 0)
    files_count = result.get("filesCount", 0)
    main = result.get("main") or {}
    semantic_main_count = result.get("semanticMainCandidateCount", 0)
    first_wrapper = result.get("firstWrapper") or {}
    causal_main = result.get("causalMain") or {}
    session_uploads = result.get("sessionUploads") or {}

    log("VERIFY_UPLOAD", f"页面 wrapper 总数: {wrappers_count}")
    log("VERIFY_UPLOAD", f"页面图片总数: {total}")
    log("VERIFY_UPLOAD", f"已注入文件的 input: {files_count} 个")
    log("VERIFY_UPLOAD", "必填主图槽位: found=%s present=%s img=%s bg=%s reason=%s"
        % (main.get("found"), main.get("present"), main.get("imageCount", 0),
           main.get("backgroundCount", 0), main.get("reason", "")))
    log("VERIFY_UPLOAD", f"必填主图语义候选数: {semantic_main_count}")
    log("VERIFY_UPLOAD", "主图因果绑定: delta=%s found=%s present=%s img=%s bg=%s" %
        (causal_main.get("deltaCount"), causal_main.get("found"), causal_main.get("present"),
         causal_main.get("imageCount", 0), causal_main.get("backgroundCount", 0)))
    main_slot_bound = exact_main_slot_binding(
        causal_main.get("mainInputFieldIndex", -1),
        causal_main.get("previewFieldIndex", -1),
        causal_main.get("fieldDeltaCount", 0),
        causal_main.get("present"),
    ) and bool(causal_main.get("slotBound"))
    log("VERIFY_UPLOAD", "权威主图同槽绑定: inputIndex=%s previewIndex=%s fieldDelta=%s present=%s bound=%s" % (
        causal_main.get("mainInputFieldIndex"), causal_main.get("previewFieldIndex"),
        causal_main.get("fieldDeltaCount"), causal_main.get("present"), main_slot_bound))
    log("VERIFY_UPLOAD", "本次会话图片身份: expected=%s present=%s deltas=%s" % (
        session_uploads.get("expectedCount"), session_uploads.get("presentCount"),
        session_uploads.get("deltas")))
    log("VERIFY_UPLOAD", "1:1首个主图wrapper: found=%s present=%s imageCount=%s"
        % (first_wrapper.get("found"), first_wrapper.get("present"),
           first_wrapper.get("imageCount", 0)))

    # 运营确认：1主图+3白底图（共4张）即可；若本地有4张白底图，则仍要求5张。
    # 根据本地有效素材动态计算本商品应落图数量，避免 Play 10T 只有3张白底图时
    # 为凑统一的5张门槛而重复补传；荣耀100有4张白底图时 expected 仍为5。
    expected_total, valid_aux_count = _expected_main_image_total(parsed)
    log("VERIFY_UPLOAD", f"本商品动态落图门槛: {expected_total} 张（有效白底图 {valid_aux_count} 张）")

    # 单一权威判定：最终函数的签名不接受 causal/delta/wrapper/field 等宽松
    # 证据；它们只能打印诊断，物理上不能参与最终布尔值运算。
    initial_main_count = page.evaluate(
        "() => Number((window.__wbMainUploadEvidence||{}).initialCandidateCount||0)"
    )
    expected_session = 1 + min(4, valid_aux_count)
    main_ok = strict_main_present(initial_main_count, main_slot_bound, main_slot_bound)
    session_ok = session_uploads_present(
        session_uploads.get("expectedCount", 0),
        session_uploads.get("presentCount", 0), valid_aux_count
    )
    all_ok = authoritative_upload_verdict(
        initial_main_count, main_slot_bound, main_slot_bound,
        session_uploads.get("expectedCount", 0),
        session_uploads.get("presentCount", 0), valid_aux_count
    )
    if all_ok:
        visual = _run_visual_check(
            page,
            "商品正面图位置是否显示了一张手机图片缩略图？回答 yes/no。",
            "main-image",
        )
        if visual.get("available") and visual.get("verdict") == "no":
            all_ok = False
            log("VERIFY_UPLOAD", "视觉二次核验未通过: %s" % visual.get("reason", ""))
        elif visual.get("available"):
            log("VERIFY_UPLOAD", "视觉二次核验通过: %s" % visual.get("reason", ""))

    if all_ok:
        log("VERIFY_UPLOAD", "===== 上传验证通过 =====")
    else:
        log("VERIFY_UPLOAD", "===== 上传验证未通过 =====")
        if not main_ok:
            log("VERIFY_UPLOAD", f"  问题: 必填商品正面图未确认落图（{main.get('reason', '未知')}）")
        if not session_ok:
            log("VERIFY_UPLOAD", "  问题: 本次会话图片身份不完整，预期上传%s张，已确认%s/%s张" % (
                expected_session, session_uploads.get("presentCount", 0),
                session_uploads.get("expectedCount", 0)))

    return all_ok


def _run_visual_check(page, question: str, label: str) -> dict:
    """截图并运行可选视觉核验；未配置时返回 unknown，不改变 DOM 结论。"""
    path = ""
    try:
        fd, path = tempfile.mkstemp(prefix="doudian-%s-" % label, suffix=".png")
        os.close(fd)
        page.screenshot(path=path, full_page=False)
        from vision_checks import vision_check
        result = vision_check(path, question)
        return {"available": result.available, "verdict": result.verdict,
                "reason": result.reason}
    except Exception as exc:
        return {"available": False, "verdict": "unknown", "reason": str(exc)}
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _visual_click_save_draft(page) -> str:
    """DOM 找不到保存按钮时才启用的高置信度视觉坐标兜底。"""
    path = ""
    try:
        fd, path = tempfile.mkstemp(prefix="doudian-save-button-", suffix=".png")
        os.close(fd)
        page.screenshot(path=path, full_page=False)
        from vision_checks import vision_locate
        result = vision_locate(path, "页面上是否有‘保存草稿’按钮？若有返回按钮中心坐标。")
        viewport = page.viewport_size or {"width": 0, "height": 0}
        if (result.available and result.verdict == "yes" and result.confidence >= .80 and
                isinstance(result.x, int) and isinstance(result.y, int) and
                0 <= result.x < viewport.get("width", 0) and 0 <= result.y < viewport.get("height", 0)):
            page.mouse.click(result.x, result.y)
            return "clicked by vision: (%d,%d), confidence=%.2f" % (
                result.x, result.y, result.confidence)
        return "not found by vision: %s" % result.reason
    except Exception as exc:
        return "vision unavailable: %s" % exc
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def upload_detail_images(page, parsed: dict) -> bool:
    """
    上传商详图片（共14张）。
    流程：删除原有商详图 → 上传成色图 + 13张通用图 = 14张

    根据测试结果，最可靠的方式是：
      - 策略A（首选）: 点击"上传图片"按钮触发 filechooser 对话框，一次性上传所有文件
      - 策略B（备选）: 逐个上传到 0/20 槽位
      - 验证方式：检查 .styles_imgWrapper__dqiHn 数量（与删除逻辑一致）

    参数:
        page: Playwright Page 对象
        parsed: parse_title() 返回的解析结果，需包含 condition 字段

    返回:
        True 表示上传成功，False 表示失败
    """
    log("UPLOAD_DETAIL", "===== 开始上传商详图片 =====")

    # 1. 准备文件路径
    detail_paths = get_detail_image_paths(parsed)

    if not detail_paths:
        log("UPLOAD_DETAIL", "错误: 没有找到任何商详图片")
        return False

    total_files = len(detail_paths)
    log("UPLOAD_DETAIL", f"商详图片数量: {total_files} (预期{DETAIL_IMAGE_COUNT}张)")
    if total_files != DETAIL_IMAGE_COUNT:
        log("UPLOAD_DETAIL", f"错误: 商详图片不完整，实际{total_files}张，必须为{DETAIL_IMAGE_COUNT}张")
        return False
    for i, p in enumerate(detail_paths):
        log("UPLOAD_DETAIL", f"  [{i+1}] {os.path.basename(p)}")

    # 检查文件是否都存在
    for p in detail_paths:
        if not os.path.isfile(p):
            log("UPLOAD_DETAIL", f"错误: 图片文件不存在 - {p}")
            return False

    # 读取当前商详图数量
    initial_count = page.evaluate("""
        () => document.querySelectorAll('.styles_imgWrapper__dqiHn').length
    """)
    log("UPLOAD_DETAIL", f"当前商详图数量: {initial_count}")

    try:
        # ===== 策略A: 使用 filechooser 上传（首选，最可靠） =====
        log("UPLOAD_DETAIL", "策略A: 使用 filechooser 上传...")

        # 找到"上传图片"按钮（商详图区域的）
        # 从测试结果看，input[12] label='上传图片' 是商详图上传按钮
        # 使用 Playwright 原生 click 触发文件对话框
        upload_btn = page.locator('label:has-text("上传图片")').last
        btn_count = upload_btn.count()
        log("UPLOAD_DETAIL", f"找到 '上传图片' label 按钮: {btn_count} 个")

        if btn_count == 0:
            # 备选：直接找 span 包含"上传图片"文本
            upload_btn = page.locator('span:has-text("上传图片")').first
            btn_count = upload_btn.count()
            log("UPLOAD_DETAIL", f"备选 '上传图片' span: {btn_count} 个")

        if btn_count > 0:
            try:
                with page.expect_file_chooser(timeout=15000) as fc_info:
                    upload_btn.click()
                    log("UPLOAD_DETAIL", "已点击上传图片按钮，等待文件对话框...")
                    time.sleep(2)

                file_chooser = fc_info.value
                log("UPLOAD_DETAIL", f"文件对话框已打开，正在注入 {total_files} 张图片...")
                file_chooser.set_files(detail_paths)
                log("UPLOAD_DETAIL", f"✅ filechooser 已注入 {total_files} 张图片")
                # 批量图片会在抖店服务端异步处理。旧逻辑只等 10 秒，看到 0 张
                # 就立即进入策略B；随后策略A的图片又延迟到达，最终重复成 17/18 张。
                # 改为最多等待 45 秒，只要数量还在增长就继续等，避免两种策略并发。
                after_count = initial_count
                stable_rounds = 0
                last_count = -1
                for waited in range(2, 47, 2):
                    time.sleep(2)
                    after_count = page.evaluate("""
                        () => document.querySelectorAll('.styles_imgWrapper__dqiHn').length
                    """)
                    if after_count == DETAIL_IMAGE_COUNT:
                        break
                    if after_count == last_count:
                        stable_rounds += 1
                    else:
                        stable_rounds = 0
                        last_count = after_count
                    if waited % 10 == 0:
                        log("UPLOAD_DETAIL", f"策略A异步处理中: {after_count}/{DETAIL_IMAGE_COUNT}（已等{waited}秒）")
                log("UPLOAD_DETAIL", f"策略A 上传后商详图数量: {after_count} (初始: {initial_count})")

                if after_count == DETAIL_IMAGE_COUNT:
                    log("UPLOAD_DETAIL", f"✅ 策略A 成功，最终商详图严格确认为{DETAIL_IMAGE_COUNT}张")
                    return True
                else:
                    log("UPLOAD_DETAIL", f"策略A 最终数量不是{DETAIL_IMAGE_COUNT}张（实际{after_count}），尝试策略B...")

            except Exception as e_a:
                log("UPLOAD_DETAIL", f"策略A 异常: {e_a}")
                log("UPLOAD_DETAIL", "策略A 失败，尝试策略B...")
        else:
            log("UPLOAD_DETAIL", "未找到 '上传图片' 按钮，尝试策略B...")

        # ===== 策略B: 逐个上传到 0/20 槽位 =====
        log("UPLOAD_DETAIL", "策略B: 逐个上传到 0/20 槽位...")

        # 探测 file input 分布
        input_info = page.evaluate("""
            () => {
                let inputs = document.querySelectorAll('input[type="file"]');
                let result = [];
                for (let inp of inputs) {
                    let label = inp.closest('label');
                    let labelText = label ? label.innerText.trim() : '';
                    let parent = inp.parentElement;
                    let parentClass = parent ? parent.className : '';
                    result.push({
                        label: labelText.substring(0, 30),
                        index: result.length,
                        parentClass: parentClass.substring(0, 50)
                    });
                }
                return result;
            }
        """)

        log("UPLOAD_DETAIL", f"探测到 {len(input_info)} 个 file input:")
        for item in input_info:
            log("UPLOAD_DETAIL", f"  [{item['index']}] label='{item['label']}' parent={item['parentClass']}")

        # 找到 mainLabel 的 0/20 槽位
        detail_slots = [item for item in input_info
                        if '0/20' in item['label']
                        and 'mainLabel' in item['parentClass']]

        if not detail_slots:
            detail_slots = [item for item in input_info if '0/20' in item['label']]

        if not detail_slots:
            log("UPLOAD_DETAIL", "错误: 未找到 0/20 槽位")
            return False

        log("UPLOAD_DETAIL", f"找到 {len(detail_slots)} 个 0/20 槽位: {[s['index'] for s in detail_slots]}")

        all_inputs = page.locator('input[type="file"]')
        confirmed = 0

        # 逐个上传图片到 0/20 槽位，交替使用不同的槽位
        for i, path in enumerate(detail_paths):
            # 策略A可能已部分成功；每次上传前都用页面实际数量做幂等闸门。
            # 达到目标数量立即停止，绝不再把剩余文件重复注入。
            current_before = page.evaluate("""
                () => document.querySelectorAll('.styles_imgWrapper__dqiHn').length
            """)
            if current_before >= DETAIL_IMAGE_COUNT:
                log("UPLOAD_DETAIL", f"策略B幂等闸门：当前已有{current_before}张，停止追加")
                break
            fname = os.path.basename(path)
            slot = detail_slots[i % len(detail_slots)]
            log("UPLOAD_DETAIL", f"  上传第 {i+1}/{total_files} 张: {fname} → input[{slot['index']}]")

            try:
                all_inputs.nth(slot['index']).set_input_files(path)
                time.sleep(0.5)

                # 触发 change 事件
                page.evaluate("""
                    () => {
                        let inputs = document.querySelectorAll('input[type="file"]');
                        for (let inp of inputs) {
                            if (inp.files && inp.files.length > 0) {
                                inp.dispatchEvent(new Event('change', {bubbles: true}));
                                inp.dispatchEvent(new Event('input', {bubbles: true}));
                            }
                        }
                    }
                """)
                log("UPLOAD_DETAIL", f"    change 事件已触发")

                # 等待处理
                time.sleep(3)

                # 检查 imgWrapper 数量
                current = page.evaluate("""
                    () => document.querySelectorAll('.styles_imgWrapper__dqiHn').length
                """)
                if current > initial_count + confirmed:
                    confirmed = current - initial_count
                    log("UPLOAD_DETAIL", f"    上传成功! 已确认 {confirmed}/{total_files} 张")
                else:
                    log("UPLOAD_DETAIL", f"    等待处理中... (当前 {current} 张)")

            except Exception as e:
                log("UPLOAD_DETAIL", f"  第 {i+1} 张上传异常: {e}")

        # 最终验证
        time.sleep(5)
        final_count = page.evaluate("""
            () => document.querySelectorAll('.styles_imgWrapper__dqiHn').length
        """)
        log("UPLOAD_DETAIL", f"策略B 最终商详图数量: {final_count} (初始: {initial_count})")

        if final_count == DETAIL_IMAGE_COUNT:
            log("UPLOAD_DETAIL", f"✅ 策略B 成功，最终商详图严格确认为{DETAIL_IMAGE_COUNT}张")
            return True
        else:
            log("UPLOAD_DETAIL", f"❌ 策略B 失败，最终商详图为{final_count}张，必须为{DETAIL_IMAGE_COUNT}张")
            return False

    except Exception as e:
        log("UPLOAD_DETAIL", f"商详图片上传异常: {e}")
        import traceback
        traceback.print_exc()
        return False


# ========== 其他属性: 是否原装 ==========

def select_is_original(page) -> bool:
    """在'其他属性'区域中找到'是否原装'下拉框，选择'是'"""
    log("STEP4.6", "===== 选择'是否原装'为'是' =====")

    locate_result = page.evaluate("""
        () => {
            let selects = document.querySelectorAll('.ecom-g-select');
            for (let s of selects) {
                let parent = s.parentElement;
                for (let i = 0; i < 8; i++) {
                    if (!parent) break;
                    let text = parent.innerText || '';
                    if (text.includes('是否原装') && !text.includes('电池') && text.length < 200) {
                        let rect = s.getBoundingClientRect();
                        let selItem = s.querySelector('.ecom-g-select-selection-item');
                        let phItem = s.querySelector('.ecom-g-select-selection-placeholder');
                        let scrollContainer = s.closest('.overflowScroll') || s.closest('[class*="overflow"]');
                        if (scrollContainer) {
                            let containerRect = scrollContainer.getBoundingClientRect();
                            let offsetInContainer = rect.top - containerRect.top + scrollContainer.scrollTop;
                            let targetScroll = Math.max(0, offsetInContainer - 150);
                            scrollContainer.scrollTo({ top: targetScroll, behavior: 'smooth' });
                            return {
                                found: true,
                                containerText: text.substring(0, 60),
                                currentValue: selItem ? (selItem.title || selItem.innerText || '').trim() : '',
                                hasPlaceholder: !!phItem,
                                y: Math.round(rect.top),
                                containerScrollTop: scrollContainer.scrollTop,
                                targetScroll: targetScroll,
                                usedContainer: true
                            };
                        } else {
                            return {
                                found: true,
                                containerText: text.substring(0, 60),
                                currentValue: selItem ? (selItem.title || selItem.innerText || '').trim() : '',
                                hasPlaceholder: !!phItem,
                                y: Math.round(rect.top),
                                usedContainer: false
                            };
                        }
                    }
                    parent = parent.parentElement;
                }
            }
            return { found: false };
        }
    """)

    if not locate_result.get("found"):
        log("STEP4.6", "未找到'是否原装'下拉框，跳过")
        return False

    log("STEP4.6", f"定位成功: containerScroll={locate_result.get('containerScrollTop', '?')}, target={locate_result.get('targetScroll', '?')}, usedContainer={locate_result.get('usedContainer')}")
    time.sleep(1.5)

    vp_check = page.evaluate("""
        () => {
            let selects = document.querySelectorAll('.ecom-g-select');
            for (let s of selects) {
                let parent = s.parentElement;
                for (let i = 0; i < 8; i++) {
                    if (!parent) break;
                    let text = parent.innerText || '';
                    if (text.includes('是否原装') && !text.includes('电池') && text.length < 200) {
                        let rect = s.getBoundingClientRect();
                        let inVP = rect.top >= 0 && rect.bottom <= window.innerHeight;
                        if (!inVP) {
                            let scrollContainer = s.closest('.overflowScroll') || s.closest('[class*="overflow"]');
                            if (scrollContainer) {
                                let containerRect = scrollContainer.getBoundingClientRect();
                                let offsetInContainer = rect.top - containerRect.top + scrollContainer.scrollTop;
                                scrollContainer.scrollTo({ top: Math.max(0, offsetInContainer - 150) });
                            }
                            return { inViewport: false, y: Math.round(rect.top) };
                        }
                        return { inViewport: true, top: Math.round(rect.top), bottom: Math.round(rect.bottom) };
                    }
                    parent = parent.parentElement;
                }
            }
            return { inViewport: false };
        }
    """)
    log("STEP4.6", f"视口检查: {vp_check}")
    time.sleep(0.5)

    current_val = locate_result.get("currentValue", "")
    log("STEP4.6", f"'是否原装'下拉框: 当前值='{current_val}'")

    if current_val == "是":
        log("STEP4.6", "'是否原装'已经是'是'，无需操作")
        return True

    try:
        click_info = page.evaluate("""
            () => {
                let selects = document.querySelectorAll('.ecom-g-select');
                for (let s of selects) {
                    let parent = s.parentElement;
                    for (let i = 0; i < 8; i++) {
                        if (!parent) break;
                        let text = parent.innerText || '';
                        if (text.includes('是否原装') && !text.includes('电池') && text.length < 200) {
                            let rect = s.getBoundingClientRect();
                            return { x: Math.round(rect.x + rect.width / 2), y: Math.round(rect.y + rect.height / 2) };
                        }
                        parent = parent.parentElement;
                    }
                }
                return null;
            }
        """)

        if not click_info:
            log("STEP4.6", "无法获取'是否原装'下拉框坐标，跳过")
            return False

        page.mouse.click(click_info["x"], click_info["y"])
        log("STEP4.6", f"已点击下拉框坐标 ({click_info['x']}, {click_info['y']})")
        time.sleep(1)

        option_locator = page.locator('.ecom-g-select-dropdown:visible').locator('.ecom-g-select-item:has-text("是")').first
        if option_locator.count() == 0:
            option_locator = page.locator('[class*="select-dropdown"]:visible').locator('[class*="select-item"]:has-text("是")').first

        if option_locator.count() > 0:
            option_locator.click()
            log("STEP4.6", "已点击下拉选项'是'")
        else:
            js_click = page.evaluate("""
                () => {
                    let popups = document.querySelectorAll('body > div');
                    for (let popup of popups) {
                        let items = popup.querySelectorAll('[class*="select-item"], [class*="Select-item"]');
                        if (items.length > 0) {
                            for (let item of items) {
                                let text = (item.innerText || '').trim();
                                if (text === '是') {
                                    item.click();
                                    return 'clicked via popup scan';
                                }
                            }
                        }
                    }
                    let allEls = document.querySelectorAll('*');
                    for (let el of allEls) {
                        let text = (el.innerText || '').trim();
                        let rect = el.getBoundingClientRect();
                        if (text === '是' && rect.width > 0 && rect.height > 0 && rect.top >= 0) {
                            let cls = (el.className || '').toString();
                            if (cls.includes('item') || cls.includes('option') || cls.includes('Item') || cls.includes('Option')) {
                                el.click();
                                return 'clicked via fallback scan: ' + cls.substring(0, 40);
                            }
                        }
                    }
                    return 'option not found';
                }
            """)
            log("STEP4.6", f"JS备选点击结果: {js_click}")

        time.sleep(1)

        verify = page.evaluate("""
            () => {
                let selects = document.querySelectorAll('.ecom-g-select');
                for (let s of selects) {
                    let parent = s.parentElement;
                    for (let i = 0; i < 8; i++) {
                        if (!parent) break;
                        let text = parent.innerText || '';
                        if (text.includes('是否原装') && !text.includes('电池') && text.length < 200) {
                            let selItem = s.querySelector('.ecom-g-select-selection-item');
                            return {
                                value: selItem ? (selItem.title || selItem.innerText || '').trim() : '',
                                hasPlaceholder: !!s.querySelector('.ecom-g-select-selection-placeholder')
                            };
                        }
                        parent = parent.parentElement;
                    }
                }
                return { value: '', hasPlaceholder: true };
            }
        """)

        final_val = verify.get("value", "")
        log("STEP4.6", f"验证结果: '是否原装' = '{final_val}'")

        if final_val == "是":
            log("STEP4.6", "===== '是否原装'已成功选择'是' =====")
            return True
        else:
            log("STEP4.6", f"警告: '是否原装'值为'{final_val}'，选择可能未生效")
            return False

    except Exception as e:
        log("STEP4.6", f"选择'是否原装'异常: {e}")
        return False


# ========== 价格库存 ==========

def navigate_to_price_stock(page) -> bool:
    """切换到'价格库存'标签页"""
    log("STEP8", "切换到价格库存标签页...")

    result = page.evaluate("""
        () => {
            let tabs = document.querySelectorAll('[role=tab]');
            for (let t of tabs) {
                if (t.innerText.includes('价格库存')) {
                    t.click();
                    return 'clicked: ' + t.innerText.trim();
                }
            }
            return 'not found';
        }
    """)

    log("STEP8", f"切换结果: {result}")
    time.sleep(2)
    return "clicked" in result


def _find_spec_switch_rect_and_state(page) -> dict:
    """用“最小容器”策略定位“修改规格信息”开关。

    旧实现靠 ancestor-walk（向上 6~8 层 parentElement），很容易走到 body 等共同祖先，
    把页面其它 switch（如顶部“消费者预览效果”开关）误当成规格开关，导致误判“已关”。
    本实现改为：先找同时包含“暂不想更新规格信息”和“修改规格信息”的最小可见容器，
    再在该容器内取第一个 switch / .ecom-g-switch；找不到再退化到 3 层 ancestor-walk。
    """
    try:
        return page.evaluate(r"""
            () => {
                // 策略1：最小容器匹配（首选）
                let all = document.querySelectorAll('*');
                let candidates = [];
                for (let el of all) {
                    let t = (el.innerText || '').replace(/\s+/g, ' ');
                    if (t.includes('暂不想更新规格信息') && t.includes('修改规格信息')) {
                        let r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                            candidates.push({el: el, area: r.width * r.height, text: t});
                        }
                    }
                }
                candidates.sort((a, b) => a.area - b.area);
                for (let c of candidates) {
                    let sw = c.el.querySelector('[role="switch"], .ecom-g-switch');
                    if (sw) {
                        let r = sw.getBoundingClientRect();
                        return {
                            found: true,
                            x: r.x, y: r.y, w: r.width, h: r.height,
                            state: sw.getAttribute('aria-checked') === 'true' ? '开' :
                                   sw.getAttribute('aria-checked') === 'false' ? '关' : 'unknown',
                            aria: sw.getAttribute('aria-checked'),
                            className: sw.className || '',
                            method: 'smallest-container',
                            containerText: c.text.substring(0, 120)
                        };
                    }
                }
                // 策略2：限制 3 层的 ancestor-walk（退化）
                let switches = document.querySelectorAll('[role="switch"], .ecom-g-switch');
                for (let sw of switches) {
                    let a = sw;
                    for (let i = 0; i < 3; i++) {
                        a = a.parentElement;
                        if (!a) break;
                        let t = (a.innerText || '').replace(/\s+/g, ' ');
                        if (t.includes('暂不想更新规格信息') && t.includes('修改规格信息')) {
                            let r = sw.getBoundingClientRect();
                            return {
                                found: true,
                                x: r.x, y: r.y, w: r.width, h: r.height,
                                state: sw.getAttribute('aria-checked') === 'true' ? '开' :
                                       sw.getAttribute('aria-checked') === 'false' ? '关' : 'unknown',
                                aria: sw.getAttribute('aria-checked'),
                                className: sw.className || '',
                                method: 'ancestor-walk',
                                containerText: t.substring(0, 120)
                            };
                        }
                    }
                }
                return { found: false, method: 'none', state: 'unknown' };
            }
        """)
    except Exception as e:
        return {"found": False, "method": "error", "state": "unknown", "error": str(e)[:120]}


def close_spec_switch(page) -> bool:
    """关闭'修改规格信息'开关"""
    log("STEP9", "===== 关闭修改规格信息开关 =====")

    info = _find_spec_switch_rect_and_state(page)
    if not info.get("found"):
        log("STEP9", f"未找到规格开关元素: {info.get('method')}, error={info.get('error')}")
        return False

    log("STEP9", f"规格开关定位: method={info.get('method')}, state={info.get('state')}, aria={info.get('aria')}, class={info.get('className')}, rect=({info.get('x'):.0f},{info.get('y'):.0f},{info.get('w'):.0f},{info.get('h'):.0f}), ctx={info.get('containerText', '')[:60]}")

    if info.get("state") == "关":
        log("STEP9", "规格开关已经是关闭状态，无需操作")
        return True
    if info.get("state") != "开":
        log("STEP9", "规格开关没有明确的 aria-checked 状态，拒绝盲点")
        return False

    log("STEP9", "规格开关当前为'开'，准备点击关闭...")

    # 滚动并重新取最新 rect，再点击中心
    try:
        clicked = False
        fresh_rect = page.evaluate(r"""
            () => {
                let all = document.querySelectorAll('*');
                let candidates = [];
                for (let el of all) {
                    let t = (el.innerText || '').replace(/\s+/g, ' ');
                    if (t.includes('暂不想更新规格信息') && t.includes('修改规格信息')) {
                        let r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) candidates.push({el: el, area: r.width * r.height});
                    }
                }
                candidates.sort((a, b) => a.area - b.area);
                for (let c of candidates) {
                    let sw = c.el.querySelector('[role="switch"], .ecom-g-switch');
                    if (sw) {
                        sw.scrollIntoView({ block: 'center' });
                        let r = sw.getBoundingClientRect();
                        return { found: true, x: r.x, y: r.y, w: r.width, h: r.height };
                    }
                }
                return { found: false };
            }
        """)
        if fresh_rect and fresh_rect.get("found") and fresh_rect.get("w", 0) > 0:
            cx = fresh_rect["x"] + fresh_rect["w"] / 2
            cy = fresh_rect["y"] + fresh_rect["h"] / 2
            time.sleep(0.3)
            page.mouse.click(cx, cy)
            clicked = True
            log("STEP9", f"通过规格开关中心坐标 ({cx:.0f}, {cy:.0f}) 点击")
        else:
            log("STEP9", "重新定位规格开关 rect 失败，尝试 locator 兜底")
    except Exception as e:
        log("STEP9", f"坐标点击异常: {e}")

    if not clicked:
        try:
            container = page.locator('text="修改规格信息"').locator('xpath=ancestor::*[contains(text(),"暂不想更新规格信息")]').first
            if container.count() > 0:
                switch_btn = container.locator('.ecom-g-switch').first
                switch_btn.scroll_into_view_if_needed()
                time.sleep(0.3)
                switch_btn.click()
                clicked = True
                log("STEP9", "通过 locator 定位 .ecom-g-switch 点击")
            else:
                log("STEP9", "未找到包含'暂不想更新规格信息'的容器")
        except Exception as e:
            log("STEP9", f"locator 点击失败: {e}")

    if not clicked:
        log("STEP9", "所有点击方法均失败")
        return False

    time.sleep(1.5)

    verify = _find_spec_switch_rect_and_state(page)
    log("STEP9", f"关闭后验证: state={verify.get('state')}, method={verify.get('method')}, aria={verify.get('aria')}, class={verify.get('className')}")

    if verify.get("state") == "关":
        log("STEP9", "===== 规格开关已成功关闭 =====")
        return True
    elif verify.get("state") == "开":
        log("STEP9", "首次点击后开关仍为'开'，执行 DOM 精确点击兜底")
        # 页面滚动后坐标偶发偏移。此处重新按语义容器找到真正 switch，
        # 直接调用 HTMLElement.click；每次点击后都重新探测，最多两次。
        for retry in range(1, 3):
            try:
                dom_clicked = page.evaluate(r"""
                    () => {
                        let all = Array.from(document.querySelectorAll('*'));
                        let candidates = [];
                        for (let el of all) {
                            let text = (el.innerText || '').replace(/\s+/g, ' ');
                            if (!text.includes('暂不想更新规格信息') || !text.includes('修改规格信息')) continue;
                            let sw = el.querySelector('[role="switch"], .ecom-g-switch');
                            if (!sw) continue;
                            let r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) candidates.push({el, sw, area: r.width * r.height});
                        }
                        candidates.sort((a, b) => a.area - b.area);
                        if (!candidates.length) return false;
                        candidates[0].sw.click();
                        return true;
                    }
                """)
                log("STEP9", f"DOM 兜底第{retry}次点击结果: {dom_clicked}")
            except Exception as e:
                log("STEP9", f"DOM 兜底第{retry}次点击异常: {e}")
            time.sleep(1.5)
            retry_verify = _find_spec_switch_rect_and_state(page)
            log("STEP9", f"DOM 兜底第{retry}次验证: state={retry_verify.get('state')}, aria={retry_verify.get('aria')}")
            if retry_verify.get("state") == "关":
                log("STEP9", "===== 规格开关经兜底已成功关闭 =====")
                return True
        log("STEP9", "警告: 多次点击后开关仍为'开'状态，必须阻止发布")
        return False
    else:
        log("STEP9", f"开关状态未知({verify.get('state')})，不能假设已关闭")
        return False


def _probe_spec_switch_state(page) -> dict:
    """探测“修改规格信息”规格开关状态。

    返回 dict: {
        "state": "开" / "关" / "unknown",
        "method": "smallest-container" / "ancestor-walk" / "none" / "error",
        "className": str,
        "rect": {x, y, w, h} or None,
        "aria": "true" / "false" / None,
        "containerText": str
    }
    """
    info = _find_spec_switch_rect_and_state(page)
    if not info.get("found"):
        return {
            "state": "unknown",
            "method": info.get("method", "none"),
            "className": info.get("className", ""),
            "rect": None,
            "aria": None,
            "containerText": info.get("error", "")
        }
    return {
        "state": info.get("state", "unknown"),
        "method": info.get("method", "none"),
        "className": info.get("className", ""),
        "rect": {k: info[k] for k in ("x", "y", "w", "h") if k in info},
        "aria": info.get("aria"),
        "containerText": info.get("containerText", "")
    }


# 仅这些店铺在发布后易出现“修改商品规格”开关未关导致“当前规格层级超过上限”的问题，
# 发布失败落到价格库存区时，仿照库存修复做“关闭规格开关”后再重试发布。
SPEC_SWITCH_GUARD_SHOP_IDS = {"636781"}


def _maybe_close_spec_switch_for_shop(page, shop_id) -> bool:
    """636781 专属兜底：发布失败落在价格库存区时，若“修改商品规格”开关仍为打开，
    则关闭它（复用 close_spec_switch 逻辑）。返回 True 表示执行了关闭动作（重试有意义）。"""
    if not shop_id or shop_id not in SPEC_SWITCH_GUARD_SHOP_IDS:
        return False
    probe = _probe_spec_switch_state(page)
    state = probe.get("state", "unknown")
    log("STEP10", f"[{shop_id}] 专属兜底探测规格开关: state={state}, method={probe.get('method')}, class={probe.get('className', '')}")
    if state != "开":
        return False
    log("STEP10", f"[{shop_id}] 专属兜底：规格开关仍为打开，尝试关闭后重试发布")
    return bool(close_spec_switch(page))


def ensure_spec_switch_closed(page, stage: str) -> bool:
    """Keep the browser editor's spec switch off at each write boundary.

    DouDian may rebuild the specification section on every page visit or tab
    change, reopening the switch.  It is a page-local UI setting, so an earlier
    successful click cannot be treated as persistent.  This phone-only browser
    workflow must fail closed if the switch is missing or its state is unknown.
    """
    for attempt in range(1, 4):
        probe = _probe_spec_switch_state(page)
        state = probe.get("state", "unknown")
        class_name = str(probe.get("className", ""))
        log("SPEC", "%s规格总开关检查%d: state=%s method=%s class=%s" % (
            stage, attempt, state, probe.get("method"), class_name))
        if state == "开":
            if not close_spec_switch(page):
                continue
        elif state == "unknown":
            time.sleep(0.5)
            continue

        # React 重绘期间读到一次“关”还不是稳定状态。
        stable_closed_reads = 0
        for _ in range(12):
            time.sleep(0.5)
            settled = _probe_spec_switch_state(page)
            settled_class = str(settled.get("className", ""))
            if settled.get("state") == "关" and "loading" not in settled_class:
                stable_closed_reads += 1
                if stable_closed_reads >= 2:
                    log("SPEC", "%s规格总开关已稳定关闭" % stage)
                    return True
            else:
                stable_closed_reads = 0
            if settled.get("state") == "开":
                log("SPEC", "%s规格总开关在页面重绘后重新打开，将再次关闭" % stage)
                break
    log("SPEC", "%s规格总开关无法确认关闭，阻止后续操作" % stage)
    return False


def _ensure_spec_switch_closed_before_publish(page) -> bool:
    """Backward-compatible publish guard, now shared with draft and page-open."""
    return ensure_spec_switch_closed(page, "发布前")


def save_draft(page) -> bool:
    """滚动到页面底部并点击'保存草稿'按钮"""
    log("STEP10", "===== 保存草稿 =====")

    # 发布校验失败后，属性补全面板/规则弹窗可能遮住底部操作栏。
    # 只执行取消、关闭、稍后、Escape 等非确认动作，绝不猜测提交按钮。
    _dismiss_blocking_overlay_before_save(page)

    # 先检查是否还在编辑页，如果已离开，说明可能已发布成功，跳过保存
    current_url = page.url
    if "/ffa/g/create" not in current_url:
        log("STEP10", f"当前不在编辑页（{current_url[:80]}），可能已发布成功，跳过保存草稿")
        return True

    if not ensure_spec_switch_closed(page, "保存草稿前"):
        set_last_publish_error("保存草稿前无法确认修改规格信息开关已关闭")
        return False

    log("STEP10", "滚动到页面底部...")
    page.evaluate("""
        () => {
            window.scrollTo(0, document.body.scrollHeight);
            return 'scrolled to bottom';
        }
    """)
    time.sleep(1)

    save_writes = []

    def _capture_save_response(response):
        try:
            request = response.request
            method = str(request.method or "").upper()
            url = str(response.url or "")
            if method in ("POST", "PUT", "PATCH") and response.status in range(200, 300):
                lowered = url.casefold()
                # 只接受明确的写入端点。diagnose_product 等诊断请求同样返回
                # 200，但不代表草稿落库，必须排除。
                is_save_write = any(token in lowered for token in (
                    "editwithschema", "createwithschema", "save_draft", "savedraft",
                    "/draft/save", "/product/save", "/product/update",
                ))
                if is_save_write and "diagnose" not in lowered:
                    save_writes.append({"method": method, "status": response.status, "url": url})
        except Exception:
            pass

    response_listener_supported = hasattr(page, "on")
    if response_listener_supported:
        page.on("response", _capture_save_response)
    save_result = page.evaluate("""
        () => {
            let buttons = document.querySelectorAll('button, [role="button"]');
            for (let btn of buttons) {
                let text = btn.innerText.trim();
                if (text.includes('保存草稿') || text === '保存草稿') {
                    let rect = btn.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        btn.click();
                        return 'clicked by text: ' + text;
                    }
                }
            }
            let allBtns = document.querySelectorAll('button, a, div[role="button"]');
            for (let btn of allBtns) {
                let className = btn.className || '';
                let id = btn.id || '';
                if ((className.includes('draft') || className.includes('save') ||
                     id.includes('draft') || id.includes('save')) &&
                    (btn.innerText.includes('保存') || btn.innerText.includes('草稿'))) {
                    let rect = btn.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        btn.click();
                        return 'clicked by class/id: ' + (btn.innerText || '').substring(0, 20);
                    }
                }
            }
            let actionBars = document.querySelectorAll('[class*="action"], [class*="footer"], [class*="bottom"]');
            for (let bar of actionBars) {
                let barRect = bar.getBoundingClientRect();
                if (barRect.top > window.innerHeight * 0.7) {
                    let btns = bar.querySelectorAll('button');
                    for (let btn of btns) {
                        if (btn.innerText.includes('保存')) {
                            btn.click();
                            return 'clicked from action bar: ' + btn.innerText.trim();
                        }
                    }
                }
            }
            return 'not found';
        }
    """)

    log("STEP10", f"保存按钮点击结果: {save_result}")

    if "not found" in save_result:
        save_result = _visual_click_save_draft(page)
        log("STEP10", "保存按钮视觉兜底结果: %s" % save_result)
        if not save_result.startswith("clicked by vision"):
            log("STEP10", "未找到保存草稿按钮")
            if response_listener_supported:
                try:
                    page.remove_listener("response", _capture_save_response)
                except Exception:
                    pass
            return False

    log("STEP10", "等待明确的保存成功证据...")
    save_verify = {"success": False, "message": "尚未检测"}
    # toast 通常只存在很短时间，不能先固定 sleep 3 秒再读取。立即开始轮询；
    # “仍在编辑页”不是保存成功证据，只能接受明确成功提示或已返回草稿箱。
    for _ in range(12):
        save_verify = page.evaluate("""
            () => {
                let url = window.location.href || '';
                if (url.includes('/ffa/g/draft')) {
                    return { success: true, message: '已返回草稿箱' };
                }
                let nodes = document.querySelectorAll(
                    '.ant-message, .ant-notification, [class*="toast"], [class*="message"], [role="alert"]'
                );
                for (let node of nodes) {
                    let text = (node.innerText || '').trim();
                    if (!text) continue;
                    if ((text.includes('草稿') && text.includes('成功')) ||
                        text.includes('保存草稿成功') || text.includes('保存成功')) {
                        return { success: true, message: text.substring(0, 100) };
                    }
                    if (text.includes('保存失败') || text.includes('草稿保存失败')) {
                        return { success: false, terminal: true, message: text.substring(0, 100) };
                    }
                }
                return { success: false, terminal: false,
                         message: url.includes('/ffa/g/create') ? '仍在编辑页，未发现成功提示' : '页面状态未知' };
            }
        """)
        if save_verify.get("success") or save_verify.get("terminal"):
            break
        time.sleep(0.5)

    if response_listener_supported:
        try:
            page.remove_listener("response", _capture_save_response)
        except Exception:
            pass

    if not save_verify.get("success") and save_writes:
        evidence = save_writes[-1]
        save_verify = {
            "success": True,
            "message": "保存接口返回 HTTP %s: %s" %
                       (evidence["status"], evidence["url"][:160]),
        }

    log("STEP10", f"保存状态: {save_verify}")
    if save_verify.get("success"):
        log("STEP10", "===== 保存草稿完成 =====")
        return True
    log("STEP10", "保存草稿未获得明确成功证据，按失败处理")
    return False


def _click_publish_button(page) -> str:
    """滚动到页面底部并点击“发布商品”按钮，返回点击结果描述。"""
    page.evaluate("""
        () => {
            window.scrollTo(0, document.body.scrollHeight);
            return 'scrolled to bottom';
        }
    """)
    time.sleep(1)

    return page.evaluate("""
        () => {
            let buttons = document.querySelectorAll('button, [role="button"]');
            for (let btn of buttons) {
                let text = btn.innerText.trim();
                if (text.includes('发布商品') || text === '发布商品') {
                    let rect = btn.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        btn.click();
                        return 'clicked by text: ' + text;
                    }
                }
            }
            let allBtns = document.querySelectorAll('button, a, div[role="button"]');
            for (let btn of allBtns) {
                let className = btn.className || '';
                let id = btn.id || '';
                if ((className.includes('publish') || className.includes('submit') ||
                     id.includes('publish') || id.includes('submit')) &&
                    btn.innerText.includes('发布')) {
                    let rect = btn.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        btn.click();
                        return 'clicked by class/id: ' + (btn.innerText || '').substring(0, 20);
                    }
                }
            }
            let actionBars = document.querySelectorAll('[class*="action"], [class*="footer"], [class*="bottom"]');
            for (let bar of actionBars) {
                let barRect = bar.getBoundingClientRect();
                if (barRect.top > window.innerHeight * 0.7) {
                    let btns = bar.querySelectorAll('button');
                    for (let btn of btns) {
                        if (btn.innerText.includes('发布商品')) {
                            btn.click();
                            return 'clicked from action bar: ' + btn.innerText.trim();
                        }
                    }
                }
            }
            return 'not found';
        }
    """)


def _dismiss_attribute_optimization_popup(page) -> dict:
    """
    检测并关闭"商品属性待优化"弹窗（点击"不修改，继续发布"）。
    返回 {'dismissed': bool, 'detail': str}。
    若弹窗不存在，返回 {'dismissed': False, 'detail': '无弹窗'}。
    """
    return page.evaluate("""
        () => {
            // 查找包含"商品属性待优化"标题的模态框
            let modals = document.querySelectorAll('[class*="modal"], [role="dialog"], [class*="dialog"]');
            for (let m of modals) {
                let text = m.innerText || '';
                if (!text.includes('商品属性待优化')) continue;
                // 找到目标弹窗，点击"不修改，继续发布"按钮
                let buttons = m.querySelectorAll('button, [role="button"], [class*="btn"]');
                for (let btn of buttons) {
                    let btnText = (btn.innerText || '').trim();
                    if (btnText.includes('不修改') && btnText.includes('继续发布')) {
                        let rect = btn.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            btn.click();
                            return { dismissed: true, detail: '已点击"不修改，继续发布"' };
                        }
                    }
                }
                // 按钮未找到或不可见，返回弹窗存在但未处理
                return { dismissed: false, detail: '发现"商品属性待优化"弹窗，但未找到"不修改，继续发布"按钮' };
            }
            return { dismissed: false, detail: '无弹窗' };
        }
    """)


def _detect_publish_result(page) -> dict:
    """
    检测当前页面发布结果。
    返回 {success: bool, reason: str, debug?: [...]}。
    """
    return page.evaluate("""
        () => {
            function isMeaningfulError(text) {
                if (!text) return false;
                let t = text.trim();
                if (t.length < 5) return false;
                // 平台规则调整/填写助手属于全局运营通知，即使含“必填”也不是
                // 本商品的表单校验错误，不能据此判定发布失败。
                let globalNotices = ['平台将于', '调整属性规则', '即将变为必填项', '填写助手'];
                if (globalNotices.some(w => t.includes(w))) return false;
                let noise = ['错误', '失败', '举报', '请选择', '去申请', '修改'];
                let stripped = t;
                for (let w of noise) stripped = stripped.split(w).join('');
                return stripped.trim().length >= 2;
            }

            function extractBestError(candidates) {
                let bestStrong = '';
                let bestNormal = '';
                for (let c of candidates) {
                    let isStrong = c.includes('错误问题待处理') || c.includes('个错误') || c.includes('问题待处理');
                    if (isStrong) {
                        if (c.length > bestStrong.length) bestStrong = c;
                        continue;
                    }
                    if (!isMeaningfulError(c)) continue;
                    if (c.includes('错误提示') || c.includes('提升建议') || c.includes('不合规')) {
                        if (c.length > bestNormal.length) bestNormal = c;
                    } else if (!bestNormal) {
                        bestNormal = c;
                    }
                }
                return bestStrong || bestNormal;
            }

            let toasts = document.querySelectorAll('.ant-message, .ant-notification, [class*="toast"], [class*="success"], [class*="message"]');
            for (let t of toasts) {
                let text = (t.innerText || '').trim();
                if (!text) continue;
                // 只把明确与“发布/提交成功”相关的文案视为成功；"保存成功"等不算
                if (text.includes('发布成功') || text.includes('提交成功') || /(发布|提交).*成功|成功.*(发布|提交)/.test(text)) {
                    return { success: true, reason: text.substring(0, 80) };
                }
                if (text.includes('失败') || text.includes('错误') || text.includes('不合规') ||
                    text.includes('不完善') || text.includes('请完善') || text.includes('无法')) {
                    if (isMeaningfulError(text)) {
                        return { success: false, reason: text.substring(0, 120) };
                    }
                }
            }

            let modals = document.querySelectorAll('.ant-modal, [class*="modal"], [class*="dialog"], [role="dialog"]');
            let modalCandidates = [];
            for (let m of modals) {
                let text = (m.innerText || '').trim();
                if (text && (text.includes('失败') || text.includes('错误') || text.includes('不合规') ||
                             text.includes('不完善') || text.includes('请完善') || text.includes('无法发布'))) {
                    modalCandidates.push(text);
                }
            }
            let bestModal = extractBestError(modalCandidates);
            if (bestModal) {
                return { success: false, reason: bestModal.substring(0, 120) };
            }

            let panelSelectors = '[class*="error"], [class*="helper"], [class*="assist"], [class*="error-list"], [class*="problem"], ' +
                                 '[class*="form-tip"], [class*="tips"], [class*="guide"], [class*="suggest"]';
            let errorPanels = document.querySelectorAll(panelSelectors);
            let panelCandidates = [];
            for (let p of errorPanels) {
                let text = (p.innerText || '').trim();
                if (!text) continue;
                if (text.includes('待处理') || text.includes('错误提示') || text.includes('错误问题') ||
                    text.includes('提升建议') || text.includes('个错误') || text.includes('问题待处理') ||
                    text.includes('不合规') || text.includes('商品规格') || text.includes('标准化')) {
                    panelCandidates.push(text);
                }
            }
            let allEls = document.querySelectorAll('div, section, aside');
            for (let el of allEls) {
                let text = (el.innerText || '').trim();
                if (text.includes('个错误') || text.includes('错误问题待处理')) {
                    panelCandidates.push(text);
                }
            }
            let bestPanel = extractBestError(panelCandidates);
            if (bestPanel) {
                return { success: false, reason: bestPanel.substring(0, 250) };
            }

            let errorNotes = document.querySelectorAll('[class*="error"], [class*="warn"], [class*="tip"], [class*="notice"], [role="alert"], [aria-invalid="true"]');
            let noteCandidates = [];
            for (let e of errorNotes) {
                let text = (e.innerText || '').trim();
                if (text && (text.includes('错误') || text.includes('失败') || text.includes('不合规') ||
                             text.includes('请完善') || text.includes('必填') || text.includes('不符合'))) {
                    noteCandidates.push(text);
                }
            }
            let bestNote = extractBestError(noteCandidates);
            if (bestNote) {
                return { success: false, reason: bestNote.substring(0, 120) };
            }

            // 主图/必填项错误常挂在表单项的 explain/help 节点中，既不是 toast
            // 也不一定带通用 error 类。只扫描可见且带明确校验语义的短文本，
            // 避免再次把页面固定的“反馈”按钮当成失败原因。
            let validationSelectors = '[class*="explain"], [class*="help"], [class*="validate"], ' +
                                      '[class*="feedback"], [data-status="error"], [aria-live="polite"]';
            let validationNodes = document.querySelectorAll(validationSelectors);
            let validationCandidates = [];
            for (let el of validationNodes) {
                let r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                let text = (el.innerText || el.textContent || '').trim();
                if (!text || text === '反馈' || text.length > 180) continue;
                if (text.includes('主图') || text.includes('正面图') || text.includes('请上传') ||
                    text.includes('不能为空') || text.includes('必填') || text.includes('请选择') ||
                    text.includes('请完善') || text.includes('不符合')) {
                    validationCandidates.push(text);
                }
            }
            let bestValidation = extractBestError(validationCandidates);
            if (bestValidation) {
                return { success: false, reason: bestValidation.substring(0, 160) };
            }

            let debugTexts = [];
            for (let p of errorPanels) debugTexts.push(p.innerText.trim().substring(0, 200));
            for (let e of errorNotes) debugTexts.push(e.innerText.trim().substring(0, 200));

            // 兜底扫描：toast/modal/panel 检查均未命中时，扩大范围搜索成功关键词
            // 因为抖店某些成功提示（如"商品提交成功"）可能不出现在标准 toast 容器中
            let broaderSelectors = document.querySelectorAll('div, span, p, section, [class*="step"], [class*="process"], [class*="progress"], [class*="result"], [class*="status"]');
            for (let el of broaderSelectors) {
                let text = (el.innerText || '').trim();
                if (!text || text.length < 5 || text.length > 300) continue;
                if ((text.includes('商品提交成功') || text.includes('已提交成功') ||
                     text.includes('发布成功') || text.includes('已发布成功') ||
                     text.includes('审核通过后生效') || text.includes('提交成功')) &&
                    !text.includes('错误') && !text.includes('失败') && !text.includes('不合规')) {
                    return { success: true, reason: text.substring(0, 100) };
                }
            }

            // 如果 debugTexts 中已有成功关键词，也视为成功
            for (let dt of debugTexts) {
                if ((dt.includes('商品提交成功') || dt.includes('已提交成功') ||
                     dt.includes('发布成功') || dt.includes('已发布成功') ||
                     dt.includes('审核通过后生效')) &&
                    !dt.includes('错误') && !dt.includes('失败') && !dt.includes('不合规')) {
                    return { success: true, reason: dt.substring(0, 100) };
                }
            }

            let stillOnEdit = window.location.href.includes('/ffa/g/create');
            if (!stillOnEdit) {
                return { success: true, reason: '已离开编辑页' };
            }

            return { success: false, reason: '点击发布后仍在编辑页，可能信息不完善或不合规', debug: debugTexts.slice(0, 5) };
        }
    """)


def probe_required_multiselects(page) -> list:
    """Read semantic labels/options for empty “最多选N项” fields; never selects values."""
    candidates = page.evaluate(r"""
        () => {
            document.querySelectorAll('[data-wb-probe-multiselect]').forEach(
                e=>e.removeAttribute('data-wb-probe-multiselect'));
            const results=[];
            for(const select of document.querySelectorAll('.ecom-g-select')){
                const ph=select.querySelector('.ecom-g-select-selection-placeholder');
                const placeholder=(ph&&ph.innerText||'').trim();
                if(!placeholder.includes('最多选')) continue;
                let p=select,label='',scopeText='',debug=[];
                const ariaLabelled=select.getAttribute('aria-labelledby');
                if(ariaLabelled){ const el=document.getElementById(ariaLabelled); if(el) label=(el.innerText||'').trim(); }
                for(let d=0;d<7&&p;d++,p=p.parentElement){
                    const prev=p.previousElementSibling;
                    if(prev){
                        const pt=(prev.innerText||'').trim().replace(/^\s*\*\s*/,'');
                        if(!label&&pt&&pt.length<80) label=pt;
                        if(pt) debug.push('prev:'+pt.slice(0,100));
                    }
                    const labels=p.querySelectorAll(
                        '.ecom-g-form-item-label,label,[class*="form-item-label"],[class*="field-label"]');
                    for(const el of labels){
                        const t=(el.innerText||'').trim().replace(/^\s*\*\s*/,'');
                        if(t&&t!==placeholder&&t.length<80){label=t;break;}
                    }
                    const own=(p.innerText||'').trim();
                    if(own) debug.push('d'+d+':'+own.slice(0,120));
                    if(label){scopeText=own.slice(0,240);break;}
                }
                const index=results.length;
                select.setAttribute('data-wb-probe-multiselect',String(index));
                results.push({index,placeholder,label:label||'(未识别)',scopeText,debug:debug.slice(0,12)});
            }
            return results;
        }
    """)
    findings = []
    for candidate in candidates:
        options = []
        try:
            target = page.locator('[data-wb-probe-multiselect="%s"]' % candidate["index"])
            if target.count() != 1:
                raise RuntimeError("探查目标重绘后丢失")
            target.scroll_into_view_if_needed()
            trigger = target.locator('.ecom-g-select-selector')
            (trigger if trigger.count() == 1 else target).click()
            time.sleep(0.8)
            option_probe = page.evaluate(r"""
                () => {
                    const visible=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
                    const options=[...document.querySelectorAll(
                    '[role="option"],.ecom-g-select-item,.ecom-g-select-item-option,'+
                    '.ecom-g-select-item-option-content,.ant-select-item-option-content,'+
                    '.rc-virtual-list-holder-inner>div')]
                    .filter(visible)
                    .map(el=>(el.innerText||el.textContent||'').trim())
                    .filter((v,i,a)=>v&&a.indexOf(v)===i);
                    const listboxes=[...document.querySelectorAll('[role="listbox"],.ecom-g-select-dropdown')]
                        .filter(visible).map(el=>(el.innerText||'').trim().slice(0,1000));
                    return {options,listboxes};
                }
            """)
            options = option_probe.get("options") or []
            candidate["listboxes"] = option_probe.get("listboxes") or []
            if not options and candidate["listboxes"]:
                options = []
                for block in candidate["listboxes"]:
                    for value in str(block).splitlines():
                        value = value.strip()
                        if value and value not in options and "点击反馈" not in value:
                            options.append(value)
            page.keyboard.press("Escape")
        except Exception as exc:
            candidate["error"] = str(exc)
        candidate["options"] = options
        findings.append(candidate)
        log("ATTR_PROBE", "必填多选字段探查: label=%r placeholder=%r options=%r scope=%r error=%r" % (
            candidate.get("label"), candidate.get("placeholder"), options,
            candidate.get("scopeText", "")[:160], candidate.get("error", "")))
        if candidate.get("listboxes") and not options:
            log("ATTR_PROBE", "下拉框可见文本: %r" % candidate.get("listboxes"))
        if candidate.get("label") == "(未识别)":
            log("ATTR_PROBE", "字段DOM语义链: %r" % candidate.get("debug", []))
    return findings


def _try_fix_negative_stock(page) -> dict:
    """
    检查"价格库存"页中是否存在"可售库存"为负数的行。
    若存在，把对应行的"现货库存"输入框直接设为合适的值（不走增/减按钮）。

    由于抖店校验"库存数量不能低于预留库存"，因此目标值为：
        target = max(0, 预留库存)
    这样可售库存 = 现货库存 - 预留库存 >= 0，且满足平台校验。

    抖店的"价格库存"表格使用虚拟滚动（ecom-g-table-virtual）：
      - DOM 中只渲染当前可视行（约 10-12 行），其余行未渲染
      - .ecom-g-table-container 自身不可滚动（maxScroll=0）
      - 只有真实鼠标滚轮事件才会触发虚拟表格重新渲染
      - 直接设置 scrollTop 或 dispatchEvent('wheel') 都不生效

    本函数采用：
      1. 把鼠标移动到表格区域中心；
      2. 调用 page.mouse.wheel 触发虚拟滚动；
      3. 每轮扫描当前可见行中"可售库存"为负的行；
      4. 给未处理过的 input 加 TARGET_CLASS 并记录目标值；
      5. Python 侧 Playwright fill 设值；
      6. 用 data-row-key 跟踪已处理的行，避免虚拟滚动来回滚时重复处理；
      7. 完整滚到底（连续多轮无可修复行）时停止。

    返回 {fixed: 修复行数, reason: 说明, rows: [...], diag: {...}}。
    """
    TARGET_CLASS = '__fix_stock_target__'
    DONE_CLASS = '__fix_stock_done__'
    ROW_DONE_CLASS = '__fix_stock_row_done__'
    MAX_ROUNDS = 600          # 每轮滚一行(70px)，最多 600 轮。平台 SKU 上限 450 个，
                               # 部分机型(如 OPPO Reno14)规格行极多、负值落在最后一行，
                               # 80 轮(≈5600px)滚不到底导致漏修；放大到 600 保证能滚到末尾。
                               # 正常到底后由 same_view_rounds>=2 提前结束，600 仅作硬保险。
    IDLE_LIMIT = 4             # 仅用于日志：连续多少轮无负库存（不再作为提前停止条件）

    total_fixed = 0
    all_details = []
    last_diag = {}

    def _table_center() -> dict:
        return page.evaluate(r"""
            () => {
                let el = document.querySelector('.styles_skuTable__oiABK, .ecom-g-table-wrapper, .ecom-g-table');
                if (!el) return null;
                let rect = el.getBoundingClientRect();
                if (rect.width === 0) return null;
                return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + Math.min(rect.height / 2, 300)) };
            }
        """)

    def _scroll_table(delta_y: int) -> dict:
        """用真实鼠标滚轮触发虚拟表格重新渲染。"""
        pos = _table_center()
        if not pos:
            return {'ok': False, 'reason': 'table area not found'}
        try:
            page.mouse.move(pos['x'], pos['y'])
            page.mouse.wheel(0, delta_y)
        except Exception as e:
            return {'ok': False, 'reason': f'mouse wheel failed: {e}'}
        time.sleep(0.6)
        return {'ok': True, 'reason': 'mouse wheel', 'deltaY': delta_y, 'pos': pos}

    def _scroll_to_top() -> dict:
        """滚到表格顶部（用 scrollIntoView 触发虚拟表格跳回首行）。"""
        return page.evaluate(r"""
            () => {
                let rows = document.querySelectorAll('tr[data-row-key]');
                if (rows.length === 0) return { ok: false, reason: 'no rows' };
                rows[0].scrollIntoView({ behavior: 'auto', block: 'start' });
                return { ok: true };
            }
        """)

    def _scan_and_mark() -> dict:
        """扫描当前可见行，给未修复的负库存 input 加标记并记录目标值。"""
        return page.evaluate(r"""
            () => {
                let diag = { steps: [] };

                // 关闭可能遮挡的"保存成功"等弹窗
                let modals = document.querySelectorAll('.ant-modal, [class*="modal"], [role="dialog"], [class*="mask"]');
                for (let m of modals) {
                    let txt = (m.innerText || '').trim();
                    if (txt.includes('保存成功') || txt.includes('我知道了')) {
                        let confirmBtns = m.querySelectorAll('button');
                        for (let b of confirmBtns) {
                            if (b.innerText.includes('我知道了') || b.innerText.includes('确定') || b.innerText.includes('确认')) {
                                b.click();
                                diag.steps.push('closed save-success modal');
                                break;
                            }
                        }
                    }
                }

                // 找表格
                function findTableWithColumns() {
                    let candidates = [];
                    document.querySelectorAll('table').forEach(t => candidates.push(t));
                    document.querySelectorAll('[class*="table"], [class*="Table"]').forEach(el => {
                        if (!candidates.includes(el)) candidates.push(el);
                    });
                    let best = null, bestDataRows = 0;
                    for (let t of candidates) {
                        let theadRow = t.querySelector('thead tr');
                        if (!theadRow) {
                            let firstRow = t.querySelector('tr');
                            if (firstRow && firstRow.querySelector('th')) theadRow = firstRow;
                        }
                        if (!theadRow) continue;
                        let headers = Array.from(theadRow.children).filter(c => c.tagName === 'TH' || c.tagName === 'TD');
                        let headerTexts = headers.map(h => (h.innerText || '').trim());
                        if (!headerTexts.some(h => h.includes('现货库存')) || !headerTexts.some(h => h.includes('可售库存'))) continue;
                        let dataRows = 0;
                        t.querySelectorAll('tr').forEach(r => { if (r.querySelector('td')) dataRows++; });
                        if (dataRows > bestDataRows) { best = { el: t, headers: headers, headerTexts: headerTexts, dataRows: dataRows }; bestDataRows = dataRows; }
                    }
                    return best;
                }

                let tableInfo = findTableWithColumns();
                if (!tableInfo) {
                    diag.reason = 'price-stock table not found';
                    return { fixed: 0, reason: diag.reason, rows: [], diag: diag };
                }
                diag.tableHeaderTexts = tableInfo.headerTexts;
                diag.rowCount = tableInfo.dataRows;

                let headers = tableInfo.headers;
                let spotIdx = -1, sellableIdx = -1, reservedIdx = -1;
                for (let i = 0; i < headers.length; i++) {
                    let txt = (headers[i].innerText || '').trim();
                    if (txt.includes('现货库存')) spotIdx = i;
                    if (txt.includes('可售库存')) sellableIdx = i;
                    if (txt.includes('预留库存')) reservedIdx = i;
                }
                diag.spotIdx = spotIdx;
                diag.sellableIdx = sellableIdx;
                diag.reservedIdx = reservedIdx;
                if (spotIdx < 0 || sellableIdx < 0) {
                    diag.reason = 'required columns not found';
                    return { fixed: 0, reason: diag.reason, rows: [], diag: diag };
                }

                // 清掉过期的 TARGET_CLASS（保留 DONE_CLASS）
                document.querySelectorAll('.' + '__fix_stock_target__').forEach(el => el.classList.remove('__fix_stock_target__'));

                let t = tableInfo.el;
                let fixed = 0;
                let jsFixed = 0;
                let targetFixed = 0;
                let details = [];
                let visibleRowKeys = [];

                t.querySelectorAll('tr[data-row-key]').forEach(row => {
                    if (!row.querySelector('td')) return;
                    let rowKey = row.getAttribute('data-row-key') || '';
                    visibleRowKeys.push(rowKey);

                    // 已修复的行直接跳过（row-level 标记）
                    if (row.classList.contains('__fix_stock_row_done__')) return;

                    let cells = Array.from(row.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH');
                    if (cells.length <= Math.max(spotIdx, sellableIdx)) return;

                    let sellableCell = cells[sellableIdx];
                    let sellableMatch = (sellableCell.innerText || '').trim().match(/-?[0-9]+/);
                    let sellable = sellableMatch ? parseInt(sellableMatch[0], 10) : NaN;
                    let sellableNegative = (!isNaN(sellable) && sellable < 0);

                    let spotCell = cells[spotIdx];
                    // 现货库存输入框选择器：
                    // 优先 .ecom-g-input-number-input，其次 [role=spinbutton]，
                    // 再 type=number，再 type=text，最后任何可见 input
                    let input = spotCell.querySelector(
                        'input.ecom-g-input-number-input, input[role="spinbutton"], input[type="number"], input[type="text"]:not([disabled])'
                    );
                    if (!input) {
                        let cands = spotCell.querySelectorAll('input:not([type=radio]):not([type=hidden])');
                        for (let inp of cands) {
                            let rect = inp.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) { input = inp; break; }
                        }
                    }
                    if (!input || input.classList.contains('__fix_stock_done__')) return;

                    // 平台报错“输入库存数量最小应为0”指向现货库存输入框本身为负。
                    // 可售库存显示列可能因未重渲染而仍为非负，故同时直接检查输入框的值。
                    let inputVal = (input.value || '').trim();
                    let inputMatch = inputVal.match(/-?[0-9]+/);
                    let inputNegative = inputMatch ? (parseInt(inputMatch[0], 10) < 0) : false;

                    if (!sellableNegative && !inputNegative) return;

                    // 读取预留库存，计算目标现货库存
                    let target = '0';
                    if (reservedIdx >= 0 && cells.length > reservedIdx) {
                        let reservedMatch = (cells[reservedIdx].innerText || '').trim().match(/[0-9]+/);
                        if (reservedMatch) {
                            let reserved = parseInt(reservedMatch[0], 10);
                            if (!isNaN(reserved) && reserved > 0) target = String(reserved);
                        }
                    }

                    let currentValue = (input.value || '').trim();
                    // 也参考现货库存显示值（有些 input 为空但显示有值）
                    if (!currentValue) {
                        let disp = spotCell.querySelector('.style_stockNum__NtDzx');
                        if (disp) currentValue = (disp.innerText || '').trim();
                    }
                    if (currentValue === target) {
                        row.classList.add('__fix_stock_row_done__');
                        input.classList.add('__fix_stock_done__');
                        return;
                    }

                    // 在发现时立即用 JS 填充（虚拟表格中元素当前一定 attached）
                    let setOk = false;
                    try {
                        let nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        nativeSetter.call(input, target);
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        input.dispatchEvent(new Event('blur', { bubbles: true }));
                        setOk = true;
                    } catch (err) {
                        diag.steps.push('js fill failed for row ' + rowKey + ': ' + err.message);
                    }

                    // 清理 rowText：把换行/回车/制表符等空白统一替换成单个空格，避免日志“乱码”
                    let rowText = (row.innerText || '').trim().replace(/\s+/g, ' ').substring(0, 120);

                    if (setOk) {
                        input.classList.remove('__fix_stock_target__');
                        input.classList.add('__fix_stock_done__');
                        row.classList.add('__fix_stock_row_done__');
                        fixed++;
                        jsFixed++;
                        details.push({
                            rowKey: rowKey,
                            sellable: sellable,
                            oldValue: currentValue,
                            targetValue: target,
                            rowText: rowText
                        });
                    } else {
                        // JS 填充失败时回退到 Playwright 兜底：保留 TARGET 标记
                        input.classList.add('__fix_stock_target__');
                        input.setAttribute('data-fix-stock-target', target);
                        input.setAttribute('data-fix-stock-rowkey', rowKey);
                        targetFixed++;
                        details.push({
                            rowKey: rowKey,
                            sellable: sellable,
                            oldValue: currentValue,
                            targetValue: target,
                            rowText: rowText
                        });
                    }
                });

                diag.fixed = fixed;
                diag.jsFixed = jsFixed;
                diag.targetFixed = targetFixed;
                diag.visibleRowKeys = visibleRowKeys.slice(0, 5);
                diag.visibleRowCount = visibleRowKeys.length;
                diag.reason = fixed > 0 ? 'fixed negative sellable stock inputs via JS' : 'no negative sellable stock in current view';
                return { fixed: fixed, jsFixed: jsFixed, targetFixed: targetFixed, reason: diag.reason, rows: details, diag: diag };
            }
        """)

    def _fill_target_inputs() -> int:
        """兜底：对 JS 填充失败仍保留 TARGET 标记的 input 再用 Playwright 填充，返回成功数量。"""
        count = 0
        try:
            inputs = page.locator(f"input.{TARGET_CLASS}").all()
            for inp in inputs:
                row_key = ''
                target = '0'
                try:
                    row_key = inp.evaluate("el => el.getAttribute('data-fix-stock-rowkey') || ''")
                    target = inp.evaluate("el => el.getAttribute('data-fix-stock-target') || '0'")
                except Exception:
                    target = '0'
                try:
                    # 优先用 JS 直接设值（避免虚拟表格中元素滚动后 detached 导致 click 超时）
                    js_ok = inp.evaluate("""
                        (el, value) => {
                            try {
                                let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                                setter.call(el, value);
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                el.dispatchEvent(new Event('blur', { bubbles: true }));
                                el.classList.remove('__fix_stock_target__');
                                el.classList.add('__fix_stock_done__');
                                let r = el.closest('tr[data-row-key]');
                                if (r) r.classList.add('__fix_stock_row_done__');
                                return true;
                            } catch (e) {
                                return false;
                            }
                        }
                    """, target)
                    if js_ok:
                        count += 1
                        continue

                    # JS 失败再尝试 Playwright（短超时，避免卡死）
                    try:
                        inp.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    inp.click(timeout=3000)
                    inp.fill('', timeout=3000)
                    inp.fill(target, timeout=3000)
                    try:
                        inp.blur()
                    except Exception:
                        pass
                    inp.evaluate("el => { el.classList.remove('__fix_stock_target__'); el.classList.add('__fix_stock_done__'); let r=el.closest('tr[data-row-key]'); if(r) r.classList.add('__fix_stock_row_done__'); }")
                    count += 1
                except Exception as e:
                    log("STEP10", f"库存修复兜底 fill 异常 rowKey={row_key} target={target}: {e}")
                    try:
                        inp.evaluate("el => { el.classList.remove('__fix_stock_target__'); el.classList.add('__fix_stock_done__'); let r=el.closest('tr[data-row-key]'); if(r) r.classList.add('__fix_stock_row_done__'); }")
                    except Exception:
                        pass
            time.sleep(0.3)
        except Exception as e:
            log("STEP10", f"库存修复兜底 fill 批次异常: {e}")
        return count

    # 0. 滚到表格顶部
    _scroll_to_top()
    time.sleep(0.5)

    idle_rounds = 0           # 仅用于日志：连续无负库存的轮数
    same_view_rounds = 0      # 连续可视行未变化的轮数（用于判定真正到底）
    last_row_keys_sig = ''

    for round_num in range(1, MAX_ROUNDS + 1):
        # 1. 扫描并标记
        scan_result = _scan_and_mark()
        diag = scan_result.get("diag") or {}
        last_diag = diag
        fixed_this_round = scan_result.get("fixed", 0)
        visible_keys = diag.get('visibleRowKeys', [])

        if fixed_this_round > 0:
            idle_rounds = 0
            js_fixed_this_round = scan_result.get("jsFixed", 0)
            target_fixed_this_round = scan_result.get("targetFixed", 0)
            log("STEP10", f"库存修复第 {round_num} 轮: 发现 {fixed_this_round} 行负库存 (JS={js_fixed_this_round}, TARGET={target_fixed_this_round}, 可见行 keys={visible_keys[:3]})")
            for d in scan_result.get("rows", [])[:5]:
                log("STEP10", f"  修复行: rowKey={d.get('rowKey')}, 可售={d.get('sellable')}, 目标={d.get('targetValue')}, 内容={d.get('rowText', '')[:80]}")

            # 2. JS 已立即修复大部分行；_fill_target_inputs 仅兜底处理仍保留 TARGET 标记的 input
            total_fixed += js_fixed_this_round
            filled_count = _fill_target_inputs()
            total_fixed += filled_count
            all_details.extend(scan_result.get("rows", []))
        else:
            idle_rounds += 1
            if idle_rounds <= 2:
                log("STEP10", f"库存修复第 {round_num} 轮: 当前视图无新负库存 (idle={idle_rounds})")

        # 3. 检查可视行是否变化（虚拟表格是否真的换了内容）
        cur_sig = ','.join(visible_keys)
        row_changed = (cur_sig != last_row_keys_sig and len(cur_sig) > 0)
        last_row_keys_sig = cur_sig

        # 4. 连续 2 轮可视行未变化 = 虚拟表格真正到底（单次 wheel 不生效的抖动不计），
        #    停止滚动。不再因“连续几轮没负值”提前退出——否则颜色/种类多的机型的
        #    负库存行在表格靠下时会被漏掉，导致 fixed=0 不触发再次发布。
        if not row_changed:
            same_view_rounds += 1
        else:
            same_view_rounds = 0
        if round_num > 1 and same_view_rounds >= 2:
            log("STEP10", f"库存修复第 {round_num} 轮: 虚拟表格已到底（连续 {same_view_rounds} 轮可视行无变化）")
            break

        # 5. 已达最大轮数（兜底超时保护，仅作为硬上限，正常由第 4 步判到底提前结束）
        if round_num >= MAX_ROUNDS:
            log("STEP10", f"库存修复已达最大轮数 {MAX_ROUNDS}，停止遍历")
            break

        # 6. 滚到下一行（一次滚 1 行高度，约 60-70px）
        scroll_info = _scroll_table(70)
        if round_num <= 3 or round_num % 5 == 0:
            log("STEP10", f"库存修复第 {round_num} 轮滚动: {scroll_info} | row_changed={row_changed}")

    # 6. 滚回顶部再扫一次，确认是否漏了开头的行
    _scroll_to_top()
    time.sleep(0.8)
    final_scan = _scan_and_mark()
    final_fixed = final_scan.get("fixed", 0)
    if final_fixed > 0:
        log("STEP10", f"库存修复最终轮: 发现 {final_fixed} 行负库存（开头漏掉的）")
        for d in final_scan.get("rows", [])[:5]:
            log("STEP10", f"  最终行: rowKey={d.get('rowKey')}, 可售={d.get('sellable')}, 目标={d.get('targetValue')}")
        total_fixed += final_scan.get("jsFixed", 0)
        filled_count = _fill_target_inputs()
        total_fixed += filled_count
        all_details.extend(final_scan.get("rows", []))

    # 清理临时标记（保留 ROW_DONE_CLASS / DONE_CLASS 用于调试）
    try:
        page.evaluate("() => { document.querySelectorAll('.__fix_stock_target__').forEach(el => el.classList.remove('__fix_stock_target__')); }")
    except Exception:
        pass

    result = {
        "fixed": total_fixed,
        "reason": "scrolled via mouse wheel and fixed all negative sellable stock rows" if total_fixed > 0 else "no negative sellable stock found",
        "rows": all_details,
        "diag": last_diag
    }
    if total_fixed > 0:
        result["diag"]["rounds"] = round_num
    log("STEP10", f"库存修复总计: fixed={total_fixed}, rounds={round_num}")
    return result

def _is_stock_error_reason(reason: str) -> bool:
    """
    判断发布失败原因是否指向“价格与库存”区域的库存类问题（可用把现货库存设为 0 修复）。
    例如：可售库存为负、库存数量不能低于预留库存等。
    价格类问题（如售价不合规）不含这些关键词，不应进入库存修复兜底。
    """
    if not reason:
        return False
    keywords = ["库存", "可售", "现货", "价格与库存"]
    return any(k in reason for k in keywords)


def _is_on_price_stock_area(page) -> bool:
    """
    判断当前页面是否停留在“价格与库存”区域（含现货库存/可售库存列的库存表格可见）。
    用于：发布失败原因文案模糊（如“信息不完善”）时，只要仍在价格与库存区域，
    仍视为可走库存兜底修复（扫描若无负库存则自然放弃）。
    """
    try:
        return page.evaluate(r"""
            () => {
                let t = document.querySelector('.styles_skuTable__oiABK, [class*="skuTable"]');
                if (!t) return false;
                let h = (t.innerText || '');
                return h.includes('现货库存') && h.includes('可售库存');
            }
        """)
    except Exception:
        return False


def publish_product(page, shop_id: str = "") -> bool:
    """
    滚动到页面底部并点击“发布商品”按钮，失败时含库存兜底重试循环。

    流程（最多 MAX_PUBLISH_ATTEMPTS 次发布尝试，默认 3 次）：
      1. 点击“发布商品”，等待响应；成功则直接返回 True。
      2. 若失败且错误指向“价格与库存”库存类问题（可售库存为负等）：
         自动扫描表格、把“可售库存”为负的规格行“现货库存”设为 0，
         等待校验提示消失后再点“发布商品”重试（兜底循环，最多再走一遍）。
      3. 若失败但非“价格与库存”库存类问题（如价格不合规），不再重试，
         返回 False 由调用方兜底保存草稿。
      4. 若 MAX_PUBLISH_ATTEMPTS 次尝试后仍未成功，返回 False 由调用方兜底保存草稿。

    返回 True/False 表示是否发布成功。
    """
    log("STEP10", "===== 发布商品 =====")

    # 解析生效店铺ID：优先用入参，其次用全局 CURRENT_SHOP_ID（由 panel_runner 写入）。
    # 注意：process_single_product 内部的 parsed 来自商品标题、不含 shop_id，
    # 故 published 时若入参为空必须回退到全局，否则按店铺启用的特殊逻辑（如 636781）会整体失效。
    eff_shop = (shop_id or "").strip() or (CURRENT_SHOP_ID or "").strip()

    # 636781 专属：首次发布前强制确保“修改规格信息”开关处于关闭。
    # 原因：该店铺在 STEP9 关闭开关后，会因切到基础信息页固定 OPPO 型号再切回价格库存，
    # 导致页面重新渲染、开关被重置为“开”，最终发布时报“当前规格层级超过上限”。
    if eff_shop in SPEC_SWITCH_GUARD_SHOP_IDS:
        pre_probe = _probe_spec_switch_state(page)
        log("STEP10", f"[{eff_shop}] 发布前规格开关探测: state={pre_probe.get('state')}, method={pre_probe.get('method')}, class={pre_probe.get('className', '')}")
        if pre_probe.get("state") == "开":
            log("STEP10", f"[{eff_shop}] 发布前规格开关为打开，先关闭再发布")
            close_spec_switch(page)
            post_probe = _probe_spec_switch_state(page)
            log("STEP10", f"[{eff_shop}] 发布前关闭后探测: state={post_probe.get('state')}, method={post_probe.get('method')}")

    MAX_PUBLISH_ATTEMPTS = 3   # 首次 + 两次库存兜底
    last_reason = "发布商品失败（未获取到具体原因）"

    for attempt in range(1, MAX_PUBLISH_ATTEMPTS + 1):
        # 所有店铺通用：型号终检/切页可能重建规格区并把总开关重新打开。
        # 必须在每次点击发布前最后一刻再次关闭，避免机身颜色/存储容量被
        # 重新纳入必填校验。早先仅636781执行此检查，导致其他店铺假关闭。
        if not _ensure_spec_switch_closed_before_publish(page):
            set_last_publish_error("修改规格信息总开关发布前无法稳定关闭，已转人工")
            return False
        click_result = _click_publish_button(page)
        log("STEP10", f"发布按钮点击结果(第{attempt}次): {click_result}")

        if "not found" in click_result:
            log("STEP10", "未找到发布商品按钮")
            set_last_publish_error("未找到发布商品按钮")
            return False

        log("STEP10", f"等待第{attempt}次发布响应...")
        time.sleep(6)

        # 检测并关闭"商品属性待优化"弹窗（如有）
        popup = _dismiss_attribute_optimization_popup(page) or {
            "dismissed": False,
            "detail": "弹窗探测未返回结果，按无弹窗继续",
        }
        if popup.get("dismissed"):
            log("STEP10", f"检测到商品属性优化弹窗，已处理: {popup['detail']}")
            time.sleep(8)  # 等待弹窗关闭后发布结果
        else:
            log("STEP10", f"商品属性优化弹窗检查: {popup['detail']}")

        publish_result = _detect_publish_result(page)
        log("STEP10", f"发布结果(第{attempt}次): {publish_result}")

        if not publish_result.get("success") and publish_result.get("debug"):
            visual = _run_visual_check(
                page,
                "当前页面是否显示商品表单自身的校验错误？全局公告或系统通知不算。回答 yes/no。",
                "publish-result",
            )
            if visual.get("available"):
                log("STEP10", "发布错误视觉分类: %s (%s)" %
                    (visual.get("verdict"), visual.get("reason")))
            if visual.get("available") and visual.get("verdict") == "no":
                # 视觉确认只是全局通知时，不沿用关键词错误文案；页面未跳转也不能
                # 冒充发布成功，因此保守地记录为状态未确认并转人工/保存草稿。
                publish_result["reason"] = "未发现商品表单校验错误，但发布后仍未确认成功（已过滤全局公告）"

        debug = publish_result.get("debug")
        if debug:
            for i, d in enumerate(debug[:3], 1):
                log("STEP10", f"发布失败 debug[{i}]: {d[:150]}")

        if publish_result.get("success"):
            clear_last_publish_error()
            log("STEP10", f"===== 第{attempt}次发布商品成功 =====")
            return True

        # 失败 → 记录原因
        last_reason = publish_result.get("reason") or last_reason

        # 运营已确认颜色/容量/音频规格无需自动处理。遇到“最多选5项”属性
        # 补全面板时不再展开探查，直接关闭其右上角按钮，随后由主流程保存草稿。
        if is_required_multiselect_error(last_reason):
            closed = _dismiss_blocking_overlay_before_save(page)
            log("STEP10", "必填多选属性按运营规则跳过；属性面板关闭结果: closed=%s action=%s" % (
                closed.get("closed"), closed.get("action")))
            set_last_publish_error("需人工处理: 必填多选属性未填写（%s）" % last_reason)
            return False

        # 判断是否“价格与库存”库存类问题（可用设 0 修复）
        # 原因文案含库存关键词，或页面仍停留在价格与库存区域，均视为可走库存兜底
        is_price_stock_fail = _is_stock_error_reason(last_reason) or _is_on_price_stock_area(page)
        if not is_price_stock_fail:
            log("STEP10", f"发布失败且非价格库存库存类问题: {last_reason}；不再重试")
            set_last_publish_error(last_reason)
            return False

        # 已达最大尝试次数，兜底后仍失败
        if attempt >= MAX_PUBLISH_ATTEMPTS:
            log("STEP10", f"已达最大发布次数({MAX_PUBLISH_ATTEMPTS})，价格库存库存类问题仍未解决: {last_reason}")
            set_last_publish_error(last_reason)
            return False

        # 兜底（636781 专属）：若“修改商品规格”开关仍处于打开，先关闭它
        # （规格层级超过上限的主因），关闭成功即重试发布。
        spec_closed = _maybe_close_spec_switch_for_shop(page, eff_shop)

        # 兜底：扫描并修复“可售库存”为负的规格行
        log("STEP10", f"第{attempt}次发布失败(价格库存)，进入兜底库存修复...")
        fix_result = _try_fix_negative_stock(page)
        fixed = fix_result.get("fixed", 0)
        diag = fix_result.get("diag") or {}
        log("STEP10", f"库存修复结果: fixed={fixed}, reason={fix_result.get('reason')}")
        log("STEP10", f"库存修复诊断: onPriceStock={diag.get('onPriceStock')}, tabCount={diag.get('tabCount')}, rowCount={diag.get('rowCount')}, spotIdx={diag.get('spotIdx')}, sellableIdx={diag.get('sellableIdx')}")
        log("STEP10", f"库存修复表头: {diag.get('tableHeaderTexts')}")
        log("STEP10", f"库存修复标签信息: {diag.get('tabInfo')}")
        for step in diag.get("steps", []):
            log("STEP10", f"库存修复步骤: {step}")

        if fixed > 0 or spec_closed:
            log("STEP10", f"已修复库存({fixed} 行) / 关闭规格开关({spec_closed})，准备再次发布（第{attempt + 1}次）")
            for detail in fix_result.get("rows", []):
                log("STEP10", f"  修复行: 可售库存={detail.get('sellable')}, 内容={detail.get('rowText', '')[:80]}")

            log("STEP10", "等待校验响应...")
            time.sleep(1.5)
            # 进入下一轮 attempt（再次点击发布）
            continue
        else:
            log("STEP10", f"价格库存库存类问题但未发现可修复项(无负库存且规格开关已关): {last_reason}")
            set_last_publish_error(last_reason)
            return False

    set_last_publish_error(last_reason)
    return False


# =============================================================================
# 新增: 批量处理模块
# =============================================================================

# ---------- 日志管理 ----------

def load_processed_log() -> dict:
    """加载已处理商品日志。如果不存在则返回空结构。"""
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            log("LOG", f"日志文件读取失败: {e}，重建新日志")
    return {"version": 1, "products": {}}


def save_processed_log(log_data: dict) -> None:
    """保存已处理商品日志到 JSON 文件。"""
    log_data["updated_at"] = datetime.now().isoformat()
    try:
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("LOG", f"日志保存失败: {e}")


def mark_product_done(product_id: str, name: str, duration: float,
                      save_only: bool = False) -> None:
    """记录成功结果；保存草稿测试与正式发布使用不同状态。"""
    from processed_status_policy import success_status
    status = success_status(save_only)
    log_data = load_processed_log()
    log_data["products"][product_id] = {
        "name": name,
        "status": status,
        "completion_kind": "draft_test" if save_only else "published",
        "processed_at": datetime.now().isoformat(),
        "duration_seconds": round(duration, 1),
        "error": None
    }
    save_processed_log(log_data)
    if save_only:
        log("LOG", f"已记录草稿测试通过（不等于发布完成）: {name} (ID:{product_id})")
    else:
        log("LOG", f"已记录正式发布完成: {name} (ID:{product_id})")


def mark_product_failed(product_id: str, name: str, error_msg: str) -> None:
    """标记商品处理失败，允许下次重试。"""
    log_data = load_processed_log()
    log_data["products"][product_id] = {
        "name": name,
        "status": "failed",
        "processed_at": datetime.now().isoformat(),
        "duration_seconds": 0,
        "error": error_msg
    }
    save_processed_log(log_data)
    log("LOG", f"已记录失败: {name} (ID:{product_id}) - {error_msg}")


def mark_product_manual(product_id: str, name: str, reason: str) -> None:
    """标记为需人工处理；后续普通批次跳过，避免反复点击发布。"""
    log_data = load_processed_log()
    log_data["products"][product_id] = {
        "name": name,
        "status": "manual_required",
        "processed_at": datetime.now().isoformat(),
        "duration_seconds": 0,
        "error": reason,
    }
    save_processed_log(log_data)
    log("LOG", f"已转人工处理: {name} (ID:{product_id}) - {reason}")


def mark_product_skipped_no_image(product_id: str, name: str, image_path: str) -> None:
    """标记商品因缺少本地主图而跳过（补充图片后需手动重置状态再重试）。"""
    log_data = load_processed_log()
    log_data["products"][product_id] = {
        "name": name,
        "status": "skipped_no_image",
        "processed_at": datetime.now().isoformat(),
        "duration_seconds": 0,
        "error": f"缺少主图: {image_path}"
    }
    save_processed_log(log_data)
    log("LOG", f"已记录缺主图跳过: {name} (ID:{product_id}) - {image_path}")


# ---------- 草稿箱扫描 ----------

def scan_draft_page(page) -> list:
    """
    提取当前草稿箱页面的所有商品 ID 和名称。
    返回列表: [{"id": "3832980116280050151", "name": "REDMI K90 Pro Max 二手手机"}, ...]
    """
    products = page.evaluate("""
        () => {
            const rows = document.querySelectorAll('tr.ecom-g-table-row');
            const products = [];
            rows.forEach(row => {
                const idEl = row.querySelector('.style_productId__Xsteu');
                const nameEl = row.querySelector('.style_productName__Q9d1M');
                if (idEl) {
                    const match = idEl.innerText.match(/ID:(\\d+)/);
                    if (match) {
                        products.push({
                            id: match[1],
                            name: nameEl ? nameEl.innerText.trim() : ''
                        });
                    }
                }
            });
            return products;
        }
    """)
    return products if products else []


def go_to_next_page(page) -> bool:
    """
    翻到草稿箱下一页。
    使用 ecom-g-pagination 组件（共764件商品，77页）。
    返回 True 如果成功翻到下一页，False 如果已经是最后一页或翻页失败。
    """
    try:
        # 获取当前页第一个商品ID，用于验证翻页后数据是否刷新
        current_first_id = page.evaluate("""
            () => {
                const row = document.querySelector('tr.ecom-g-table-row');
                if (!row) return '';
                const idEl = row.querySelector('.style_productId__Xsteu');
                if (!idEl) return '';
                const match = idEl.innerText.match(/ID:(\\d+)/);
                return match ? match[1] : '';
            }
        """)

        # 使用 ecom-g-pagination 组件获取页码信息
        page_info = page.evaluate("""
            () => {
                let pagination = document.querySelector('.ecom-g-pagination');
                if (!pagination) return { found: false, total_text: '', current: 0, total: 0 };
                
                // 总商品数
                let totalEl = pagination.querySelector('.ecom-g-pagination-total-text');
                let total_text = totalEl ? totalEl.innerText.trim() : '';
                
                // 当前激活页
                let activeItem = pagination.querySelector('.ecom-g-pagination-item-active');
                let current = activeItem ? parseInt(activeItem.innerText.trim()) : 0;
                
                // 最后一个页码项（总页数）
                let allItems = pagination.querySelectorAll('.ecom-g-pagination-item');
                let total_pages = 0;
                for (let item of allItems) {
                    let num = parseInt(item.innerText.trim());
                    if (!isNaN(num) && num > total_pages) {
                        total_pages = num;
                    }
                }
                
                // 下一页按钮
                let nextBtn = pagination.querySelector('.ecom-g-pagination-next');
                let next_disabled = nextBtn ? nextBtn.classList.contains('ecom-g-pagination-disabled') || nextBtn.getAttribute('aria-disabled') === 'true' : true;
                
                return {
                    found: true,
                    total_text: total_text,
                    current: current,
                    total: total_pages,
                    next_disabled: next_disabled
                };
            }
        """)

        if not page_info.get("found"):
            log("SCAN", "未找到 ecom-g-pagination 分页组件")
            return False

        current = page_info.get("current", 0)
        total = page_info.get("total", 0)
        next_disabled = page_info.get("next_disabled", True)
        total_text = page_info.get("total_text", "")

        log("SCAN", f"分页: {total_text} | 当前第 {current}/{total} 页 | 下一页按钮: {'禁用' if next_disabled else '可用'}")

        if next_disabled or (total > 0 and current >= total):
            log("SCAN", "已经是最后一页")
            return False

        # 点击下一页按钮
        clicked = page.evaluate("""
            () => {
                let pagination = document.querySelector('.ecom-g-pagination');
                if (!pagination) return false;
                let nextBtn = pagination.querySelector('.ecom-g-pagination-next');
                if (nextBtn && !nextBtn.classList.contains('ecom-g-pagination-disabled')) {
                    let link = nextBtn.querySelector('button') || nextBtn;
                    link.click();
                    return true;
                }
                return false;
            }
        """)
        
        if not clicked:
            log("SCAN", "点击下一页按钮失败")
            return False

        # 等待数据刷新：检查第一个商品ID是否变化
        data_refreshed = False
        for wait_i in range(15):
            page.wait_for_timeout(1000)
            new_first_id = page.evaluate("""
                () => {
                    const row = document.querySelector('tr.ecom-g-table-row');
                    if (!row) return '';
                    const idEl = row.querySelector('.style_productId__Xsteu');
                    if (!idEl) return '';
                    const match = idEl.innerText.match(/ID:(\\d+)/);
                    return match ? match[1] : '';
                }
            """)
            if new_first_id and new_first_id != current_first_id:
                data_refreshed = True
                log("SCAN", f"翻页成功: 数据已刷新 (ID: {current_first_id[:8]}... → {new_first_id[:8]}...)")
                break
        
        if not data_refreshed:
            log("SCAN", "翻页后数据未刷新（第一个商品ID未变化），可能已到达末尾")
            return False

        return True

    except Exception as e:
        log("SCAN", f"翻页异常: {e}")
        return False


def collect_all_draft_ids(page, max_pages: int = None) -> list:
    """
    收集草稿箱所有页的商品列表。
    返回列表: [{"id": "...", "name": "..."}, ...]

    参数:
        max_pages: 最大扫描页数，None 表示扫描全部
    """
    log("SCAN", "===== 开始扫描草稿箱 =====")
    page.goto(DRAFT_URL, wait_until="domcontentloaded", timeout=30000)
    # networkidle 可能因 WebSocket 等持续连接超时，改用 domcontentloaded + 等待表格
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        log("SCAN", "networkidle 超时，继续等待表格加载...")
    page.wait_for_timeout(3000)
    
    # 等待表格行出现（加 try/except 防止导航冲突）
    for i in range(20):
        page.wait_for_timeout(1000)
        try:
            row_count = page.evaluate("""
                () => document.querySelectorAll('tr.ecom-g-table-row').length
            """)
            if row_count > 0:
                log("SCAN", f"表格已加载: {row_count} 行 (等待 {i+1}s)")
                break
        except Exception:
            if i < 19:
                continue
            raise
    else:
        log("SCAN", "警告: 表格未加载，继续尝试扫描")
    
    page.wait_for_timeout(1000)  # 额外等待分页组件渲染

    all_products = []
    page_num = 1
    safety_limit = 200  # 安全上限（700+商品 / 10每页 = 70+页）
    # 如果传入了 max_pages 参数，则限制扫描页数
    actual_limit = min(max_pages, safety_limit) if max_pages else safety_limit

    while page_num <= actual_limit:
        # 确保表格行已出现（加 try/except 防止导航冲突）
        for wait_i in range(10):
            try:
                row_count = page.evaluate("""
                    () => document.querySelectorAll('tr.ecom-g-table-row').length
                """)
                if row_count > 0:
                    break
            except Exception:
                pass
            page.wait_for_timeout(1000)
        
        products = scan_draft_page(page)
        log("SCAN", f"第 {page_num} 页: 扫描到 {len(products)} 个商品")
        for p in products:
            log("SCAN", f"  - [{p['id']}] {p['name']}")
        all_products.extend(products)

        if not go_to_next_page(page):
            break
        page_num += 1

    # 去重：防止翻页后数据未刷新导致重复
    seen_ids = set()
    unique_products = []
    for p in all_products:
        if p["id"] not in seen_ids:
            seen_ids.add(p["id"])
            unique_products.append(p)

    if len(unique_products) < len(all_products):
        log("SCAN", f"去重后: {len(unique_products)} 个唯一商品 (原 {len(all_products)} 个)")

    log("SCAN", f"===== 扫描完成: 共 {len(unique_products)} 个草稿商品 =====")
    return unique_products


# ---------- 单商品处理 ----------

def _relocate_model_box_for_guard(page, brand_cn: str, brand_en: str) -> None:
    """终检前把型号框坐标刷新为当前页面真实位置（修复过期坐标误读品牌框/空框）。

    背景：SELECTED_MODEL_XY 在 Step5 选型号时抓取，但 Step14 切到"价格库存"页 +
    Step15 关"修改规格信息"开关后，型号框（基础信息页字段）被隐藏/位移，旧坐标已
    失效。直接用旧坐标会导致终检误读品牌框（如 Huawei/华为）或读到空占位符（请选择），
    从而把正确型号判为"型号异常"而存草稿。

    做法：复用 _pin_and_locate_model_box（优先通过"型号"标签/品牌定位，给元素打标记
    data-wb-pin-model="1"），把新鲜坐标和 selector 写回 SELECTED_MODEL_XY。定位失败
    则保留旧坐标。

    关键修复（2026-08-07）：终检**不再使用 Step5 选型号时的过期坐标**作为 hint。
    Step8(切图文信息)/Step14(切价格库存)/Step15(关规格开关) 以及图片上传时弹出的
    "2个错误问题待处理"横幅都会导致基础信息页表单布局位移，老坐标 (如 692,505) 已
    不再指向型号框。用过期坐标做 hint 会让 pin 的 hintSanity 重锚命中坐标附近的非型号
    字段，最终 model-box-label-mismatch 失败、退而读品牌框 → 误判存草稿。
    这里强制传 hint=(-1,-1)，让 pin 完全按"型号"标签/品牌值重新定位，不受布局位移影响。
    """
    # 强制 hint=(-1,-1)：终检重新定位型号框，不依赖 Step5 的过期坐标（见上方说明）。
    hint_x = -1
    hint_y = -1
    pin = _pin_and_locate_model_box(page, hint_x, hint_y, brand_cn, brand_en)
    if pin.get("ok"):
        SELECTED_MODEL_XY["x"] = pin["x"]
        SELECTED_MODEL_XY["y"] = pin["y"]
        SELECTED_MODEL_XY["cx"] = pin.get("cx", -1)
        SELECTED_MODEL_XY["cy"] = pin.get("cy", -1)
        SELECTED_MODEL_XY["selector"] = pin.get("selector", "")
        log("MODEL", f"[终检重定位] 新鲜型号框坐标: x={pin['x']}, y={pin['y']}, 中心=({pin.get('cx')},{pin.get('cy')}), method={pin.get('method')}")
    else:
        log("MODEL", f"[终检重定位] 未定位到型号框，保留过期坐标 x={SELECTED_MODEL_XY.get('x')}, y={SELECTED_MODEL_XY.get('y')}")



def guard_model_before_publish(page, expected_model: str, brand_cn: str, brand_en: str) -> tuple:
    """发布前终检：重新读取型号下拉框的实际值，确认不是 E2 / 默认值 / 空值 / 截断值。

    返回 (ok, actual_value)：
      - ok=True：实际型号与期望型号（剥离品牌前缀后）一致，可以发布；
      - ok=False：型号异常（缺值/回弹/E2/不匹配），调用方应拒绝发布并兜底保存草稿，
        避免把错误型号（如 E2）的商品发布出去。

    通过 SELECTED_MODEL_XY 精确定位型号下拉框，避免误读品牌框。
    """
    if not expected_model:
        return True, ""
    # 终检前重定位型号框：Step14 切价格库存页 + Step15 关规格开关后旧坐标已过期，
    # 先切回基础页用品牌框→型号框重新定位拿到新鲜坐标，避免误读品牌框/空框。
    # Redmi 来源名保留在型号中，平台品牌框仍按 Xiaomi/小米定位。
    guard_disp_cn, guard_disp_en = _display_brand_for_redmi(brand_cn, brand_en, expected_model)
    _relocate_model_box_for_guard(page, guard_disp_cn, guard_disp_en)
    model_x = SELECTED_MODEL_XY.get("x", -1)
    model_y = SELECTED_MODEL_XY.get("y", -1)
    # 终检探针优先用型号框"中心"(cx,cy)，避免左上角与相邻框边框重合误读（同选型号验证修复）
    model_cx = SELECTED_MODEL_XY.get("cx", -1)
    model_cy = SELECTED_MODEL_XY.get("cy", -1)
    actual = page.evaluate("""
        (params) => {
            let modelX = params.modelX;
            let modelY = params.modelY;
            let selects = document.querySelectorAll('.ecom-g-select');

            // 2026-08-07 优先用 pin 标记直接读取型号框，不受滚动/坐标漂移影响
            let pinned = document.querySelector('[data-wb-pin-model="1"]');
            if (pinned) {
                let r = pinned.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) {
                    let sel = pinned.querySelector('.ecom-g-select-selection-item');
                    let text = sel ? (sel.title || sel.innerText || '').trim() : '';
                    if (text) return text;
                    let ph = pinned.querySelector('.ecom-g-select-selection-placeholder');
                    if (ph && ph.innerText.trim() === '请选择') return '__PLACEHOLDER__';
                }
            }

            // 其次读取"目标坐标所在框"（即我们点击的型号框本身），避免误读邻近的
            // "是/否"/摄像头规格/"颜色"等下拉框 —— 这是原先"取最近框"会读错的根因。
            if (modelX >= 0 && modelY >= 0) {
                for (let s of selects) {
                    let rect = s.getBoundingClientRect();
                    let x = Math.round(rect.left), y = Math.round(rect.top);
                    if (rect.width > 0 && rect.height > 0 &&
                        x <= modelX && modelX <= x + rect.width && y <= modelY && modelY <= y + rect.height) {
                        let sel = s.querySelector('.ecom-g-select-selection-item');
                        let text = sel ? (sel.title || sel.innerText || '').trim() : '';
                        if (text) return text;            // 型号框已选中 -> 直接返回真实值
                        // 型号框在坐标处但为空：看是否仍是 placeholder（未选 / 默认值场景）
                        let phs = document.querySelectorAll('.ecom-g-select-selection-placeholder');
                        for (let p of phs) {
                            if (p.innerText.trim() === '请选择') return '__PLACEHOLDER__';
                        }
                        return '';
                    }
                }
            }

            // 兜底1.5（标签法，2026-08-07）：pin 失败(如华为联合品牌串 'Huawei/华为' 导致
            // model-box-label-mismatch) 且坐标因横幅/切页位移失效时，用"型号"标签直接定位型号框
            // 读取。标签是权威位置，不受坐标漂移与品牌误读影响，能稳定读到真实型号值。
            {
                let modelLabels = [];
                let labelEls = document.querySelectorAll('label, [class*="label"], .ecom-g-form-item-label');
                for (let el of labelEls) {
                    let t = (el.innerText || el.textContent || '').trim();
                    let n = t.replace(/[\\*\\s\\:\\：\\(\\)（）]/g, '').trim();
                    if (n === '型号' && !/cpu|处理器|芯片/i.test(t)) modelLabels.push(el);
                }
                for (let ml of modelLabels) {
                    let sel = null;
                    let forId = ml.getAttribute && ml.getAttribute('for');
                    if (forId) { let c = document.getElementById(forId); if (c) sel = c.closest('.ecom-g-select'); }
                    if (!sel) {
                        let lr = ml.getBoundingClientRect();
                        let best = null, bestDist = 1e9;
                        for (let s of selects) {
                            let r = s.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            if (r.left < lr.left - 20) continue;
                            let dx = r.left - lr.right;
                            let dy = Math.abs((r.top + r.height / 2) - (lr.top + lr.height / 2));
                            let dist = (dx < 0 ? 0 : dx) + dy * 2;
                            if (dist < bestDist) { bestDist = dist; best = s; }
                        }
                        sel = best;
                    }
                    if (sel) {
                        let itemEl = sel.querySelector('.ecom-g-select-selection-item');
                        let txt = itemEl ? (itemEl.title || itemEl.innerText || '').trim() : '';
                        if (txt) return txt;                 // 型号框已选 -> 直接返回真实值
                        return '__PLACEHOLDER__';            // 型号框存在但为空：明确判未选，避免退而误读品牌框
                    }
                }
            }

            // 兜底（坐标未知 / 未命中坐标框）：
            // 优先取"值中包含期望型号"的 select（避免误取品牌框作为型号）；
            // 否则取第一个有值的 select；都为空则看 placeholder
            let best = null, bestMatch = null;
            let expected = params.expected || '';
            for (let s of selects) {
                let sel = s.querySelector('.ecom-g-select-selection-item');
                if (sel) {
                    let text = (sel.title || sel.innerText || '').trim();
                    if (text) {
                        if (!best) best = text;
                        if (expected && text.indexOf(expected) >= 0) bestMatch = text;
                    }
                }
            }
            if (bestMatch) return bestMatch;
            if (!best) {
                let phs = document.querySelectorAll('.ecom-g-select-selection-placeholder');
                for (let p of phs) {
                    if (p.innerText.trim() === '请选择') return '__PLACEHOLDER__';
                }
                return '';
            }
            return best;
        }
    """, {"modelX": (model_cx if model_cx > 0 else model_x), "modelY": (model_cy if model_cy > 0 else model_y), "expected": expected_model})
    snap(page, "guard_read")

    if actual == "__PLACEHOLDER__":
        navigate_to_price_stock(page)
        return False, "（仍为默认'请选择'，未选型号）"

    norm_actual = _strip_brand_tokens(actual, brand_cn, brand_en)
    norm_expected = _strip_brand_tokens(expected_model, brand_cn, brand_en)

    # 关键兜底：读到空字符串、仅品牌名、或明显不是型号值时，必须判失败，
    # 防止误读品牌框/空框导致把错误型号发布出去。
    if not norm_actual:
        navigate_to_price_stock(page)
        return False, actual
    if norm_actual.lower() == (brand_cn or '').lower() or norm_actual.lower() == (brand_en or '').lower():
        navigate_to_price_stock(page)
        return False, actual

    # 完全一致，或实际值包含期望型号（含升级字段等），视为通过
    ok = False
    if norm_actual == norm_expected or norm_expected in norm_actual or norm_actual in norm_expected:
        # 仍要拒绝"明显是默认值"的情况：E2 / E + 数字 这类疑似平台默认占位
        if re.match(r'^[Ee]\d+$', norm_actual):
            ok = False
        else:
            ok = True

    # 强制映射放行：expected_model 在 MODEL_EXACT_OVERRIDE 中时，编辑页下拉选项带全大写
    # 品牌前缀（如 'HUAWEI Pura 80 Pro'），单纯靠剥离品牌 token 可能漏匹配；这里直接按
    # 强制映射表的精确选项放行。归一化查表兼容 带前缀 / 带括号 / 无括号 三种写法。
    # 此放行只作用于"已正确读到型号框"的情况；若终检仍误读到品牌框等非型号值，需另修坐标过期问题。
    _ov_key, override_val = _resolve_model_override(expected_model, brand_cn, brand_en)
    override_exact_match = bool(
        _ov_key is not None and actual.strip() == override_val.strip()
    )
    if override_exact_match:
        ok = True
    if not ok:
        if _ov_key is not None:
            override_norm = _strip_brand_tokens(override_val, brand_cn, brand_en)
            if norm_expected in override_norm or override_norm in norm_expected:
                ok = True

    # 终检空格/大小写容错：平台编辑页下拉常吞掉规范命名里的空格/缩进（如 'Pura 90 Pro'
    # 显示为 'Pura90 Pro'），或大小写不一致。这类"内容一致仅排版不同"应放行，避免把正确
    # 商品误判拦截。仅折叠空格+忽略大小写，token 顺序/差异完全保留，不会误放不同型号。
    if not ok:
        if _norm_model_loose(norm_actual) == _norm_model_loose(norm_expected):
            ok = True
            log("VERIFY", f"终检空格/大小写容错放行: 页面型号='{actual}' 与期望='{expected_model}' 内容一致(仅排版差异)")

    # 关键新增：目标型号无后缀字段，实际值却带后缀字段 -> 拒绝
    # 防止 OPPO 等机型被平台自动"升级"（如 Find X7 -> Find X7 Pro）。
    # 原有"带后缀不降级"逻辑保持不变；此处仅拦截"无后缀变有后缀"。
    if (ok and not override_exact_match and
            not _model_has_suffix_field(norm_expected) and _model_has_suffix_field(norm_actual)):
        log("VERIFY", f"目标型号 '{expected_model}' 无后缀，但实际值 '{actual}' 带后缀，拒绝发布")
        ok = False

    navigate_to_price_stock(page)
    return (True, actual) if ok else (False, actual)


def refix_oppo_model_after_spec_switch(page, parsed: dict, category: str = "") -> bool:
    """OPPO 机型专用：Step15 关闭规格开关后型号常被重置为 E2/空，
    切回基础信息页重新做一次型号选择+纯型号改写，确保型号在发布前最后一刻被钉死。

    注意：此函数执行后页面停留在基础信息页，不再切回价格库存页，避免切走又丢失。
    """
    brand_cn = parsed.get("brand_cn", "")
    brand_normalized = parsed.get("brand_normalized", "")
    brand_en = brand_normalized.split("/")[0] if brand_normalized else ""
    model = parsed.get("model", "")
    if not _is_oppo_brand(brand_cn, brand_en) or not model:
        return True
    try:
        log("MODEL", "OPPO 规格开关关闭后：切回基础信息页重新固定型号")
        page.evaluate("""
            () => {
                let tabs = document.querySelectorAll('[role=tab]');
                for (let t of tabs) {
                    let txt = t.innerText || '';
                    if (txt.includes('商品信息') || txt.includes('基础信息') || txt.includes('基础')) {
                        t.click(); break;
                    }
                }
            }
        """)
        time.sleep(1.5)
        # 重新走一遍品牌+型号流程（品牌已 OK，主要重新选型号）
        result = ensure_brand_model(page, parsed, category)
        ok = result.get("all_ok", False)
        log("MODEL", f"OPPO 重新固定型号结果: all_ok={ok}, model_value={result.get('model_value','')}")
        return ok
    except Exception as e:
        log("MODEL", f"OPPO 重新固定型号异常: {e}")
        return False


def process_single_product(page, product_id: str, product_name: str = "") -> tuple:
    """
    处理单个商品，从导航到编辑页到保存草稿的完整流程。

    参数:
        page: Playwright Page 对象
        product_id: 商品 ID（从草稿箱扫描获取）
        product_name: 商品名称（用于日志记录）

    返回:
        (success: bool, duration_seconds: float)
    """
    start_time = time.time()
    # 每个商品必须从空错误状态开始。旧逻辑会把上一个商品的“商详图不足13张”
    # 沿用到后续的缺主图/型号失败商品，造成面板失败原因严重误报。
    clear_last_publish_error()
    log("PROCESS", f"===== 开始处理商品: {product_name} (ID:{product_id}) =====")

    # 构建编辑页路径（通用化，不再硬编码）
    edit_path = f"/ffa/g/create?product_id={product_id}&entrance=draft"
    log("PROCESS", f"编辑页路径: {edit_path}")

    try:
        # ========== Step 1: 导航到编辑页 ==========
        # 说明：原先先尝试 JS 重定向 (window.location.href) 再轮询等待 URL 变化，
        # 实测该重定向在 100% 情况下都不生效，只会白白等待约 15s 后才回退到 page.goto。
        # 既然 page.goto 才是真正生效的导航方式，这里直接走 page.goto，省去无效等待。
        log("PROCESS", "Step 1: 直接跳转到编辑页...")
        page.goto(
            f"https://fxg.jinritemai.com{edit_path}",
            wait_until="domcontentloaded",
            timeout=30000
        )

        # 等待编辑页加载完成
        log("PROCESS", "等待编辑页渲染...")
        for i in range(20):
            time.sleep(1)
            has_input = page.evaluate("""
                () => {
                    let inputs = document.querySelectorAll('input');
                    for (let i of inputs) {
                        if (i.value && i.value.length > 0) return true;
                    }
                    return false;
                }
            """)
            if has_input:
                log("PROCESS", f"页面 input 已渲染 (等待 {i+1}s)")
                break
            if i % 3 == 2:
                log("PROCESS", f"等待页面渲染... {i+1}s")
        else:
            log("PROCESS", "警告: 页面可能未完全加载，继续尝试")

        time.sleep(2)
        log("PROCESS", f"编辑页URL: {page.url}")

        # ========== Step 2: 关闭弹窗 ==========
        dismiss_popup(page, label="页面加载后")

        # 抖店每次重新打开编辑页都可能把“修改规格信息”恢复为开。
        # 本浏览器链路只处理二手手机，先关闭并核实，再开始改标题/属性/图片。
        spec_ready = ensure_spec_switch_closed(page, "进入编辑页后")
        switched_to_price_stock = False
        if not spec_ready and navigate_to_price_stock(page):
            # 某些页面按需挂载价格库存区；仅在尚未编辑任何字段时允许切换。
            switched_to_price_stock = True
            spec_ready = ensure_spec_switch_closed(page, "进入价格库存后")
        if not spec_ready:
            reason = "进入编辑页后无法确认修改规格信息开关已关闭，已停止编辑"
            set_last_publish_error(reason)
            log("PROCESS", f"❌ {reason}")
            return False, time.time() - start_time
        if switched_to_price_stock:
            page.evaluate("""() => {
                for (const tab of document.querySelectorAll('[role=tab]')) {
                    const label = tab.innerText || '';
                    if (label.includes('商品信息') || label.includes('基础信息')) {
                        tab.click();
                        break;
                    }
                }
            }""")
            if not ensure_spec_switch_closed(page, "返回商品信息后"):
                reason = "返回商品信息后规格开关重新打开或无法确认，已停止编辑"
                set_last_publish_error(reason)
                log("PROCESS", f"❌ {reason}")
                return False, time.time() - start_time
        page.evaluate("window.scrollTo(0, 0)")

        # ========== Step 3: 读取商品类目 ==========
        category = read_category(page)

        # ========== Step 4: 读取标题并规范化 ==========
        parsed = None
        for attempt in range(3):
            parsed = read_and_normalize_title(page, category)
            if parsed.get("brand_cn") or parsed.get("raw"):
                break
            log("PROCESS", f"标题为空，等待重试 ({attempt+1}/3)...")
            time.sleep(3)

        if not parsed or not parsed.get("raw"):
            raise Exception("多次尝试后标题仍为空，页面可能未正确加载")

        # 采用清洗标题后重新识别的类目（若平台已纠正为二手手机），避免沿用被截断/误判前的类目；
        # 兜底：本脚本所有店铺均为二手手机店铺，若类目未含“二手”，强制矫正为“二手手机”，
        # 防止平台误判成非二手手机类目导致发布崩。
        re_cat = parsed.get("category", "") or ""
        if re_cat and re_cat != category:
            log("CATEGORY", f"采用清洗后类目: '{re_cat}' (原='{category}')")
            category = re_cat
        if "二手" not in category:
            log("CATEGORY", f"⚠ 类目'{category}'未含'二手'，强制矫正为'二手手机'以保发布")
            category = "二手手机"

        # 关键修复：智能选择更完整的型号
        # 草稿箱列表页标题可能被平台截断（如 X50i → X50），但编辑页输入框通常是完整的
        # 同时编辑页标题也可能被平台修改，所以需要智能比较两者
        log("PROCESS", f"草稿箱标题: {product_name}")
        draft_parsed = parse_title(product_name)
        edit_model = parsed.get("model", "")
        draft_model = draft_parsed.get("model", "")
        
        # 选择更完整的型号：
        # 1. 如果编辑页型号包含草稿箱型号且更长（如 X50i vs X50），用编辑页
        # 2. 如果草稿箱型号包含编辑页型号且更长（如 X60 Pro vs X60），用草稿箱
        # 3. 如果编辑页型号明显更长（差2字符以上），用编辑页（防止截断）
        # 4. 否则用草稿箱（遵循原有逻辑）
        final_model = draft_model  # 默认用草稿箱
        if edit_model and draft_model:
            if edit_model.startswith(draft_model) and len(edit_model) > len(draft_model):
                final_model = edit_model
                log("PROCESS", f"编辑页型号更完整: '{edit_model}' > '{draft_model}'")
            elif draft_model.startswith(edit_model) and len(draft_model) > len(edit_model):
                final_model = draft_model
                log("PROCESS", f"草稿箱型号更完整: '{draft_model}' > '{edit_model}'")
            elif len(edit_model) > len(draft_model) + 2:
                final_model = edit_model
                log("PROCESS", f"编辑页型号明显更长: '{edit_model}' vs '{draft_model}'")
            else:
                log("PROCESS", f"使用草稿箱型号: '{draft_model}' (编辑页: '{edit_model}')")
        elif edit_model and not draft_model:
            final_model = edit_model
            log("PROCESS", f"草稿箱未解析到型号，使用编辑页: '{edit_model}'")
        elif draft_model:
            log("PROCESS", f"使用草稿箱型号: '{draft_model}'")
        
        if final_model:
            parsed["title_model"] = final_model
            parsed["model"] = final_model

        # 编辑页的可编辑标题偶尔只返回品牌词（例如 Redmi），而草稿列表
        # 仍保留完整的“Redmi Turbo 3 二手手机”。前面的通用清洗若直接对这种
        # 截断值追加类目词，会把草稿保存成“Redmi 二手手机”并丢失型号。
        # 当草稿型号明显比编辑页完整时，用草稿列表标题修复可编辑区；
        # 仍经过同一套违禁词/成色清洗，不会把脏标题写回去。
        if final_model and draft_model and edit_model and len(draft_model) > len(edit_model):
            current_editable = page.evaluate("""
                () => {
                    let i = document.getElementById('pg-title-input');
                    return i ? (i.value || '') : '';
                }
            """) or ""
            final_key = _norm_model_loose(final_model)
            current_key = _norm_model_loose(current_editable)
            if final_key and final_key not in current_key:
                repaired_title, _repair_actions = _clean_editable_title(product_name)
                if repaired_title and _write_title_value(page, repaired_title):
                    log("TITLE", f"草稿列表型号更完整，已修复可编辑标题: '{current_editable}' -> '{repaired_title}'")
                    parsed["title_repaired_from_draft"] = True
                else:
                    log("TITLE", f"⚠ 草稿完整标题写回失败，禁止依赖截断标题: '{current_editable}'")

        standard_model, standard_model_error = resolve_standard_model(parsed)
        if standard_model:
            old_model = parsed.get("model", "")
            parsed["source_brand"] = standard_model.get("brand", "")
            parsed["source_model"] = standard_model.get("model", "")
            parsed["model"] = standard_model["model"]
            parsed["model_source"] = "excel"
            log("MODEL", f"Excel 标准型号命中: '{old_model}' -> '{parsed['model']}' (品牌={parsed['source_brand']})")
        else:
            log("MODEL", f"⚠ {standard_model_error}，不做模糊替换")

        platform_model, platform_model_error = resolve_doudian_export_model(parsed)
        if platform_model:
            old_model = parsed.get("model", "")
            parsed["model"] = platform_model["model"]
            parsed["model_source"] = "doudian_attribute_detail"
            log("MODEL", f"抖店属性型号库命中: '{old_model}' -> '{parsed['model']}' (品牌={platform_model.get('brand', '')})")
        else:
            log("MODEL", f"⚠ {platform_model_error}，继续使用当前精确型号")

        official_model, api_model_error = resolve_official_model_via_api(parsed)
        if official_model:
            old_model = parsed.get("model", "")
            parsed["model"] = official_model["name"]
            parsed["official_model_value"] = official_model["value"]
            log("MODEL", f"API 官方型号命中: '{old_model}' -> '{official_model['name']}' (value={official_model['value']})")
        else:
            log("MODEL", f"⚠ {api_model_error}，继续使用标题型号并启用严格终检")
        
        # 品牌同样智能选择：优先用草稿箱，但如果编辑页解析出了品牌而草稿箱没有，用编辑页
        if draft_parsed.get("brand_cn"):
            parsed["brand_cn"] = draft_parsed["brand_cn"]
            parsed["brand_normalized"] = draft_parsed["brand_normalized"]
        elif parsed.get("brand_cn") and not draft_parsed.get("brand_cn"):
            log("PROCESS", f"草稿箱未解析到品牌，使用编辑页: '{parsed['brand_cn']}'")

        # 检查品牌/型号是否为空（核心校验）
        if not parsed.get("brand_cn") or not parsed.get("model"):
            log("PROCESS", f"⚠ 品牌或型号解析失败: brand_cn={parsed.get('brand_cn')}, model={parsed.get('model')}")
            # 继续执行，让后续步骤自行处理

        # ========== Step 4.5: 检查本地主图是否存在 ==========
        # 型号专测/标题修复专测都不上传/删除图片，不应被 macOS 生产图片路径阻塞；
        # 这也让测试能在 Windows 维护机安全运行。正式流程仍保持原有图片闸门。
        if not (os.environ.get("WB_TEST_MODEL_ONLY") or os.environ.get("WB_TEST_TITLE_REPAIR_ONLY")):
            main_image_path = get_main_image_path(parsed)
            if not os.path.exists(main_image_path):
                reason = f"本地主图不存在，已转人工: {main_image_path}"
                set_last_publish_error(reason)
                log("PROCESS", f"⚠ {reason}")
                mark_product_skipped_no_image(product_id, product_name, main_image_path)
                return False, 0.0
            log("PROCESS", f"✓ 本地主图存在: {main_image_path}")
        else:
            log("PROCESS", "🔧 型号专测模式：跳过本地图片存在性检查")

        # ========== Step 5: 验证品牌 + 选择型号 ==========
        brand_model_result = ensure_brand_model(page, parsed, category)
        brand_model_ok = brand_model_result.get("all_ok", False)

        # ========== 调试开关：WB_TEST_MODEL_ONLY 只测"品牌+型号选择"第一步 ==========
        # 用户要求逐步排查 OPPO 选型号问题时，临时禁用后续所有步骤（删图/上传/价格库存/
        # 发布/终检），避免流程跑到后面崩溃或互相干扰。设环境变量即可：
        #   WB_TEST_MODEL_ONLY=1 python test_doudian_batch.py ...
        if os.environ.get("WB_TEST_MODEL_ONLY"):
            log("PROCESS", "🔧 WB_TEST_MODEL_ONLY 已启用：品牌/型号选择步骤完成，跳过后续步骤用于逐步排查")
            try:
                snap(page, "test_model_only")
                final_model_val = page.evaluate("""
                    () => {
                        let pinned = document.querySelector('[data-wb-pin-model="1"]');
                        if (pinned) {
                            let item = pinned.querySelector('.ecom-g-select-selection-item');
                            return item ? (item.title || item.innerText || '').trim() : '(空)';
                        }
                        return '(未定位型号框)';
                    }
                """)
                log("PROCESS", f"🔧 [只测第一步] 型号框最终值='{final_model_val}' | brand_ok={brand_model_result.get('brand_ok')} model_ok={brand_model_result.get('model_ok')}")
            except Exception as e:
                log("PROCESS", f"🔧 [只测第一步] 读取型号框最终值异常: {e}")
            log("PROCESS", f"===== 处理完成(仅测试型号) [商品: {product_name}] =====")
            return brand_model_ok, time.time() - start_time

        # 品牌/型号是所有后续修改的安全闸门。品牌已正确但型号列表无匹配时，
        # 视为疑似平台尚未收录的新型号；品牌都未能切换时则属于自动化操作失败。
        # 两种情况都立即返回人工队列，绝不进入删图、上传、保存或发布步骤。
        if not brand_model_ok:
            brand_ok = bool(brand_model_result.get("brand_ok"))
            model_ok = bool(brand_model_result.get("model_ok"))
            if brand_ok and not model_ok:
                reason = (
                    f"型号选择失败：疑似新型号/平台未收录；品牌="
                    f"{brand_model_result.get('brand_value') or parsed.get('brand_normalized')}，"
                    f"目标型号={parsed.get('model')}，已转人工处理"
                )
            else:
                reason = (
                    f"品牌/型号不完整：自动化操作失败；目标品牌="
                    f"{parsed.get('brand_normalized') or parsed.get('brand_cn')}，"
                    f"目标型号={parsed.get('model')}，已转人工处理"
                )
            set_last_publish_error(reason)
            log("PROCESS", f"❌ {reason}")
            return False, time.time() - start_time

        # 切换品牌会触发抖店表单联动，某些 Redmi 草稿会把可编辑标题
        # 重置成“Redmi/红米”，覆盖我们在切换前恢复的“Redmi K60 Pro 二手手机”。
        # 因此品牌+型号选择成功后再做一次标题完整性闸门，修复后仍不包含
        # 期望型号则立即转人工，禁止删图和保存。
        expected_title_model = parsed.get("title_model") or parsed.get("model", "")
        if expected_title_model:
            title_after_select = page.evaluate("""
                () => {
                    let i = document.getElementById('pg-title-input');
                    return i ? (i.value || '') : '';
                }
            """) or ""
            expected_title_key = _norm_model_loose(
                _strip_brand_tokens(expected_title_model, parsed.get("brand_cn", ""),
                                    parsed.get("brand_normalized", "").split("/")[0])
            )
            current_title_key = _norm_model_loose(title_after_select)
            if expected_title_key and expected_title_key not in current_title_key:
                repaired_title, _ = _clean_editable_title(product_name)
                if repaired_title and _write_title_value(page, repaired_title):
                    time.sleep(1.2)
                    title_after_select = page.evaluate("""
                        () => {
                            let i = document.getElementById('pg-title-input');
                            return i ? (i.value || '') : '';
                        }
                    """) or ""
                    current_title_key = _norm_model_loose(title_after_select)
                    log("TITLE", f"品牌/型号联动后恢复完整标题: '{repaired_title}'")
                if expected_title_key not in current_title_key:
                    reason = f"品牌切换后标题丢失型号'{expected_title_model}'，已转人工"
                    set_last_publish_error(reason)
                    log("PROCESS", f"❌ {reason}，禁止删图和保存")
                    return False, time.time() - start_time

        # ========== 调试开关：WB_TEST_TITLE_REPAIR_ONLY 只测到"标题联动修复
        # 检查"这一步，用于验证品牌切换导致标题被清空后的自动修复是否生效，
        # 不触碰删图/上传/保存。设环境变量即可：
        #   WB_TEST_TITLE_REPAIR_ONLY=1 python test_doudian_batch.py ...
        if os.environ.get("WB_TEST_TITLE_REPAIR_ONLY"):
            final_title = page.evaluate("""
                () => {
                    let i = document.getElementById('pg-title-input');
                    return i ? (i.value || '') : '';
                }
            """) or ""
            log("PROCESS", f"🔧 WB_TEST_TITLE_REPAIR_ONLY 已启用：标题联动修复检查完成，最终标题='{final_title}'")
            log("PROCESS", f"===== 处理完成(仅测试标题修复) [商品: {product_name}] =====")
            return True, time.time() - start_time

        # ========== Step 6: 回看检查 ==========
        verify_ok = final_check(
            page, parsed, category,
            brand_value=brand_model_result.get("brand_value", ""),
            model_value=brand_model_result.get("model_value", "")
        )
        if not verify_ok:
            reason = "品牌/型号/标题联合回看未通过，已阻止删图、保存和发布"
            set_last_publish_error(reason)
            log("PROCESS", f"❌ {reason}")
            return False, time.time() - start_time

        # ========== Step 7: 选择'其他属性-是否原装'为'是' ==========
        original_ok = select_is_original(page)

        if os.environ.get("WB_PROBE_REQUIRED_MULTISELECT_ONLY"):
            findings = probe_required_multiselects(page)
            log("PROCESS", "🔎 WB_PROBE_REQUIRED_MULTISELECT_ONLY：只读探查完成，候选数=%s；未删图、未保存、未发布" % len(findings))
            set_last_publish_error("只读必填多选探查完成")
            return False, time.time() - start_time

        # ========== Step 8: 切换到图文信息标签页 ==========
        log("PROCESS", "Step 8: 切换到图文信息标签页...")
        page.evaluate("""
            () => {
                let tabs = document.querySelectorAll('[role=tab]');
                for (let t of tabs) {
                    if (t.innerText.includes('图文信息')) {
                        t.click();
                        return 'clicked';
                    }
                }
                return 'not found';
            }
        """)
        time.sleep(2)
        log("PROCESS", "已切换到图文信息标签页")

        # ========== Step 9: 关闭切换标签后弹窗 ==========
        dismiss_popup(page, label="切换图文信息后")

        # ========== Step 10: 检查商详图是否已有目标数量 ==========
        # 注意：必须在删除主图之前检查，因为 delete_all_images 可能会删除所有图片
        detail_count_before = page.evaluate("""
            () => document.querySelectorAll('.styles_imgWrapper__dqiHn').length
        """)
        log("PROCESS", f"检查商详图数量（删除前）: {detail_count_before}")
        detail_paths = get_detail_image_paths(parsed)
        try:
            detail_version = calculate_detail_image_version(detail_paths)
        except (ValueError, FileNotFoundError, OSError) as e:
            reason = f"本地商详图片校验失败，拒绝删除旧图和发布: {e}"
            set_last_publish_error(reason)
            log("PROCESS", f"❌ {reason}")
            return False, time.time() - start_time

        detail_upload_ok = True
        detail_skipped = False
        refresh_detail, refresh_reason = should_refresh_detail_images(
            CURRENT_SHOP_ID, product_id, detail_count_before, detail_version
        )
        if not refresh_detail:
            log("PROCESS", f"✅ 商详图无需更新：{refresh_reason}")
            detail_skipped = True
        else:
            log("PROCESS", f"🔄 商详图需要重新上传：{refresh_reason}")

        # ========== Step 11: 删除所有原有图片 ==========
        # 防止误删除机制：检查页面是否包含"爱回收"文字，
        # 有则说明是爱回收已上传的商品，跳过删除。
        # 注意：不检查"拍拍"因为店铺名固定包含"拍拍"，始终为真。
        main_count = page.evaluate("""
            () => document.querySelectorAll('.index-module_imgWrapper__xOFF7').length
        """)
        log("PROCESS", f"主图数量: {main_count}")

        # 删除原有主图/白底图/3:4 等主图区所有图片，便于后续重新上传。
        # 说明：原先这里有一个"爱回收"防误删 guard（document.body.innerText 含"爱回收"就跳过删除），
        # 但店铺名本身就含"爱回收"（如"爱回收严选二手数码"），导致对所有爱回收店铺恒为真、
        # 永远跳过删除，旧图残留。本批流程本就是要重传图片，故移除该 guard，统一删除后重传。
        log("PROCESS", "执行删除原有主图区图片（重传前清理）")
        delete_all_images(page)

        # ========== Step 12: 处理商详图（删除+上传，或跳过） ==========
        if detail_skipped:
            log("PROCESS", f"商详图已跳过（已有{DETAIL_IMAGE_COUNT}张）")
        else:
            log("PROCESS", "开始删除原有商详图...")
            delete_detail_images(page)
            log("PROCESS", f"开始上传新的商详图片（{DETAIL_IMAGE_COUNT}张）...")
            detail_upload_ok = upload_detail_images(page, parsed)
            if not detail_upload_ok:
                reason = f"商详图片上传后未严格达到{DETAIL_IMAGE_COUNT}张，已阻止发布"
                set_last_publish_error(reason)
                log("PROCESS", f"❌ {reason}")
                return False, time.time() - start_time

        # ========== Step 13: 上传主图 + 白底图 ==========
        log("PROCESS", "Step 13: 上传主图 + 白底图...")
        upload_success = upload_images(page, parsed)
        if upload_success:
            log("PROCESS", "✅ 主图 + 白底图上传完成")
        else:
            reason = "主图/白底图上传失败，已阻止保存和发布"
            set_last_publish_error(reason)
            log("PROCESS", f"❌ {reason}")
            return False, time.time() - start_time

        # 验证上传结果
        upload_verify_ok = verify_upload(page, parsed)
        if not upload_verify_ok:
            # 先算差异，再只补缺失辅助图。不得重新探测/上传已确认的主图。
            log("VERIFY_UPLOAD", "首次落图不足，按本次会话差异仅补传缺失辅助图后重新验证...")
            retry_upload_ok = retry_missing_aux_images(page, parsed)
            upload_verify_ok = retry_upload_ok and verify_upload(page, parsed)
        if not upload_verify_ok:
            reason = "必填主图未落图或图片数量不足，重传后仍未通过精确校验，已阻止保存和发布"
            set_last_publish_error(reason)
            log("PROCESS", f"❌ {reason}")
            return False, time.time() - start_time

        # ========== Step 14: 切换到价格库存标签页 ==========
        price_stock_ok = navigate_to_price_stock(page)

        # ========== Step 15: 关闭修改规格信息开关 ==========
        switch_closed = False
        if price_stock_ok:
            switch_closed = close_spec_switch(page)

        # 规格开关明确仍为“开”时绝不能继续点击发布，否则平台必然报
        # “当前规格层级超过上限”。未知/页面无开关不做误拦截。
        if price_stock_ok and not switch_closed:
            spec_probe = _probe_spec_switch_state(page)
            if spec_probe.get("state") == "开":
                log("STEP9", "发布前复核发现规格开关仍为开，再执行一次完整关闭流程")
                switch_closed = close_spec_switch(page)
                spec_probe = _probe_spec_switch_state(page)
            if spec_probe.get("state") == "开":
                reason = "修改规格信息开关多次关闭失败，已阻止发布以避免规格层级超限"
                set_last_publish_error(reason)
                log("PROCESS", f"❌ {reason}")
                save_ok = save_draft(page)
                log("PROCESS", f"规格开关拦截后保存草稿: {'成功' if save_ok else '失败'}")
                return False, time.time() - start_time

        # OPPO 机型：规格开关关闭后型号常被重置，必须在发布前最后一刻重新固定型号。
        # 此步骤切回基础信息页重新选择型号并改写为纯型号，完成后页面停留在基础信息页，
        # 避免切回价格库存页又触发重置。
        if price_stock_ok and switch_closed and _is_oppo_brand(parsed.get("brand_cn", ""),
                                                               parsed.get("brand_normalized", "").split("/")[0] if parsed.get("brand_normalized") else ""):
            refix_oppo_model_after_spec_switch(page, parsed, category)

        # ========== Step 15.5: 发布前型号终检（防止 E2 / 默认值 / 未选型号被发布） ==========
        expected_model = parsed.get("model", "")
        guard_ok, guard_val = guard_model_before_publish(page, expected_model,
                                                         parsed.get("brand_cn", ""),
                                                         parsed.get("brand_normalized", "").split("/")[0] if parsed.get("brand_normalized") else "")
        if not guard_ok:
            log("PROCESS", f"⚠ 发布前型号终检未通过: 页面型号='{guard_val}' 期望='{expected_model}'，拒绝发布，兜底保存草稿")
            publish_ok = False
            save_ok = save_draft(page)
            log("PROCESS", f"兜底保存草稿（型号终检失败）: {'成功' if save_ok else '失败'}")
            if save_ok and detail_upload_ok:
                try:
                    save_detail_image_version(CURRENT_SHOP_ID, product_id, detail_version)
                    log("DETAIL_IMG", f"✅ 草稿已保存，记录商详图版本: {detail_version[:12]}...")
                except Exception as e:
                    log("DETAIL_IMG", f"⚠ 商详图版本记录失败，下次将安全地重新上传: {e}")
            # 流程结束汇总
            duration = time.time() - start_time
            log("PROCESS", "=" * 50)
            log("PROCESS", f"商品处理完毕(型号终检拦截): {product_name} (ID:{product_id})")
            log("PROCESS", f"价格库存: {'已切换' if price_stock_ok else '未切换'} | 规格开关: {'已关闭' if switch_closed else '未关闭/无需'} | 发布商品: 拦截(型号异常) | 保存草稿: {'成功' if save_ok else '失败'}")
            return False, duration
        else:
            log("PROCESS", f"✓ 发布前型号终检通过: 页面型号='{guard_val}' 期望='{expected_model}'")

        # ========== Step 16: 仅保存草稿测试 / 正式发布 ==========
        save_only = str(os.environ.get("DP_SAVE_DRAFT_ONLY", "")).strip().lower() in (
            "1", "true", "yes", "on"
        )
        if save_only:
            log("PROCESS", "仅保存草稿模式已开启：跳过发布商品")
            save_ok = save_draft(page)
            if not save_ok:
                set_last_publish_error("保存草稿失败：未获得成功提示、草稿页跳转或保存接口成功响应")
            if save_ok and detail_upload_ok:
                try:
                    save_detail_image_version(CURRENT_SHOP_ID, product_id, detail_version)
                    log("DETAIL_IMG", f"✅ 草稿已保存，记录商详图版本: {detail_version[:12]}...")
                except Exception as e:
                    log("DETAIL_IMG", f"⚠ 商详图版本记录失败，下次将安全地重新上传: {e}")
            duration = time.time() - start_time
            log("PROCESS", "=" * 50)
            log("PROCESS", f"商品处理完毕(仅保存草稿): {product_name} (ID:{product_id})")
            log("PROCESS", f"型号终检: 通过 | 图片处理: {'成功' if upload_success else '失败'} | 发布商品: 已跳过 | 保存草稿: {'成功' if save_ok else '失败'}")
            return bool(save_ok), duration

        # 正式模式：发布商品（失败则兜底保存草稿）
        publish_ok = publish_product(page, parsed.get("shop_id", ""))
        save_ok = False
        if not publish_ok:
            err = get_last_publish_error() or "发布商品失败"
            log("PROCESS", f"⚠ 发布失败，兜底保存草稿: {err}")
            save_ok = save_draft(page)
            log("PROCESS", f"兜底保存草稿: {'成功' if save_ok else '失败'}")
            if save_ok and detail_upload_ok:
                try:
                    save_detail_image_version(CURRENT_SHOP_ID, product_id, detail_version)
                    log("DETAIL_IMG", f"✅ 草稿已保存，记录商详图版本: {detail_version[:12]}...")
                except Exception as e:
                    log("DETAIL_IMG", f"⚠ 商详图版本记录失败，下次将安全地重新上传: {e}")

        # ========== 流程结束汇总 ==========
        duration = time.time() - start_time
        log("PROCESS", "=" * 50)
        log("PROCESS", f"商品处理完毕: {product_name} (ID:{product_id})")
        log("PROCESS", f"耗时: {duration:.1f}s")
        log("PROCESS", f"品牌/型号: {'通过' if brand_model_ok else '未完全通过'} | 回看检查: {'通过' if verify_ok else '有问题'} | 是否原装: {'已选是' if original_ok else '未选/失败'}")
        log("PROCESS", f"图片删除: 已完成 | 商详上传: {'成功' if detail_upload_ok else '失败'} | 主图上传: {'成功' if upload_success else '失败'} | 上传验证: {'通过' if upload_verify_ok else '未通过'}")
        log("PROCESS", f"价格库存: {'已切换' if price_stock_ok else '未切换'} | 规格开关: {'已关闭' if switch_closed else '未关闭/无需'} | 发布商品: {'成功' if publish_ok else '失败'} | 保存草稿: {'成功' if save_ok else '未执行/失败'}")
        log("PROCESS", "=" * 50)

        if publish_ok:
            try:
                save_detail_image_version(CURRENT_SHOP_ID, product_id, detail_version)
                log("DETAIL_IMG", f"✅ 已记录商详图版本: {detail_version[:12]}...")
            except Exception as e:
                log("DETAIL_IMG", f"⚠ 商品已发布，但商详图版本记录失败，下次将安全地重新上传: {e}")
            clear_last_publish_error()
            return True, duration
        else:
            # 即使兜底保存草稿成功，本次也算上传失败
            return False, duration

    except Exception as e:
        duration = time.time() - start_time
        set_last_publish_error(f"处理异常: {e}")
        log("ERROR", f"处理商品 {product_id} 异常: {e}")
        import traceback
        traceback.print_exc()
        return False, duration


# ---------- 批量循环主函数 ----------

def run_batch(page, test_mode: bool = False, test_n: int = 1) -> None:
    """
    批量处理草稿箱中所有未完成的商品。
    主流程: 扫描 → 过滤已处理 → 逐个处理 → 记录日志 → 循环

    参数:
        page: Playwright Page 对象
        test_mode: True 时仅处理少量商品（用于验证图片上传功能）
        test_n: 测试模式下处理的商品数量
    """
    mode_label = f"测试模式（仅{test_n}个）" if test_mode else "批量模式"
    log("BATCH", f"===== 批量处理开始 [{mode_label}] =====")

    # 1. 收集所有草稿 ID（测试模式只扫描前5页，快速找到可测试商品）
    all_products = collect_all_draft_ids(page, max_pages=5 if test_mode else None)
    if not all_products:
        log("BATCH", "草稿箱中没有找到商品，结束")
        return

    # 2. 加载已处理日志
    log_data = load_processed_log()
    processed = log_data.get("products", {})

    # 3. 过滤: 跳过 status == "done" 或 "skipped_no_image" 的
    from processed_status_policy import should_skip_in_normal_batch
    remaining = [
        p for p in all_products
        if p["id"] not in processed
        or not should_skip_in_normal_batch(processed[p["id"]].get("status", ""))
    ]

    total = len(all_products)
    done_count = total - len(remaining)
    log("BATCH", f"总草稿: {total} | 已完成: {done_count} | 待处理: {len(remaining)}")

    if not remaining:
        log("BATCH", "所有商品已处理完毕，无需操作")
        return

    # 测试模式：找到前 test_n 个有本地主图的商品来测试
    if test_mode:
        log("BATCH", f"🔧 测试模式：查找前 {test_n} 个有本地主图的商品...")
        test_products = []
        for p in remaining:
            if len(test_products) >= test_n:
                break
            # 快速解析标题检查主图是否存在
            test_parsed = parse_title(p["name"])
            test_main_path = get_main_image_path(test_parsed)
            if os.path.isfile(test_main_path):
                test_products.append(p)
                log("BATCH", f"  ✅ 找到有主图的商品: {p['name']} (主图存在)")
            else:
                log("BATCH", f"  ⏭ 跳过(无主图): {p['name']}")

        if test_products:
            remaining = test_products
        else:
            log("BATCH", "❌ 所有待处理商品都没有本地主图，无法测试上传")
            return

    # 4. 逐个处理
    success_count = 0
    fail_count = 0

    for i, product in enumerate(remaining):
        log("BATCH", f"\n{'='*60}")
        log("BATCH", f"[{i+1}/{len(remaining)}] {product['name']} (ID:{product['id']})")
        log("BATCH", f"{'='*60}")

        success, duration = process_single_product(page, product["id"], product["name"])

        if success:
            save_only = str(os.environ.get("DP_SAVE_DRAFT_ONLY", "")).strip().lower() in (
                "1", "true", "yes", "on"
            )
            mark_product_done(product["id"], product["name"], duration, save_only=save_only)
            success_count += 1
            log("BATCH", f"✅ 完成 ({duration:.1f}s)")
        else:
            err_reason = get_last_publish_error() or "处理异常"
            if err_reason.startswith("需人工处理:"):
                mark_product_manual(product["id"], product["name"], err_reason)
                log("BATCH", f"⏭ 已跳过并转人工: {err_reason}")
            else:
                mark_product_failed(product["id"], product["name"], err_reason)
                fail_count += 1
                log("BATCH", f"❌ 失败: {err_reason}")

        # 回到草稿箱，准备处理下一个
        log("BATCH", "回到草稿箱...")
        page.goto(DRAFT_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

    # 5. 总结
    log("BATCH", f"\n{'='*60}")
    log("BATCH", f"===== 处理完成 [{mode_label}] =====")
    log("BATCH", f"总计: {len(remaining)} 个商品")
    log("BATCH", f"成功: {success_count} | 失败: {fail_count}")
    log("BATCH", f"日志文件: {LOG_FILE}")
    log("BATCH", f"{'='*60}")


# ---------- 入口 ----------

def _verify_shop_identity(name: str, sid: str):
    """
    核对店铺身份：名字与ID都非空，且名字在 SHOP_MAPPING 中、其映射ID与页面检测ID一致。

    返回: (ok: bool, msg: str)
    """
    if not name or not sid:
        return False, "店铺名称或ID为空（未能从页面识别）"
    mapped = SHOP_MAPPING.get(name)
    if mapped and mapped != sid:
        return False, (f"店铺名称'{name}'映射ID({mapped})"
                       f"与页面检测ID({sid})不一致")
    return True, "OK"


def detect_shop_info(page) -> dict:
    """
    从抖店后台页面检测当前登录的店铺信息（昵称 + 短ID）。

    检测策略（按优先级）：
      1. 从页面 DOM 中查找已知店铺名称
      2. 从 URL / cookies / localStorage 中提取店铺标识
      3. 匹配 SHOP_MAPPING 获取短ID

    返回:
        {"shop_name": str, "shop_id": str, "image_dir": str}
        如果无法识别，shop_id 为空字符串
    """
    log("SHOP", "===== 开始检测店铺信息 =====")

    # 当前账号标识优先。旧逻辑会遍历 localStorage 并命中“可切换店铺列表”的
    # 第一家店，误把列表项当成当前店铺。Tea 埋点 token 的 user_unique_id
    # 对应当前页面店铺短 ID；多个 token 指向同一 ID 时可安全采用。
    current_ids = page.evaluate("""
        () => {
            const out = new Set();
            try {
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i) || '';
                    if (!key.startsWith('__tea_cache_tokens_')) continue;
                    try {
                        const obj = JSON.parse(localStorage.getItem(key) || '{}');
                        const raw = String(obj.user_unique_id || '');
                        const m = raw.match(/(?:^|_)(\\d{5,12})$/);
                        if (m) out.add(m[1]);
                    } catch (_) {}
                }
            } catch (_) {}
            return [...out];
        }
    """)
    known_ids = set(SHOP_MAPPING.values())
    matched_current_ids = [sid for sid in (current_ids or []) if sid in known_ids]
    if len(matched_current_ids) == 1:
        shop_id = matched_current_ids[0]
        detected_name = next((name for name, sid in SHOP_MAPPING.items() if sid == shop_id), "")
        image_dir = os.path.join(PRODUCT_IMAGE_ROOT, shop_id)
        log("SHOP", f"✓ 从当前页面账号标识检测店铺: 名称='{detected_name}', 短ID='{shop_id}'")
        if os.path.isdir(image_dir):
            log("SHOP", f"✓ 店铺图片目录存在: {image_dir}")
        else:
            log("SHOP", f"⚠ 店铺图片目录不存在: {image_dir}（需要手动创建）")
        return {"shop_name": detected_name, "shop_id": shop_id, "image_dir": image_dir}

    # 将已知店铺名称传给 JS，在页面文本中搜索
    shop_names_json = json.dumps(list(SHOP_MAPPING.keys()), ensure_ascii=False)

    detected_name = page.evaluate("""
        (shopNamesJson) => {
            let shopNames = JSON.parse(shopNamesJson);
            // 获取页面可见文本（限定在头部/导航区域，避免匹配到商品标题中的文字）
            let headerSelectors = [
                'header', 'nav', '.header', '.nav',
                '[class*="header"]', '[class*="nav"]',
                '[class*="shop"]', '[class*="account"]',
                '[class*="user-info"]', '[class*="topbar"]',
                '.ecom-layout-header', '.ecom-layout-topbar',
            ];
            let searchText = '';
            for (let sel of headerSelectors) {
                let els = document.querySelectorAll(sel);
                for (let el of els) {
                    let rect = el.getBoundingClientRect();
                    // 只取页面顶部 200px 以内的元素
                    if (rect.top <= 200) {
                        searchText += ' ' + (el.innerText || '');
                    }
                }
            }
            // 如果头部没找到，搜索整个页面文本
            if (!searchText.trim()) {
                searchText = document.body.innerText || '';
            }

            // 在文本中查找已知店铺名称
            for (let name of shopNames) {
                if (searchText.includes(name)) {
                    return name;
                }
            }

            // 尝试从 localStorage 读取店铺信息
            try {
                let keys = Object.keys(localStorage);
                for (let key of keys) {
                    let val = localStorage.getItem(key);
                    if (val) {
                        for (let name of shopNames) {
                            if (val.includes(name)) {
                                return name;
                            }
                        }
                    }
                }
            } catch(e) {}

            return '';
        }
    """, shop_names_json)

    if detected_name:
        shop_id = SHOP_MAPPING.get(detected_name, "")
        image_dir = os.path.join(PRODUCT_IMAGE_ROOT, shop_id) if shop_id else ""
        log("SHOP", f"✓ 检测到店铺: 名称='{detected_name}', 短ID='{shop_id}'")
        if image_dir:
            if os.path.isdir(image_dir):
                log("SHOP", f"✓ 店铺图片目录存在: {image_dir}")
            else:
                log("SHOP", f"⚠ 店铺图片目录不存在: {image_dir}（需要手动创建）")
        return {"shop_name": detected_name, "shop_id": shop_id, "image_dir": image_dir}

    # 未找到已知店铺名称，尝试从页面读取任何店铺标识
    log("SHOP", "未在页面中匹配到已知店铺名称，尝试读取页面店铺信息...")

    fallback_info = page.evaluate("""
        () => {
            // 尝试从各种可能的元素中读取店铺名称
            let selectors = [
                '[class*="shop-name"]', '[class*="shopName"]',
                '[class*="store-name"]', '[class*="storeName"]',
                '[class*="merchant"]', '[class*="seller"]',
                '.ecom-layout-header .name', '.ecom-layout-topbar .name',
            ];
            for (let sel of selectors) {
                let el = document.querySelector(sel);
                if (el && el.innerText && el.innerText.trim().length > 1) {
                    return { name: el.innerText.trim(), source: sel };
                }
            }
            // 尝试从 document.title 提取
            let title = document.title || '';
            if (title.includes('-')) {
                let part = title.split('-')[0].trim();
                if (part.length > 1) return { name: part, source: 'title' };
            }
            return { name: '', source: '' };
        }
    """)

    fallback_name = fallback_info.get("name", "") if fallback_info else ""
    if fallback_name:
        log("SHOP", f"⚠ 读取到未知店铺名称: '{fallback_name}'（不在映射表中）")
        log("SHOP", f"  请确认该店铺的短ID并添加到 SHOP_MAPPING 中")
    else:
        log("SHOP", "⚠ 无法从页面读取店铺信息")

    return {"shop_name": fallback_name, "shop_id": "", "image_dir": ""}


def switch_shop(page, target_shop_id: str) -> bool:
    """
    自动切换店铺。
    UI 流程: 点击右上角头像 → 点击"切换组织/店铺" → 弹窗中选择目标店铺 → 等待刷新。

    参数:
        page: Playwright Page 对象
        target_shop_id: 目标店铺短ID（如"5576543"）

    返回:
        True 切换成功, False 切换失败
    """
    # 反向查找：通过 ID 找店铺名称
    target_name = ""
    for name, sid in SHOP_MAPPING.items():
        if sid == target_shop_id:
            target_name = name
            break
    if not target_name:
        log("SWITCH", f"⚠ 目标店铺ID '{target_shop_id}' 不在映射表中，无法自动切换")
        return False

    log("SWITCH", f"===== 开始切换店铺 → {target_name} (ID:{target_shop_id}) =====")

    # 新版草稿箱为沉浸式页面，不再渲染右上角账号菜单；先回商家首页再切店。
    try:
        if "/ffa/mshop/homepage/index" not in page.url:
            log("SWITCH", "新版草稿箱无账号入口，先进入商家首页...")
            page.goto("https://fxg.jinritemai.com/ffa/mshop/homepage/index",
                      wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
    except Exception as exc:
        log("SWITCH", f"进入商家首页失败: {exc}")
        return False

    # ---- Step 1: 点击右上角头像 ----
    log("SWITCH", "Step 1: 点击右上角头像...")
    avatar_clicked = False

    # 新版商家首页入口是右上角“当前店铺名称 + 下箭头”，头像选择器会误点右侧经营卡片。
    for current_name in SHOP_MAPPING.keys():
        try:
            candidates = page.get_by_text(current_name, exact=True)
            for idx in range(candidates.count()):
                el = candidates.nth(idx)
                box = el.bounding_box()
                if box and box.get("y", 9999) < 80:
                    el.click()
                    log("SWITCH", f"  ✓ 点击顶部当前店铺入口: {current_name}")
                    avatar_clicked = True
                    break
            if avatar_clicked:
                break
        except Exception:
            continue

    # 新版顶部店铺名会被真正截成“返航新生二手手...”，先按已知店铺名的前缀
    # 匹配顶部右侧文本容器（而不是依赖 img/class）。
    if not avatar_clicked:
        try:
            header_shop_entry = page.evaluate(r"""
                (knownNames) => {
                    const vw=window.innerWidth;
                    const clean=s=>(s||'').replace(/[.。…\s▼▽▾⌄vV]+$/g,'').trim();
                    const nodes=[...document.querySelectorAll('body *')].filter(el=>{
                        const r=el.getBoundingClientRect(), s=getComputedStyle(el), t=clean(el.innerText);
                        if(!t||t.length<4||r.width<=0||r.height<=0||r.top<0||r.top>=80||r.left<vw*0.72) return false;
                        if(s.display==='none'||s.visibility==='hidden') return false;
                        return knownNames.some(name=>name.startsWith(t)||t.startsWith(name));
                    }).map(el=>{const r=el.getBoundingClientRect();return {area:r.width*r.height,cx:r.left+r.width/2,cy:r.top+r.height/2,text:(el.innerText||'').trim()};});
                    nodes.sort((a,b)=>a.area-b.area);
                    return nodes[0]||null;
                }
            """, list(SHOP_MAPPING.keys()))
            if header_shop_entry:
                page.mouse.click(header_shop_entry["cx"], header_shop_entry["cy"])
                log("SWITCH", f"  ✓ 点击新版顶部截断店铺入口: '{header_shop_entry['text']}'")
                avatar_clicked = True
        except Exception as exc:
            log("SWITCH", f"  新版顶部截断店铺入口定位失败: {exc}")

    # 2026-08 新版商家首页：真正账号入口在最顶部右侧（圆形店铺头像 + 截断店铺名
    # + 下箭头）。店铺名会被省略号截断，不能 exact 匹配；按其稳定几何区域定位：
    # top<80、left>视口80%、尺寸20~60px 的可见 img。禁止点击页面右侧店铺卡片中
    # “添加小二”的管理员头像——它不会打开账号菜单，正是上一版失败原因。
    if not avatar_clicked:
        try:
            header_avatar = page.evaluate("""
                () => {
                    const vw=window.innerWidth;
                    const imgs=[...document.querySelectorAll('img')].filter(img=>{
                        const r=img.getBoundingClientRect(), s=getComputedStyle(img);
                        return r.width>=20&&r.height>=20&&r.width<=60&&r.height<=60 &&
                               r.top>=0&&r.top<80&&r.left>vw*0.80 &&
                               s.display!=='none'&&s.visibility!=='hidden';
                    }).map(img=>{const r=img.getBoundingClientRect();return {cx:r.left+r.width/2,cy:r.top+r.height/2,left:r.left,alt:img.alt||''};});
                    imgs.sort((a,b)=>b.left-a.left);
                    return imgs[0]||null;
                }
            """)
            if header_avatar:
                page.mouse.click(header_avatar["cx"], header_avatar["cy"])
                log("SWITCH", f"  ✓ 点击新版顶部账号头像: ({round(header_avatar['cx'])}, {round(header_avatar['cy'])})")
                avatar_clicked = True
        except Exception as exc:
            log("SWITCH", f"  新版顶部账号头像定位失败: {exc}")

    # 策略A: 点击绿色圆形头像图片（最可靠，位于页面右上角 ~x=1265, y=20）
    avatar_selectors = [
        'img[class*="index_logo"]',
        'img[class*="avatar"]',
        '[class*="userIconContainer"]',
    ]
    # 顶部店铺名入口已经打开菜单时不要再点头像；连续点击会让
    # 异步加载的切店菜单重置，现场会出现“文字已显示但等待阶段超时”。
    if not avatar_clicked:
        for sel in avatar_selectors:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    el.click()
                    log("SWITCH", f"  ✓ 点击头像: {sel}")
                    avatar_clicked = True
                    break
            except Exception:
                continue

    # 策略B: 兜底 — 按当前视口宽度点击右上角（禁止使用固定 1280 宽坐标）
    if not avatar_clicked:
        try:
            viewport = page.viewport_size or {"width": 1280, "height": 720}
            avatar_x = max(20, int(viewport.get("width", 1280)) - 28)
            page.mouse.click(avatar_x, 30)
            log("SWITCH", f"  ✓ 点击动态右上角坐标 ({avatar_x}, 30)，视口宽度={viewport.get('width')}")
            avatar_clicked = True
        except Exception:
            pass

    if not avatar_clicked:
        log("SWITCH", "  ❌ 无法定位头像按钮，切换失败")
        return False

    # 等待下拉菜单渲染。实测（2026-08-28 现场截图核对）：头像下拉弹层是先开壳
    # 再异步拉店铺信息（店铺ID/管理人那几行有个转圈加载动画），"切换组织/店铺"
    # 这一行要等这次异步请求回来才会渲染出来，固定 1.5s 经常等不到——诊断截图
    # 显示 1.5s 时店铺ID还在转圈、"切换组织/店铺"整行都不存在，但同一个弹层
    # 多等几秒后完整加载出来就能看到。改成轮询等这行文字真正出现，最多等 10s。
    switch_texts = ["切换组织", "切换店铺", "切换组织/店铺", "切换组织 / 店铺"]
    poll_started = time.time()
    poll_deadline = poll_started + 10.0
    menu_ready = False
    while time.time() < poll_deadline:
        try:
            found = page.evaluate(
                """(wanted) => [...document.querySelectorAll('body *')].some(el => {
                    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                    return wanted.includes((el.innerText || '').trim()) && r.width > 0 && r.height > 0
                        && s.visibility !== 'hidden' && s.display !== 'none';
                })""",
                switch_texts,
            )
        except Exception:
            found = False
        if found:
            menu_ready = True
            break
        time.sleep(0.3)
    log("SWITCH", f"  下拉弹层{'已' if menu_ready else '未'}加载出'切换组织/店铺'，等待 {round(time.time() - poll_started, 1)}s")

    # ---- Step 2: 点击"切换组织/店铺" ----
    log("SWITCH", "Step 2: 点击'切换组织/店铺'...")
    switch_btn_clicked = False
    for text in switch_texts:
        try:
            matches = page.get_by_text(text, exact=True)
            for idx in range(matches.count()):
                btn = matches.nth(idx)
                if btn.is_visible():
                    btn.click(timeout=5000)
                    log("SWITCH", f"  ✓ 点击可见入口: '{text}' (index={idx})")
                    switch_btn_clicked = True
                    break
            if switch_btn_clicked:
                break
        except Exception:
            continue

    # 新版首页入口虽然文本可见，但外层遮罩会拦截 Playwright locator.click。
    # 用 JS 只定位最小可见文本节点，再由真实鼠标点击其中心，确保触发可信事件。
    if not switch_btn_clicked:
        try:
            coords = page.evaluate("""
                () => {
                    const wanted = new Set(['切换组织','切换店铺','切换组织/店铺','切换组织 / 店铺']);
                    const nodes = [...document.querySelectorAll('body *')].filter(el => {
                        const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                        return wanted.has((el.innerText||'').trim()) && r.width>0 && r.height>0 &&
                               s.visibility!=='hidden' && s.display!=='none';
                    }).map(el => { const r=el.getBoundingClientRect(); return {area:r.width*r.height,cx:r.left+r.width/2,cy:r.top+r.height/2,text:(el.innerText||'').trim()}; });
                    nodes.sort((a,b)=>a.area-b.area);
                    return nodes[0] || null;
                }
            """)
            if coords:
                page.mouse.click(coords["cx"], coords["cy"])
                time.sleep(1.0)
                log("SWITCH", f"  ✓ 真实鼠标点击切换入口: '{coords['text']}'")
                switch_btn_clicked = True
        except Exception as exc:
            log("SWITCH", f"  真实鼠标点击切换入口失败: {exc}")

    if not switch_btn_clicked:
        try:
            diagnostic_path = os.path.join(WORK_DIR, "panel_data", "switch_shop_diagnostic.png")
            page.screenshot(path=diagnostic_path, full_page=False)
            log("SWITCH", f"  已保存店铺切换诊断截图: {diagnostic_path}")
        except Exception:
            pass
        try:
            visible_menu_text = page.evaluate("""
                () => Array.from(document.querySelectorAll('body *'))
                    .filter(el => {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                    })
                    .map(el => (el.innerText || '').trim())
                    .filter(t => t && (t.includes('切换') || t.includes('店铺') || t.includes('组织')) && t.length < 80)
                    .slice(0, 30)
            """)
            log("SWITCH", f"  当前可见切换菜单文本: {visible_menu_text}")
        except Exception as exc:
            log("SWITCH", f"  读取切换菜单诊断信息失败: {exc}")
        log("SWITCH", "  ❌ 无法找到'切换组织/店铺'按钮，切换失败")
        return False

    time.sleep(1.5)

    # ---- Step 3: 在弹窗中选择目标店铺 ----
    log("SWITCH", f"Step 3: 在弹窗中查找店铺 '{target_name}'...")

    # 等待弹窗出现
    try:
        page.wait_for_selector('text=/请选择店铺/', timeout=5000)
        log("SWITCH", "  ✓ 弹窗已出现")
    except Exception:
        log("SWITCH", "  ⚠ 未检测到'请选择店铺'弹窗，继续尝试...")

    # 策略A: 直接通过店铺名称点击
    shop_clicked = False
    try:
        # 尝试精确匹配店铺名称
        shop_item = page.locator(f'text={target_name}').first
        if shop_item.count() > 0:
            shop_item.click()
            log("SWITCH", f"  ✓ 点击店铺: '{target_name}'")
            shop_clicked = True
    except Exception:
        pass

    # 策略B: 如果精确匹配失败，尝试部分匹配
    if not shop_clicked:
        try:
            # 在弹窗内查找包含目标店铺名称的元素
            # 使用 evaluate 在弹窗中搜索
            found = page.evaluate(f"""
                (targetName) => {{
                    // 查找所有包含目标店铺名称的元素
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    let node;
                    while (node = walker.nextNode()) {{
                        if (node.textContent.includes(targetName)) {{
                            // 找到最近的可点击父元素
                            let el = node.parentElement;
                            while (el && el !== document.body) {{
                                if (el.tagName === 'BUTTON' || el.tagName === 'A' || el.tagName === 'DIV' || el.tagName === 'LI') {{
                                    el.click();
                                    return true;
                                }}
                                el = el.parentElement;
                            }}
                            // 如果找不到可点击父元素，直接点击文本节点所在的元素
                            node.parentElement.click();
                            return true;
                        }}
                    }}
                    return false;
                }}
            """, target_name)
            if found:
                log("SWITCH", f"  ✓ 通过JS点击店铺: '{target_name}'")
                shop_clicked = True
        except Exception as e:
            log("SWITCH", f"  ⚠ JS点击失败: {e}")

    if not shop_clicked:
        try:
            diagnostic_path = os.path.join(WORK_DIR, "panel_data", "switch_shop_after_click.png")
            page.screenshot(path=diagnostic_path, full_page=False)
            log("SWITCH", f"  已保存点击切换入口后的诊断截图: {diagnostic_path} | URL={page.url}")
        except Exception:
            pass
        log("SWITCH", f"  ❌ 无法在弹窗中找到店铺 '{target_name}'，切换失败")
        return False

    # ---- Step 4: 等待页面刷新 ----
    log("SWITCH", "Step 4: 等待页面刷新...")
    time.sleep(3)

    # 等待页面加载完成
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
        log("SWITCH", "  ✓ 页面刷新完成")
    except Exception:
        log("SWITCH", "  ⚠ 等待networkidle超时，继续检查...")

    # 验证切换是否成功
    log("SWITCH", "Step 5: 验证切换结果...")
    time.sleep(2)
    new_shop_info = detect_shop_info(page)
    new_id = new_shop_info.get("shop_id", "")

    if new_id == target_shop_id:
        log("SWITCH", f"  ✅ 切换成功! 当前店铺: {new_shop_info.get('shop_name', '')} (ID:{new_id})")
        return True
    else:
        log("SWITCH", f"  ⚠ 切换后检测到店铺ID为 '{new_id}'，与目标 '{target_shop_id}' 不匹配")
        return False


def main():
    """主入口: 启动浏览器 → 检查登录 → 检测店铺 → 执行批量处理"""

    # 解析命令行参数
    parser = argparse.ArgumentParser(description="抖店草稿箱批量处理脚本")
    parser.add_argument("--test", action="store_true", help="测试模式：仅处理少量商品验证图片上传")
    parser.add_argument("--test-n", type=int, default=1, help="测试模式处理的商品数量（默认1个）")
    parser.add_argument("--shop-id", type=str, default="", help="指定目标店铺短ID（如5576543），脚本会自动切换到该店铺")
    parser.add_argument("--product-id", type=str, default="", help="指定单个商品ID，跳过草稿箱扫描直接处理该商品")
    args = parser.parse_args()

    test_mode = args.test
    test_n = args.test_n if args.test_n > 0 else 1
    target_shop_id = args.shop_id.strip() if args.shop_id else ""
    single_product_id = args.product_id.strip() if args.product_id else ""

    print("=" * 70)
    print("抖店草稿箱批量处理脚本 - 通用版 v2")
    print("功能: 扫描草稿箱 → 提取ID → 过滤已处理 → 逐个编辑 → 上传图片 → 保存草稿")
    if single_product_id:
        print(f"🎯 单商品模式: 直接处理商品 {single_product_id}")
    elif test_mode:
        print(f"🔧 测试模式: 仅处理{test_n}个商品，验证主图+白底图上传")
    else:
        print("📋 批量模式: 处理所有未完成商品")
    if target_shop_id:
        shop_name = ""
        for name, sid in SHOP_MAPPING.items():
            if sid == target_shop_id:
                shop_name = name
                break
        print(f"🏪 目标店铺: {shop_name or '未知店铺'} (ID:{target_shop_id})")
    print("=" * 70)

    with sync_playwright() as p:
        log("INIT", f"启动 Chrome (用户数据: {CHROME_USER_DATA})")

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
            log("INIT", f"复用系统浏览器: {browser_path}")

        context = p.chromium.launch_persistent_context(
            user_data_dir=CHROME_USER_DATA,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                # 稳定性加固：上传/删除图片阶段浏览器频繁崩溃，降低内存与渲染压力
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--no-first-run",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--disable-translate",
                "--js-flags=--max-old-space-size=4096",
            ],
            viewport={"width": 1440, "height": 900},
            no_viewport=False,
            **launch_extra,
        )
        page = context.pages[0] if context.pages else context.new_page()

        # 监听浏览器/页面崩溃，便于定位是 renderer crash 还是 context 被关闭
        def _on_page_crash(p):
            log("INIT", f"⚠ 页面触发 crash 事件 (url={p.url})")
        def _on_page_close(p):
            log("INIT", f"⚠ 页面触发 close 事件 (url={p.url})")
        try:
            page.on("crash", _on_page_crash)
            page.on("close", _on_page_close)
        except Exception as e:
            log("INIT", f"注册页面事件监听器失败: {e}")

        try:
            # ========== Step 0: 确保登录 ==========
            log("STEP0", "检查登录状态...")
            page.goto(DRAFT_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            # 登录检测：URL 跳转 OR 页面内容包含登录表单
            # 抖店有时 URL 不跳转，但页面内容变成了登录页
            def is_login_page():
                if "login" in page.url or "passport" in page.url:
                    return True
                # 检测页面是否有登录表单元素
                login_indicators = [
                    '.index_loginPage__K_bGK',      # 登录页容器
                    'input[placeholder*="手机号"]',   # 手机号输入框
                    'input[placeholder*="验证码"]',   # 验证码输入框
                ]
                for sel in login_indicators:
                    try:
                        if page.locator(sel).count() > 0:
                            return True
                    except:
                        pass
                return False

            # 检测是否在草稿箱页面（登录成功标志）
            def is_draft_page():
                try:
                    # 检查是否有表格行或草稿箱相关元素
                    if page.locator('tr.ecom-g-table-row').count() > 0:
                        return True
                    if "draft" in page.url.lower() and "login" not in page.url.lower() and "passport" not in page.url.lower():
                        return True
                except:
                    pass
                return False

            if not is_draft_page():
                log("STEP0", "未登录，请在浏览器中扫码/验证码登录...")
                log("STEP0", "（登录态会自动保存，下次无需重复登录）")
                for i in range(180):  # 3 分钟
                    time.sleep(1)
                    if is_draft_page():
                        log("STEP0", "登录成功!")
                        time.sleep(2)
                        break
                    if i % 10 == 0:
                        log("STEP0", f"等待登录... {i}s")
                else:
                    raise TimeoutError("登录超时（3分钟内未完成登录）")
            else:
                log("STEP0", "已登录")

            # 保存登录态（cookies + localStorage）
            context.storage_state(path=STORAGE_STATE)
            log("STEP0", f"登录态已保存: {STORAGE_STATE}")

            # ========== Step 1: 检测店铺信息 ==========
            shop_info = detect_shop_info(page)
            current_shop_id = shop_info.get("shop_id", "")
            current_shop_name = shop_info.get("shop_name", "")

            # ---------- 登录后店铺一致性核对 ----------
            # 所选上架店铺(eff_target) 与 当前已登录店铺(current_shop_id) 对比：
            #   一致  → 不切换店铺，核对名字+ID无误后直接进入草稿箱
            #   不一致 → 自动切换，切换后再次核对
            #   未指定目标店铺 → 仅核对当前店铺身份，不切换
            # 优先用 --shop-id，其次用面板写入的全局 CURRENT_SHOP_ID（所选上架店铺）
            eff_target = (target_shop_id or "").strip() or (CURRENT_SHOP_ID or "").strip()

            if eff_target:
                if current_shop_id == eff_target:
                    # 已登录店铺与目标一致 → 不切换，仅核对身份
                    ok, msg = _verify_shop_identity(current_shop_name, current_shop_id)
                    if ok:
                        log("SHOP", f"✓ 已登录店铺与目标一致（{current_shop_name} ID:{current_shop_id}），"
                                    f"无需切换，核对名字/ID无误，直接进入草稿箱")
                    else:
                        log("SHOP", f"⚠ 店铺身份核对未通过: {msg}，仍不切换（后续或受影响）")
                else:
                    # 不一致 → 自动切换，切换后重新检测并核对
                    log("SHOP", f"当前店铺 '{current_shop_name}' (ID:{current_shop_id}) "
                                f"与目标 '{eff_target}' 不一致，准备切换...")
                    switch_ok = switch_shop(page, eff_target)
                    if switch_ok:
                        # 切换成功后重新检测
                        shop_info = detect_shop_info(page)
                        current_shop_id = shop_info.get("shop_id", "")
                        current_shop_name = shop_info.get("shop_name", "")
                        ok, msg = _verify_shop_identity(current_shop_name, current_shop_id)
                        if ok:
                            log("SHOP", f"✓ 切换后店铺身份核对无误（{current_shop_name} ID:{current_shop_id}）")
                        else:
                            log("SHOP", f"⚠ 切换后店铺身份核对未通过: {msg}")
                    else:
                        log("SHOP", "⚠ 自动切换失败，将使用当前检测到的店铺继续")
            else:
                # 未指定目标店铺 → 仅核对当前店铺身份，不切换
                ok, msg = _verify_shop_identity(current_shop_name, current_shop_id)
                if ok:
                    log("SHOP", f"✓ 当前店铺身份核对无误（{current_shop_name} ID:{current_shop_id}），直接扫描草稿箱")
                else:
                    log("SHOP", f"⚠ 未指定目标店铺，且店铺身份核对未通过: {msg}（将尝试继续）")

            global IMAGE_BASE_DIR
            IMAGE_BASE_DIR = shop_info.get("image_dir", "")
            if not IMAGE_BASE_DIR:
                log("SHOP", "⚠ 未识别到店铺或店铺无对应目录，使用默认目录")
                IMAGE_BASE_DIR = os.path.join(PRODUCT_IMAGE_ROOT, "3C数码及配件", "二手手机")
            else:
                log("SHOP", f"✓ 当前店铺图片目录: {IMAGE_BASE_DIR}")
            log("SHOP", f"  商详图目录（所有店铺共享）: {DETAIL_IMAGE_DIR}")

            # ========== 执行批量处理 ==========
            if single_product_id:
                log("BATCH", f"🎯 单商品模式：直接处理 {single_product_id}")
                process_single_product(page, single_product_id)
            else:
                run_batch(page, test_mode=test_mode, test_n=test_n)

        except Exception as e:
            log("ERROR", f"主流程异常: {e}")
            import traceback
            traceback.print_exc()

        finally:
            log("INIT", "关闭浏览器...")
            context.close()


if __name__ == "__main__":
    main()

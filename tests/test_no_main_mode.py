#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证"免主图"逻辑已全局化到所有店铺（不再按店铺名单区分）。

规则：
  - 有主图            → 正常用主图，白底图照常传辅助位；
  - 无主图、有白底图  → 主图位用首张白底图顶替，其余白底图传辅助位；
  - 主图、白底图都无  → 判"主图白底图缺失"，跳过该商品。

验证点：
1. 缺主图但有白底图的机型 → classify 返回 None（放行），且 get_main_image_path 回退到首张白底图；
2. 主图与白底图都无的机型 → classify 返回"主图白底图缺失"；
3. SPECIAL_NO_MAIN 全局为 True（免主图对所有店铺生效，无需按店铺名单开启）。
"""
import sys
import os
import tempfile
import shutil
from unittest.mock import MagicMock

sys.modules['playwright'] = MagicMock()
sys.modules['playwright.sync_api'] = MagicMock()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import test_doudian_batch as dd
import panel_runner

BASE = "/Users/atrenew/商品图片/7970406/3C数码及配件/二手手机"
dd.IMAGE_BASE_DIR = BASE


def test_universal_no_main_pass():
    # 缺主图但有白底图的真实机型（荣耀 200 Pro）
    parsed = {"brand_cn": "荣耀", "brand_normalized": "荣耀", "model": "200 Pro"}
    img_dir = dd.get_image_dir(parsed)
    assert os.path.isdir(img_dir), f"期望目录存在: {img_dir}"

    main_dir = os.path.join(img_dir, "主图")
    main_files = os.listdir(main_dir) if os.path.isdir(main_dir) else []
    assert len(main_files) == 0, "主图目录应为空（缺主图）"

    aux = dd.get_aux_image_paths(parsed)
    assert len(aux) > 0, "白底图应存在"

    # 全局免主图：SPECIAL_NO_MAIN 恒为 True（对所有店铺生效）
    assert panel_runner.SPECIAL_NO_MAIN is True, "免主图应全局生效（SPECIAL_NO_MAIN 为 True）"

    # 缺主图 → 主图位回退到首张白底图
    mp = dd.get_main_image_path(parsed)
    assert os.path.isfile(mp), f"主图应回退到首张白底图: {mp}"
    assert "白底图" in mp, "回退的主图应取自白底图目录"
    # 用作主图的白底图已从辅助图列表剔除（避免同一张既当主图又当辅助图重复上传）
    assert mp not in aux, "用作主图的白底图不应再出现在辅助图列表中（防重复上传）"

    # 放行
    assert panel_runner.classify_image_error(parsed) is None, "缺主图但有白底图应放行（返回 None）"


def test_universal_both_missing():
    # 主图与白底图都无 → 判缺失（用临时目录模拟，避免依赖真实店铺目录结构）
    tmp = tempfile.mkdtemp()
    model_dir = os.path.join(tmp, "缺失测试")
    os.makedirs(model_dir)
    orig = dd.get_image_dir
    dd.get_image_dir = lambda parsed: model_dir
    try:
        parsed = {"brand_cn": "测试", "brand_normalized": "测试", "model": "X"}
        assert not os.path.isfile(dd.get_main_image_path(parsed)), "主图应不存在"
        assert len(dd.get_aux_image_paths(parsed)) == 0, "白底图应不存在"
        assert panel_runner.classify_image_error(parsed) == "主图白底图缺失", "主图白底图都无应判缺失"
    finally:
        dd.get_image_dir = orig
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_universal_no_main_pass()
    test_universal_both_missing()
    print("===== test_no_main_mode 全部通过 =====")

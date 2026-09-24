"""不接触真实店铺的稳定性压力回归。直接运行即可，不依赖 pytest。"""

import importlib.util
import os
import random
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_doudian_batch as dd


def stress_brand_model(rounds=20000):
    redmi_models = ["Redmi Note 13 Pro", "REDMI K80 Pro", "红米 K70 至尊版"]
    for _ in range(rounds):
        model = random.choice(redmi_models)
        plan = dd.resolve_brand_model_plan({
            "brand_cn": "红米", "brand_normalized": "Redmi/红米", "model": model
        })
        assert dd._brand_value_matches_plan(plan, random.choice(["Redmi/红米", "REDMI/红米", "redmi/红米"]))
        assert not dd._brand_value_matches_plan(plan, random.choice(["Xiaomi/小米", "小米", "Huawei/华为", ""]))


def stress_title_plans(rounds=5000):
    titles = [
        "95新 Xiaomi/小米 Redmi Note 13 Pro 二手手机",
        "9新 Xiaomi/小米 Redmi K70 (至尊版) 二手手机",
        "99新 Huawei/华为 华为 Pura X 二手手机",
        "99新 Huawei/华为 华为 Mate X5 二手手机",
        "99新 REDMI K80 Pro 二手手机",
    ]
    for _ in range(rounds):
        parsed = dd.parse_title(random.choice(titles))
        plan = dd.resolve_brand_model_plan(parsed)
        assert plan["target_brand_value"]
        assert plan["source_model"]
        if plan["is_redmi"]:
            assert plan["target_brand_value"] == "Redmi/红米"


def stress_image_thresholds():
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for index in range(1, 6):
            path = Path(tmp) / ("白底图_%d.jpg" % index)
            path.write_bytes(b"test")
            files.append(str(path))
        for valid_count in range(0, 6):
            with patch.object(dd, "get_aux_image_paths", return_value=files), \
                 patch.object(dd, "_is_valid_main_area_image", side_effect=lambda p, n=valid_count: files.index(p) < n):
                expected, actual_valid = dd._expected_main_image_total({})
                assert actual_valid == valid_count
                assert expected == max(4, 1 + min(4, valid_count))


class FakeSavePage:
    def __init__(self, outcomes):
        self.url = "https://fxg.jinritemai.com/ffa/g/create?product_id=1"
        self.outcomes = iter(outcomes)

    def evaluate(self, script, *args):
        if "const overlays=" in script:
            return {"found": False, "closed": False, "action": "", "text": ""}
        if "暂不想更新规格信息" in script and "修改规格信息" in script:
            return {"found": True, "state": "关", "aria": "false",
                    "className": "", "method": "test", "x": 0, "y": 0, "w": 20, "h": 20}
        if "scrollTo" in script:
            return "scrolled to bottom"
        if "clicked by text" in script:
            return "clicked by text: 保存草稿"
        return next(self.outcomes)


def stress_save_confirmation(rounds=1000):
    with patch.object(dd.time, "sleep", return_value=None), patch.object(dd, "log", return_value=None):
        for _ in range(rounds):
            assert dd.save_draft(FakeSavePage([{"success": True, "message": "保存成功"}]))
            assert not dd.save_draft(FakeSavePage([
                {"success": False, "terminal": False, "message": "仍在编辑页"}
            ] * 12))
            assert not dd.save_draft(FakeSavePage([
                {"success": False, "terminal": True, "message": "保存失败"}
            ]))


def run_publish_regressions():
    spec = importlib.util.spec_from_file_location("publish_tests", "tests/test_publish_logic.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with patch.object(dd.time, "sleep", return_value=None), patch.object(dd, "log", return_value=None):
        for name in sorted(n for n in dir(module) if n.startswith("test_")):
            getattr(module, name)()


def main():
    random.seed(20260831)
    stress_brand_model()
    stress_title_plans()
    stress_image_thresholds()
    stress_save_confirmation()
    run_publish_regressions()
    print("STABILITY_STRESS_PASS")
    print("brand_model=20000 title_plan=5000 image_matrix=6 save=3000 publish=9")


if __name__ == "__main__":
    main()

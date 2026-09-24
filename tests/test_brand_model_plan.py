import test_doudian_batch as dd


def test_redmi_requires_independent_platform_brand():
    plan = dd.resolve_brand_model_plan({
        "brand_cn": "红米",
        "brand_normalized": "Redmi/红米",
        "model": "Redmi Note 13 Pro",
    })
    assert plan["target_brand_value"] == "Redmi/红米"
    assert dd._brand_value_matches_plan(plan, "Redmi/红米")
    assert not dd._brand_value_matches_plan(plan, "Xiaomi/小米")


def test_xiaomi_does_not_accept_redmi_as_alias():
    plan = dd.resolve_brand_model_plan({
        "brand_cn": "小米",
        "brand_normalized": "Xiaomi/小米",
        "model": "小米 15",
    })
    assert dd._brand_value_matches_plan(plan, "Xiaomi/小米")
    assert not dd._brand_value_matches_plan(plan, "Redmi/红米")


def test_huawei_exact_model_override_belongs_to_plan():
    plan = dd.resolve_brand_model_plan({
        "brand_cn": "华为",
        "brand_normalized": "Huawei/华为",
        "model": "Pura X",
    })
    assert plan["target_brand_value"] == "Huawei/华为"
    assert plan["target_model_exact"] == "HUAWEI Pura X"


def test_k70_override_does_not_change_brand_identity():
    plan = dd.resolve_brand_model_plan({
        "brand_cn": "红米",
        "brand_normalized": "Redmi/红米",
        "model": "Redmi K70 至尊版",
    })
    assert plan["target_brand_value"] == "Redmi/红米"
    assert plan["target_model_exact"] == "K70至尊"

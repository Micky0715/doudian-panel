"""已处理商品状态策略。

草稿测试通过不等于正式发布完成。这里集中定义成功状态和批量跳过规则，
避免面板循环与旧批量入口再次各自实现一份判断。
"""

PUBLISHED_STATUS = "done"
DRAFT_TESTED_STATUS = "draft_tested"
MANUAL_STATUS = "manual_required"


def success_status(save_only: bool) -> str:
    """根据本轮实际动作返回应写入的成功状态。"""
    return DRAFT_TESTED_STATUS if save_only else PUBLISHED_STATUS


def should_skip_in_normal_batch(status: str) -> bool:
    """正式/普通批量只跳过已发布或已明确转人工的商品。"""
    return str(status or "") in {PUBLISHED_STATUS, MANUAL_STATUS}

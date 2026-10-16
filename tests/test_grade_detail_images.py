import struct
import tempfile
import unittest
from pathlib import Path

from app.grade_detail_images import GradeDetailError, build_grade_detail_plan, build_grade_only_plan
from app.grade_detail_images.plan import COMMON_SLOTS


def _png(width=800, height=1000, marker=b""):
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00" + marker


def _jpeg(width=1125, height=1685):
    return (
        b"\xff\xd8\xff\xc0"
        + struct.pack(">HBHHB", 17, 8, height, width, 3)
        + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00\xff\xd9"
    )


def _detail(*grades):
    return {"specs": [{"name": "颜色", "values": [{"name": "黑色"}]},
                      {"name": "成色", "values": [{"name": grade} for grade in grades]}]}


class GradeDetailPlanTests(unittest.TestCase):
    def _gallery(self, folder, grades=("99新", "95新")):
        root = Path(folder)
        for index, slot in enumerate((*COMMON_SLOTS, *grades)):
            (root / (f" {slot}.jpg" if "新" in slot else f"{slot}.jpg")).write_bytes(
                _png(marker=str(index).encode())
            )
        return root

    def test_two_actual_grades_produce_fifteen_ordered_images(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self._gallery(folder)
            plan = build_grade_detail_plan(_detail("99新", "95新"), root)
            self.assertEqual(len(plan.images), 15)
            self.assertEqual([item.slot for item in plan.images[:5]],
                             ["1", "2", "99新", "95新", "4"])
            self.assertEqual(plan.images[2].upload_name, "99新.png")

    def test_missing_or_unrecognised_grade_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self._gallery(folder)
            with self.assertRaisesRegex(GradeDetailError, "槽位 9新"):
                build_grade_detail_plan(_detail("9新"), root)
            with self.assertRaisesRegex(GradeDetailError, "明确支持"):
                build_grade_detail_plan(_detail("85新"), root)

    def test_duplicate_grade_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self._gallery(folder)
            with self.assertRaisesRegex(GradeDetailError, "重复"):
                build_grade_detail_plan(_detail("99新", "99新"), root)

    def test_grade_only_plan_does_not_require_common_gallery_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / " 99新.jpg").write_bytes(_png(marker=b"a"))
            (root / " 95新.jpg").write_bytes(_png(marker=b"b"))
            plan = build_grade_only_plan(_detail("99新", "95新"), root)
            self.assertEqual([item.slot for item in plan.images], ["99新", "95新"])

    def test_nearly_new_uses_no_grade_image(self):
        with tempfile.TemporaryDirectory() as folder:
            only = build_grade_only_plan(_detail("准新品"), folder)
            self.assertEqual(only.strategy, "no_grade_image")
            self.assertEqual(only.images, ())
            root = self._gallery(folder, grades=())
            complete = build_grade_detail_plan(_detail("准新品"), root)
            self.assertEqual(complete.strategy, "common_only")
            self.assertEqual([image.slot for image in complete.images], list(COMMON_SLOTS))
            with self.assertRaisesRegex(GradeDetailError, "混用"):
                build_grade_only_plan(_detail("准新品", "99新"), root)

    def test_doudian_decorative_brackets_around_grade_group_are_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / " 99新.jpg").write_bytes(_png(marker=b"a"))
            detail = _detail("99新")
            detail["specs"][1]["name"] = "【成色】"
            plan = build_grade_only_plan(detail, root)
            self.assertEqual(plan.grades, ("99新",))
            self.assertEqual([item.slot for item in plan.images], ["99新"])

    def test_plain_and_bracketed_grade_groups_still_count_as_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            detail = _detail("99新")
            detail["specs"].append({"name": "【成色】", "values": [{"name": "99新"}]})
            with self.assertRaisesRegex(GradeDetailError, "缺失或重复"):
                build_grade_only_plan(detail, folder)

    def test_reviewed_ninety_new_alias_uses_nine_new_image(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / " 9新.jpg").write_bytes(_png(marker=b"nine"))
            plan = build_grade_only_plan(_detail("90新"), root)
            self.assertEqual(plan.grades, ("90新",))
            self.assertEqual([image.slot for image in plan.images], ["9新"])
            with self.assertRaisesRegex(GradeDetailError, "同一张成色图"):
                build_grade_only_plan(_detail("90新", "9新"), root)

    def test_all_three_grades_use_maintained_combined_jpeg(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "三合一.jpeg").write_bytes(_jpeg())
            plan = build_grade_only_plan(_detail("99新", "95新", "9新"), folder)
            self.assertEqual(plan.strategy, "combined_gallery")
            self.assertEqual([image.slot for image in plan.images], ["三合一"])
            self.assertEqual(plan.images[0].upload_name, "三合一.jpg")
            self.assertEqual((plan.images[0].width, plan.images[0].height), (1125, 1685))
            self.assertEqual(plan.grades, ("99新", "95新", "9新"))

    def test_all_three_grades_require_exactly_one_combined_image(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(GradeDetailError, "槽位 三合一"):
                build_grade_only_plan(_detail("99新", "95新", "9新"), folder)


if __name__ == "__main__":
    unittest.main()

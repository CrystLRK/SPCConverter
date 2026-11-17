from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spc_v0_chart_decoder import (  # noqa: E402
    SCENE_HIDEGROUP,
    TIMINGGROUP_NOCLIP_BIT,
    TIMINGGROUP_NOINPUT_BIT,
    read_spc_v0_chart,
)


class BdChartCompatibilityTests(unittest.TestCase):
    def parse(self, text: str) -> dict:
        directory = Path(tempfile.mkdtemp(prefix="bd-chart-compat-"))
        try:
            path = directory / "chart.spc"
            path.write_text(text, encoding="utf-8")
            return read_spc_v0_chart(path)
        finally:
            for child in directory.iterdir():
                child.unlink()
            directory.rmdir()

    def test_named_timinggroup_groupend_and_hidegroup(self) -> None:
        chart = self.parse(
            "chart(100,4)\n"
            "timinggroup(blackline_rain,noinput):\n"
            "track(0,0.5)\n"
            "hidegroup(10,1)\n"
            "skyarea(0,50,100,10,50,100,10,0,0,20,7)\n"
            "groupend()\n"
            "tap(5.4,1,2)\n"
        )

        sky = next(n for n in chart["notes"] if n["type"] == 5)
        marker = next(n for n in chart["notes"] if n["type"] == 3)
        tap = next(n for n in chart["notes"] if n["type"] == 1)
        self.assertEqual((sky["line"] >> 32) & 0xFFFFFFFF, 0x80000001)
        self.assertEqual(marker["line"], 1)
        self.assertEqual(marker["endMs"], 1)
        self.assertEqual(marker["flags"] & 0xFF, SCENE_HIDEGROUP)
        self.assertEqual(tap["timeMs"], 5)
        self.assertTrue(any(
            e["type"] == 5 and e["value"] == 0.5 and e["speed"] == 1
            for e in chart["events"]
        ))

    def test_hidegroup_can_target_base_timing_group(self) -> None:
        chart = self.parse(
            "chart(100,4)\n"
            "hidegroup(10,1)\n"
            "tap(20,1,2)\n"
            "hidegroup(30,0)\n"
        )

        markers = [
            n for n in chart["notes"]
            if n["type"] == 3 and (n["flags"] & 0xFF) == SCENE_HIDEGROUP
        ]
        self.assertEqual([n["line"] for n in markers], [0, 0])
        self.assertEqual([n["endMs"] for n in markers], [1, 0])

    def test_braced_noclip_timinggroup_sets_both_modes(self) -> None:
        chart = self.parse(
            "chart(100,4)\n"
            "timinggroup(7,noclip) {\n"
            "skyarea(0,50,100,10,50,100,10,0,0,20,7)\n"
            "}\n"
        )

        sky = next(n for n in chart["notes"] if n["type"] == 5)
        packed = (sky["line"] >> 32) & 0xFFFFFFFF
        self.assertEqual(packed & 0x1FFFFFFF, 7)
        self.assertTrue(packed & TIMINGGROUP_NOINPUT_BIT)
        self.assertTrue(packed & TIMINGGROUP_NOCLIP_BIT)

    def test_noclip_false_leaves_group_as_normal_timinggroup(self) -> None:
        chart = self.parse(
            "chart(100,4)\n"
            "timinggroup(7,noclip=false) {\n"
            "skyarea(0,50,100,10,50,100,10,0,0,20,7)\n"
            "}\n"
        )

        sky = next(n for n in chart["notes"] if n["type"] == 5)
        packed = (sky["line"] >> 32) & 0xFFFFFFFF
        self.assertEqual(packed, 7)

    def test_fractional_milliseconds_round_half_up(self) -> None:
        chart = self.parse(
            "chart(120,4)\n"
            "flick(10.4,50,100,20,4)\n"
            "skyarea(20.5,50,100,10,50,100,10,0,0,30.6)\n"
        )
        flick = next(n for n in chart["notes"] if n["type"] == 4)
        sky = next(n for n in chart["notes"] if n["type"] == 5)
        self.assertEqual(flick["timeMs"], 10)
        self.assertEqual(sky["timeMs"], 21)
        self.assertEqual(sky["endMs"], 52)

    def test_short_skyarea_preserves_authored_duration(self) -> None:
        chart = self.parse(
            "chart(120,4)\n"
            "skyarea(100,50,100,10,50,100,10,0,0,1)\n"
        )
        sky = next(n for n in chart["notes"] if n["type"] == 5)
        self.assertEqual(sky["timeMs"], 100)
        self.assertEqual(sky["endMs"], 101)

    def test_notes_are_stably_sorted_by_time_and_duration(self) -> None:
        chart = self.parse(
            "chart(120,4)\n"
            "tap(200,1,2)\n"
            "tap(100,1,4)\n"
            "tap(100,1,3)\n"
            "skyarea(150,50,100,10,50,100,10,0,0,20,10)\n"
        )
        self.assertEqual(
            [(n["type"], n["timeMs"], n["endMs"]) for n in chart["notes"]],
            [(1, 100, 100), (1, 100, 100), (5, 150, 170), (1, 200, 200)],
        )

    def test_skyarea_group_ids_are_preserved(self) -> None:
        chart = self.parse(
            "chart(120,4)\n"
            "skyarea(100,50,100,10,50,100,10,0,0,20,10)\n"
            "skyarea(200,50,100,10,50,100,10,0,0,20,1)\n"
        )
        self.assertEqual([n["line"] for n in chart["notes"]], [10, 1])

    def test_resumed_skyarea_group_is_not_remapped(self) -> None:
        chart = self.parse(
            "chart(120,4)\n"
            "skyarea(100,20,100,20,20,100,20,0,0,20,7)\n"
            "skyarea(200,20,100,20,20,100,20,0,0,20,8)\n"
            "skyarea(300,20,100,20,20,100,20,0,0,20,7)\n"
        )
        self.assertEqual([n["line"] for n in chart["notes"]], [7, 8, 7])

    def test_same_group_skyareas_keep_authored_geometry(self) -> None:
        chart = self.parse(
            "chart(120,4)\n"
            "skyarea(100,20,100,20,20,100,20,0,0,100,7)\n"
            "skyarea(150,40,100,20,40,100,20,0,0,100,7)\n"
        )
        sky = [n for n in chart["notes"] if n["type"] == 5]
        self.assertEqual(len(sky), 2)
        self.assertTrue(all(not n.get("flags", 0) for n in sky))
        starts = [n["timeMs"] for n in chart["notes"]]
        self.assertEqual(starts, sorted(starts))

    def test_root_compatibility_decoder_uses_canonical_rules(self) -> None:
        root_path = (
            Path(__file__).resolve().parents[2]
            / "internal-docs"
            / "archive"
            / "legacy-scripts"
            / "spc_v0_chart_decoder.py"
        )
        spec = importlib.util.spec_from_file_location(
            "root_spc_v0_chart_decoder", root_path
        )
        if spec is None or spec.loader is None:
            self.fail(f"无法加载根目录兼容解析器：{root_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertFalse(hasattr(module, "AUTO_GROUP_PACK_ENABLED"))
        self.assertFalse(hasattr(module, "BD_SKYAREA_RUN_REMAP"))
        self.assertFalse(hasattr(module, "PACK_FLAG"))
        self.assertFalse(hasattr(module, "PIECE_FLAG"))
        self.assertFalse(hasattr(module, "PACKTRAP_MAGIC"))
        self.assertFalse(hasattr(module, "MIN_SKYAREA_RECORD_DURATION_MS"))

    def test_spatial_overlap_preserves_individual_skyareas(self) -> None:
        chart = self.parse(
            "chart(120,4)\n"
            "skyarea(100,50,100,60,50,100,60,0,0,100,7)\n"
            "skyarea(150,50,100,60,50,100,60,0,0,100,7)\n"
        )
        sky = [n for n in chart["notes"] if n["type"] == 5]
        self.assertEqual(len(sky), 2)
        self.assertTrue(all(not n.get("flags", 0) for n in sky))

    def test_different_groups_are_not_combined(self) -> None:
        chart = self.parse(
            "chart(120,4)\n"
            "skyarea(100,20,100,20,20,100,20,0,0,100,7)\n"
            "skyarea(100,60,100,20,60,100,20,0,0,100,8)\n"
        )
        sky = [n for n in chart["notes"] if n["type"] == 5]
        self.assertEqual(len(sky), 2)
        self.assertTrue(all(not n.get("flags", 0) for n in sky))

    def test_same_group_but_spatially_disconnected_is_not_combined(self) -> None:
        chart = self.parse(
            "chart(120,4)\n"
            "skyarea(100,10,100,20,10,100,20,0,0,100,7)\n"
            "skyarea(100,70,100,20,70,100,20,0,0,100,7)\n"
        )
        sky = [n for n in chart["notes"] if n["type"] == 5]
        self.assertEqual(len(sky), 2)
        self.assertTrue(all(not n.get("flags", 0) for n in sky))

    def test_noinput_same_group_keeps_timing_flag_without_converter_pack(
        self,
    ) -> None:
        chart = self.parse(
            "chart(120,4)\n"
            "timinggroup(3,noinput=true) {\n"
            "skyarea(100,20,100,20,20,100,20,0,0,100,7)\n"
            "skyarea(100,40,100,20,40,100,20,0,0,100,7)\n"
            "}\n"
        )
        sky = [n for n in chart["notes"] if n["type"] == 5]
        self.assertEqual(len(sky), 2)
        self.assertTrue(all((n["line"] >> 32) & 0x80000000 for n in sky))

    def test_mirairo_fixture_preserves_authored_skyareas(self) -> None:
        fixture = Path(__file__).resolve().parent / "fixtures" / "skyarea_group_connectivity.spc"
        chart = read_spc_v0_chart(str(fixture))
        sky = [n for n in chart["notes"] if n["type"] == 5]
        self.assertEqual(len(sky), 12)
        self.assertTrue(all(not n.get("flags", 0) for n in sky))

    def test_native_sequential_chain_is_not_rewritten_at_conversion(
        self,
    ) -> None:
        chart = self.parse(
            "chart(120,4)\n"
            "skyarea(100,10,100,20,10,100,20,0,0,100,7)\n"
            "skyarea(200,30,100,20,30,100,20,0,0,100,7)\n"
            "skyarea(500,80,100,20,80,100,20,0,0,100,7)\n"
        )
        sky = [n for n in chart["notes"] if n["type"] == 5]
        self.assertEqual(len(sky), 3)
        self.assertTrue(all(not n.get("flags", 0) for n in sky))

    def test_manual_skyareapack_is_permanently_retired(self) -> None:
        with self.assertRaisesRegex(ValueError, "已永久停用"):
            self.parse(
                "chart(120,4)\n"
                "skyareapack(7)\n"
                "skyarea(100,20,100,20,20,100,20,0,0,100,7)\n"
            )


if __name__ == "__main__":
    unittest.main()

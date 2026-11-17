from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from chart_packer import pack_chart  # noqa: E402
from icp1_codec import decode  # noqa: E402
from spc_json_conv import raw_to_standard  # noqa: E402
from spc_semantic import semantic_digest, serialize_v0_semantic  # noqa: E402
from spc_v0_chart_decoder import read_spc_v0_chart  # noqa: E402


class SemanticRoundtripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="spc-semantic-roundtrip-"))

    def tearDown(self) -> None:
        for path in sorted(self.temp.iterdir(), reverse=True):
            if path.is_file():
                path.unlink()
        self.temp.rmdir()

    def parse(self, text: str) -> dict:
        path = self.temp / "chart.spc"
        path.write_text(text, encoding="utf-8")
        return read_spc_v0_chart(str(path))

    def v1_roundtrip(self, standard: dict, key: str) -> tuple[dict, bytes]:
        data = bytes(pack_chart(standard, key))
        return raw_to_standard(decode(data, key)), data

    def assert_semantically_stable(self, standard: dict, key: str) -> None:
        expected = semantic_digest(standard)

        first_v1, _ = self.v1_roundtrip(standard, key)
        self.assertEqual(semantic_digest(first_v1), expected)

        first_v0 = serialize_v0_semantic(first_v1)
        reparsed = self.parse(first_v0)
        self.assertEqual(semantic_digest(reparsed), expected)

        second_v1, _ = self.v1_roundtrip(reparsed, key)
        self.assertEqual(semantic_digest(second_v1), expected)

        second_v0 = serialize_v0_semantic(second_v1)
        reparsed_again = self.parse(second_v0)
        self.assertEqual(semantic_digest(reparsed_again), expected)

    def test_v0_v1_v0_v1_preserves_supported_semantics(self) -> None:
        source = (
            "chart(196,4)\n"
            "audiooffset(25)\n"
            "track(0,1.0)\n"
            "bpm(100,196,0.25)\n"
            "bpm(200,196,4,1)\n"
            "lane(250,4,1)\n"
            "tap(300,1,2)\n"
            "hold(400,2,1,150)\n"
            "flick(500,30,100,20,4)\n"
            "bomb(600,2,3)\n"
            "skyarea(700,20,100,10,40,100,20,1,2,90,20)\n"
            "showtext(900,7,6)\n"
            "setcolor(950,20,#112233,0)\n"
            "scenecontrol(1000,trackdisplay,120,160)\n"
            "scenecontrol(1100,hidegroup,0,1)\n"
            "trackmove(1200,1.5,-2.25,3.5,10,20,30,3,200)\n"
            "timinggroup(1) {\n"
            "  track(0,0.5)\n"
            "  track(1300,0)\n"
            "  track(1400,-1)\n"
            "  tap(1250,1,1)\n"
            "  skyarea(1350,10,100,10,30,100,10,0,0,100,30)\n"
            "}\n"
            "timinggroup(2,noinput=true) {\n"
            "  track(0,0.25)\n"
            "  tap(1500,1,3)\n"
            "  hold(1520,3,1,80)\n"
            "  flick(1540,70,100,20,16)\n"
            "  bomb(1560,1,4)\n"
            "  skyarea(1580,80,100,10,60,100,10,2,0,80,40)\n"
            "}\n"
            "timinggroup(3,noclip=true) {\n"
            "  track(0,2)\n"
            "  tap(1700,1,4)\n"
            "  skyarea(1720,30,100,10,30,100,10,0,0,80,50)\n"
            "}\n"
        )
        self.assert_semantically_stable(self.parse(source), "roundtrip.spc")

    def test_speed_only_timinggroup_is_preserved_and_deduplicated(self) -> None:
        standard = self.parse(
            "chart(120,4)\n"
            "timinggroup(1) {\n"
            "  track(0,1.0)\n"
            "  track(0,2.0)\n"
            "}\n"
        )
        expected = semantic_digest(standard)
        self.assertEqual(expected[4], ((1, ((0, 2.0),)),))
        self.assertEqual(
            [(event["type"], event["timeMs"], event["value"])
             for event in standard["events"]],
            [(5, 0, 2.0)],
        )

        text = serialize_v0_semantic(standard)
        self.assertEqual(text.count("timinggroup(1)"), 1)
        self.assertEqual(text.count("track(0,"), 1)

        reparsed = self.parse(text)
        self.assertEqual(semantic_digest(reparsed), expected)

    def test_v0_ground_group_placeholder_is_zero(self) -> None:
        standard = self.parse(
            "chart(120,4)\n"
            "tap(100,1,1)\n"
            "hold(200,2,1,50)\n"
            "flick(300,50,100,20,4)\n"
        )
        _, data = self.v1_roundtrip(standard, "ground-group.spc")
        decoded = decode(data, "ground-group.spc")
        for note in decoded["notes"]:
            if note["type"] in (1, 2, 4):
                self.assertEqual(note["group"] & 0xFFFFFFFF, 0)

    def test_unknown_v0_statement_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "未知或不支持的 SPC 语句"):
            self.parse("chart(120,4)\nunknown_note(100,1)\n")

    def test_unknown_v1_note_type_fails_v0_serialization(self) -> None:
        standard = self.parse("chart(120,4)\n")
        standard["notes"].append({
            "index": 0,
            "line": 0,
            "type": 9,
            "track": 1,
            "timeMs": 100,
            "endMs": 100,
            "fractions": [[0, 1], [0, 1], [1, 4], [1, 4]],
        })
        with self.assertRaisesRegex(ValueError, "不支持的 note type"):
            serialize_v0_semantic(standard)

    def test_unknown_v1_event_type_fails_v0_serialization(self) -> None:
        standard = self.parse("chart(120,4)\n")
        standard["events"].append({
            "id": 0,
            "type": 9,
            "timeMs": 100,
            "value": 1,
        })
        with self.assertRaisesRegex(ValueError, "不支持的 event type"):
            serialize_v0_semantic(standard)

    def test_flick_direction_is_part_of_semantic_digest(self) -> None:
        standard = self.parse(
            "chart(120,4)\n"
            "flick(100,50,100,20,4)\n"
        )
        changed = copy.deepcopy(standard)
        changed["notes"][0]["flick"] = 16
        self.assertNotEqual(semantic_digest(standard), semantic_digest(changed))

    def test_skyarea_ease_is_part_of_semantic_digest(self) -> None:
        standard = self.parse(
            "chart(120,4)\n"
            "skyarea(100,20,100,10,40,100,20,1,2,90,7)\n"
        )
        changed = copy.deepcopy(standard)
        changed["notes"][0]["leftEase"] = 2
        changed["notes"][0]["rightEase"] = 0
        self.assertNotEqual(semantic_digest(standard), semantic_digest(changed))


if __name__ == "__main__":
    unittest.main()

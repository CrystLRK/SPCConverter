from __future__ import annotations

import json
import math
import os
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from chart_packer import main as packer_main, pack_chart  # noqa: E402
from chart_validation import (  # noqa: E402
    ChartValidationError,
    validate_standard_chart,
)
from config_profiles import (  # noqa: E402
    last_profile_error,
    load_profiles,
    profile_label_map,
    render_template,
)
from showtext_validation import ShowTextValidationError, validate_showtext_sidecar  # noqa: E402
import spc_converter as converter  # noqa: E402
import spc_json_conv  # noqa: E402
from icp1_codec import decode as raw_decode, encode as raw_encode  # noqa: E402
from spc_json_conv import raw_to_standard, serialize_v0  # noqa: E402
from spc_v0_chart_decoder import read_spc_v0_chart  # noqa: E402
from song_package import DIFFICULTY_SLOTS  # noqa: E402


class CodeReviewRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="ifce-review-tests-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def parse(self, text: bytes | str, *, encoding: str = "utf-8") -> dict:
        path = self.temp / "chart.spc"
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding=encoding)
        return read_spc_v0_chart(str(path))

    def test_utf8_bom_is_accepted(self) -> None:
        chart = self.parse("chart(120,4)\ntap(100,1,2)\n", encoding="utf-8-sig")
        self.assertEqual(chart["noteCount"], 1)

    def test_fractional_event_milliseconds_round(self) -> None:
        chart = self.parse(
            "chart(120,4)\n"
            "track(10.6,1.25)\n"
            "bpm(20.6,130,4,0)\n"
            "lane(30.6,2,1)\n"
        )
        self.assertEqual([event["timeMs"] for event in chart["events"]], [11, 21, 31])

    def test_removed_skynote_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "skynote 已停用"):
            self.parse(
                "chart(120,4)\n"
                "skynote(100,2,4,1)\n"
            )

    def test_legacy_packer_defaults_to_documented_output(self) -> None:
        old_cwd = Path.cwd()
        try:
            os.chdir(self.temp)
            source = self.temp / "legacy.spc"
            source.write_text(
                "chart(120,4)\ntap(100,1,2)\n",
                encoding="utf-8",
            )
            self.assertEqual(
                packer_main(["--spcV02spcV1", str(source)]),
                0,
            )
            self.assertTrue((self.temp / "output" / "legacy.spc").is_file())
            self.assertFalse((self.temp / "output" / "legacy00.spc").exists())
        finally:
            os.chdir(old_cwd)

    def test_showtext_sidecar_is_required_and_validated(self) -> None:
        chart = self.parse(
            "chart(120,4)\n"
            "showtext(100,7,6)\n"
        )
        chart_path = self.temp / "chart.spc"
        chart_path.write_bytes(pack_chart(chart, chart_path.name))

        with self.assertRaises(ShowTextValidationError):
            validate_showtext_sidecar(chart_path, chart_path.name)

        sidecar = chart_path.with_suffix(".text.json")
        sidecar.write_text(
            json.dumps({"texts": [{"id": 6, "text": "ok"}]}),
            encoding="utf-8",
        )
        with self.assertRaises(ShowTextValidationError):
            validate_showtext_sidecar(chart_path, chart_path.name)

        sidecar.write_text(
            json.dumps({"texts": [{"id": 7, "text": "ok"}]}),
            encoding="utf-8",
        )
        validate_showtext_sidecar(chart_path, chart_path.name)

    def test_converter_rejects_missing_showtext_before_writing(self) -> None:
        source = self.temp / "missing_text.spc"
        source.write_text(
            "chart(120,4)\n"
            "showtext(100,7,6)\n",
            encoding="utf-8",
        )
        output = self.temp / "out" / "missing_text.spc"
        with self.assertRaises(ShowTextValidationError):
            converter.convert_file(source, "v1", output=output)
        self.assertFalse(output.exists())

    def test_legacy_packer_rejects_missing_showtext_before_writing(self) -> None:
        source = self.temp / "legacy_missing_text.spc"
        source.write_text(
            "chart(120,4)\n"
            "showtext(100,7,6)\n",
            encoding="utf-8",
        )
        output = self.temp / "out" / "legacy_missing_text.spc"
        with self.assertRaises(ShowTextValidationError):
            packer_main([
                "--spcV02spcV1",
                str(source),
                str(output),
            ])
        self.assertFalse(output.exists())

    def test_sidecar_respects_overwrite_and_preflights_all_outputs(self) -> None:
        source = self.temp / "source.spc"
        source.write_text("chart(120,4)\ntap(100,1,2)\n", encoding="utf-8")
        source.with_suffix(".text.json").write_text(
            '{"texts":[]}', encoding="utf-8")
        output = self.temp / "out" / "renamed.spc"
        output.parent.mkdir(parents=True)
        sidecar = output.with_suffix(".text.json")
        sidecar.write_text('{"texts":[]}', encoding="utf-8")

        with self.assertRaisesRegex(
                converter.ConversionError, "showtext 输出文件已存在"):
            converter.convert_file(
                source, "v1", output=output, overwrite=False)
        self.assertFalse(output.exists())
        self.assertEqual(
            sidecar.read_text(encoding="utf-8"),
            '{"texts":[]}',
        )

    def test_sidecar_copy_failure_leaves_no_chart(self) -> None:
        source = self.temp / "source_copy_fail.spc"
        source.write_text("chart(120,4)\ntap(100,1,2)\n", encoding="utf-8")
        source.with_suffix(".text.json").write_text(
            '{"texts":[]}', encoding="utf-8")
        output = self.temp / "out" / "copy_fail.spc"
        with mock.patch.object(
                converter.shutil,
                "copy2",
                side_effect=OSError("sidecar copy failed")):
            with self.assertRaisesRegex(
                    converter.ConversionError, "发布失败"):
                converter.convert_file(
                    source, "v1", output=output, overwrite=True)
        self.assertFalse(output.exists())
        self.assertFalse(output.with_suffix(".text.json").exists())

    def test_malformed_profile_is_reported(self) -> None:
        path = self.temp / "config_profiles.json"
        path.write_text("{ invalid", encoding="utf-8")
        profiles = load_profiles(path)
        self.assertIn("current_loader", profiles)
        self.assertIsNotNone(last_profile_error())

    def test_unknown_profile_placeholder_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            render_template({"value": "${not_defined}"}, {"song_title": "x"})

    def test_current_loader_profile_uses_schema_v1(self) -> None:
        profile = load_profiles()["current_loader"]
        self.assertEqual(profile.config_file, "if")
        self.assertEqual(profile.template["schemaVersion"], 1)
        self.assertEqual(profile.template["chart"][0], "${chart_file_0}")
        self.assertEqual(profile.template["sp"][0], "${section_0}")
        self.assertNotIn("audioFile", profile.template)

    def test_v0_input_domains_are_validated(self) -> None:
        invalid_cases = [
            "tap(100,1,6)",
            "hold(100,2,1,-5)",
            "flick(100,30,0,20,4)",
            "flick(100,30,100,20,5)",
            "skyarea(100,20,100,10,40,100,20,3,0,90,1)",
            "trackmove(100,0,0,0,0,0,0,4,10)",
            "lane(100,6,1)",
            "setcolor(100,1,#GG0000)",
            "timinggroup(536870912) {\n}",
        ]
        for statement in invalid_cases:
            with self.subTest(statement=statement):
                with self.assertRaises(ValueError):
                    self.parse("chart(120,4)\n" + statement + "\n")

    def test_standard_json_domains_shared_by_packer_and_converter(self) -> None:
        invalid = {
            "format": "ICP1",
            "version": 1,
            "bpm": 120.0,
            "meter": 4.0,
            "noteCount": 1,
            "eventCount": 0,
            "notes": [{
                "index": 0,
                "line": 0,
                "type": 1,
                "track": 1,
                "timeMs": 100,
                "endMs": 100,
                "fractions": [[6, 4], [6, 4], [1, 4], [1, 4]],
            }],
            "events": [],
        }
        with self.assertRaisesRegex(ValueError, "轨道"):
            pack_chart(invalid, "invalid.spc")
        with self.assertRaises(converter.ConversionError):
            converter.normalize_standard_json(invalid)

    def test_timinggroup_float_integer_boundary(self) -> None:
        accepted = self.parse(
            "chart(120,4)\n"
            "timinggroup(16777216) {\n"
            "tap(100,1,2)\n"
            "}\n"
        )
        self.assertEqual(accepted["noteCount"], 1)

        with self.assertRaisesRegex(ValueError, "float 精确整数范围"):
            self.parse(
                "chart(120,4)\n"
                "timinggroup(16777217) {\n"
                "tap(100,1,2)\n"
                "}\n"
            )

    def test_same_time_event_source_order_is_stable(self) -> None:
        chart = {
            "format": "ICP1",
            "version": 1,
            "bpm": 120.0,
            "meter": 4.0,
            "noteCount": 0,
            "eventCount": 3,
            "notes": [],
            "events": [
                {"id": 0, "type": 0, "timeMs": 100, "value": 1.25},
                {"id": 1, "type": 0, "timeMs": 100, "value": 0.75},
                {"id": 2, "type": 0, "timeMs": 100, "value": 1.5},
            ],
        }
        parsed = self.parse(serialize_v0(chart))
        self.assertEqual(
            [event["value"] for event in parsed["events"]],
            [1.25, 0.75, 1.5],
        )

    def test_standard_json_count_and_id_invariants(self) -> None:
        base = {
            "format": "ICP1",
            "version": 1,
            "bpm": 120.0,
            "meter": 4.0,
            "noteCount": 1,
            "eventCount": 1,
            "notes": [{
                "index": 0,
                "line": 0,
                "type": 1,
                "track": 1,
                "timeMs": 100,
                "endMs": 100,
                "fractions": [[1, 4], [1, 4], [1, 4], [1, 4]],
            }],
            "events": [{
                "id": 0,
                "type": 0,
                "timeMs": 100,
                "value": 1.0,
            }],
        }
        invalid_variants = []

        missing_count = json.loads(json.dumps(base))
        del missing_count["noteCount"]
        invalid_variants.append(missing_count)

        wrong_count = json.loads(json.dumps(base))
        wrong_count["eventCount"] = 2
        invalid_variants.append(wrong_count)

        duplicate_note_id = json.loads(json.dumps(base))
        duplicate_note_id["notes"].append(
            dict(duplicate_note_id["notes"][0]))
        duplicate_note_id["noteCount"] = 2
        invalid_variants.append(duplicate_note_id)

        wrong_event_id = json.loads(json.dumps(base))
        wrong_event_id["events"][0]["id"] = 7
        invalid_variants.append(wrong_event_id)

        for invalid in invalid_variants:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    pack_chart(invalid, "invalid.spc")

    def test_dict_when_false_is_dropped_recursively(self) -> None:
        template = {
            "keep": "ok",
            "remove": {
                "$when": False,
                "value": "must-not-leak",
            },
            "nested": {
                "keep": {"$when": True, "value": "yes"},
                "remove": {
                    "$when": False,
                    "value": "must-not-leak",
                },
            },
        }
        rendered = render_template(template, {})
        self.assertEqual(rendered["keep"], "ok")
        self.assertNotIn("remove", rendered)
        self.assertNotIn("remove", rendered["nested"])
        self.assertEqual(rendered["nested"]["keep"]["value"], "yes")

    def test_profile_labels_remain_unique(self) -> None:
        path = self.temp / "profiles.json"
        path.write_text(
            json.dumps({
                "profiles": {
                    "first": {
                        "description": "same",
                        "configFile": "a.json",
                        "template": {"value": "a"},
                    },
                    "second": {
                        "description": "same",
                        "configFile": "b.json",
                        "template": {"value": "b"},
                    },
                    "empty": {
                        "configFile": "c.json",
                        "template": {"value": "c"},
                    },
                }
            }),
            encoding="utf-8",
        )
        labels = profile_label_map(load_profiles(path))
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(set(labels.values()), {"current_loader", "legacy_collection",
                         "in_falsus_chart_loader", "simple_example",
                         "first", "second", "empty"})

    def test_v1_non_finite_event_values_are_not_normalized(self) -> None:
        nan64 = struct.unpack("<Q", struct.pack("<d", math.nan))[0]
        nan32 = struct.unpack("<I", struct.pack("<f", math.nan))[0]
        bpm = struct.unpack("<Q", struct.pack("<d", 120.0))[0]
        speed_group = struct.unpack("<I", struct.pack("<f", 1.0))[0]
        raw = {
            "version": 1,
            "u20": struct.unpack("<I", struct.pack("<f", 120.0))[0],
            "u24": struct.unpack("<I", struct.pack("<f", 4.0))[0],
            "notes": [],
            "events": [
                {
                    "id": 0, "time_ms": 0, "type": 0,
                    "value1": nan64, "value2": 0,
                },
                {
                    "id": 1, "time_ms": 0, "type": 1,
                    "value1": bpm, "value2": nan32,
                },
                {
                    "id": 2, "time_ms": 0, "type": 2,
                    "value1": bpm, "value2": nan32,
                },
                {
                    "id": 3, "time_ms": 0, "type": 5,
                    "value1": nan64, "value2": speed_group,
                },
            ],
        }
        standard = raw_to_standard(raw)
        self.assertTrue(math.isnan(standard["events"][0]["value"]))
        self.assertTrue(math.isnan(standard["events"][1]["speed"]))
        self.assertTrue(math.isnan(standard["events"][2]["speed"]))
        self.assertTrue(math.isnan(standard["events"][3]["value"]))
        with self.assertRaises(ChartValidationError):
            validate_standard_chart(standard)

    def _invalid_type_v1(self, path: Path) -> None:
        standard = {
            "format": "ICP1",
            "version": 1,
            "noteCount": 1,
            "eventCount": 0,
            "bpm": 120.0,
            "meter": 4.0,
            "notes": [
                {
                    "index": 0,
                    "id": 0,
                    "group": 0,
                    "timeMs": 1000,
                    "endMs": 1000,
                    "track": 1,
                    "type": 1,
                    "flags": 0,
                    "cipher": [0, 0, 0, 0],
                    "pairs": [0, 1, 0, 1, 1, 4, 1, 4],
                    "fractions": [[0, 1], [0, 1], [1, 4], [1, 4]],
                    "line": 0,
                }
            ],
            "events": [],
        }
        data = bytes(pack_chart(standard, path.name))
        raw = raw_decode(data, path.name)
        raw["notes"][0]["type"] = 9
        raw["notes"][0]["side_type_raw"] = (
            raw["notes"][0]["side"] | (9 << 8)
        )
        path.write_bytes(raw_encode(raw, path.name))

    def test_verify_rejects_invalid_v1_semantics(self) -> None:
        path = self.temp / "invalid-type.spc"
        self._invalid_type_v1(path)
        with self.assertRaisesRegex(
            converter.ConversionError,
            "输入域校验失败",
        ):
            converter.verify_path(path)

    def test_low_level_v1bin2json_rejects_invalid_semantics(self) -> None:
        path = self.temp / "invalid-low-level.spc"
        self._invalid_type_v1(path)
        output = self.temp / "invalid-low-level.json"
        with mock.patch.object(
            sys,
            "argv",
            [
                "spc_json_conv.py",
                "v1bin2json",
                str(path),
                path.name,
                str(output),
            ],
        ):
            with self.assertRaises(ChartValidationError):
                spc_json_conv.main()
        self.assertFalse(output.exists())

    def test_r4_setcolor_stress_chart_shape(self) -> None:
        chart_path = (
            Path(__file__).resolve().parents[2]
            / "output" / "test_charts" / "r4_setcolor_stress"
            / "R4_SetColor_Stress0.v0.txt"
        )
        chart = read_spc_v0_chart(str(chart_path))
        skyareas = [n for n in chart["notes"] if n["type"] == 5]
        setcolors = [
            n for n in chart["notes"]
            if n["type"] == 3 and ((n["flags"] >> 8) & 0xFFFFFF) == 0x434F4C
        ]
        taps = [n for n in chart["notes"] if n["type"] == 1]
        self.assertEqual(len(skyareas), 480)
        self.assertEqual(len(setcolors), 60)
        self.assertEqual(len(taps), 20)
        self.assertEqual(
            sorted({(n["line"] >> 32) & 0xFFFFFFFF for n in skyareas}),
            [0],
        )

    def test_difficulty_labels_match_game_enum(self) -> None:
        self.assertEqual(
            DIFFICULTY_SLOTS,
            (
                ("0", "MIN"),
                ("1", "EVO"),
                ("2", "ULT"),
                ("3", "FBD"),
            ),
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
ROOT = Path(__file__).resolve().parents[2]
TEST_CHARTS = ROOT / "testdata" / "charts"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import spc_converter as converter  # noqa: E402
from icp1_codec import decode  # noqa: E402
from spc_json_conv import raw_to_standard  # noqa: E402
from spc_v0_chart_decoder import read_spc_v0_chart  # noqa: E402


class ConverterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="spc-converter-tests-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_detect_formats(self) -> None:
        self.assertEqual(converter.detect_format(ROOT / "charts" / "alamode0.spc"), "v1")
        self.assertEqual(converter.detect_format(TEST_CHARTS / "test.spc"), "v0")

    def test_v1_to_v0_to_v1_preserves_judged_notes_when_repacking(self) -> None:
        source = ROOT / "charts" / "alamode0.spc"
        source_raw = decode(source.read_bytes(), "alamode0.spc")
        source_standard = raw_to_standard(source_raw)
        v0_dir = self.temp / "v0"
        v1_dir = self.temp / "v1"
        first = converter.convert_file(source, "v0", output_dir=v0_dir)
        second = converter.convert_file(Path(first.output), "v1", output_dir=v1_dir)
        raw = decode(Path(second.output).read_bytes(), "alamode0.spc")
        standard = raw_to_standard(raw)
        for note_type in (1, 2, 4):
            self.assertEqual(
                sum(n["type"] == note_type for n in standard["notes"]),
                sum(n["type"] == note_type for n in source_standard["notes"]),
            )
        self.assertEqual(
            sum(n["type"] == 5 for n in standard["notes"]),
            sum(n["type"] == 5 for n in source_standard["notes"]),
        )
        self.assertEqual(len(standard["events"]), 2)

    def test_v1_rekey_uses_new_filename(self) -> None:
        source = ROOT / "charts" / "alamode0.spc"
        target = self.temp / "renamed.spc"
        result = converter.convert_file(source, "v1", output=target)
        self.assertEqual(result.target_key, "renamed.spc")
        raw = decode(target.read_bytes(), "renamed.spc")
        self.assertEqual(len(raw["notes"]), 132)

    def test_json_roundtrip_preserves_group_speed(self) -> None:
        source = TEST_CHARTS / "demo_scenecontrol.spc"
        v1 = self.temp / "scene" / "demo_scenecontrol.spc"
        js = self.temp / "scene.json"
        back = self.temp / "scene_back" / "demo_scenecontrol.spc"
        converter.convert_file(source, "v1", output=v1)
        converter.convert_file(v1, "json", output=js)
        converter.convert_file(js, "v1", output=back)
        payload = json.loads(js.read_text(encoding="utf-8"))
        type5 = [item for item in payload["events"] if item["type"] == 5]
        self.assertTrue(type5)
        self.assertTrue(all("speed" in item for item in type5))
        raw = decode(back.read_bytes(), "demo_scenecontrol.spc")
        self.assertEqual(len(raw["events"]), len(payload["events"]))

    def test_sidecar_is_renamed_with_output(self) -> None:
        source = self.temp / "chart.spc"
        source.write_text("chart(120,4)\ntap(100,1,2)\n", encoding="utf-8")
        sidecar = self.temp / "chart.text.json"
        sidecar.write_text('{"texts":[]}', encoding="utf-8")
        output = self.temp / "out" / "chart.spc"
        converter.convert_file(source, "v1", output=output, copy_sidecars=True)
        self.assertTrue((self.temp / "out" / "chart.text.json").is_file())

    def test_long_sidecar_name_is_copied(self) -> None:
        source = self.temp / "long.spc"
        source.write_text("chart(120,4)\ntap(100,1,2)\n", encoding="utf-8")
        sidecar = self.temp / "long.spc.text.json"
        sidecar.write_text('{"texts":[]}', encoding="utf-8")
        output = self.temp / "out" / "long.spc"
        converter.convert_file(source, "v1", output=output, copy_sidecars=True)
        self.assertTrue((self.temp / "out" / "long.text.json").is_file())

    def test_folder_batch_skips_text_json(self) -> None:
        folder = self.temp / "input"
        folder.mkdir()
        shutil.copy2(TEST_CHARTS / "test.spc", folder / "test.spc")
        (folder / "test.text.json").write_text('{"texts":[]}', encoding="utf-8")
        files = converter.collect_inputs([str(folder)])
        self.assertEqual([p.name for p in files], ["test.spc"])

    def test_skyarea_v1_auto_grouping(self) -> None:
        chart = read_spc_v0_chart(
            str(Path(__file__).resolve().parent / "fixtures" / "skyarea_v1_grouping.spc")
        )
        areas = [note for note in chart["notes"] if note["type"] == 5]
        self.assertEqual([note["line"] for note in areas], [0, 0, 1, 1, 9, 10])

    def test_skyarea_v1_auto_grouping_survives_v1_conversion(self) -> None:
        fixture = Path(__file__).resolve().parent / "fixtures" / "skyarea_v1_grouping.spc"
        output = self.temp / "grouping" / "skyarea_v1_grouping.spc"
        converter.convert_file(fixture, "v1", output=output)
        standard = raw_to_standard(decode(output.read_bytes(), output.name))
        self.assertEqual(
            [note["line"] for note in standard["notes"] if note["type"] == 5],
            [0, 0, 1, 1, 9, 10],
        )

    def test_studio_ungrouped_skyarea_minus_one_uses_auto_grouping(self) -> None:
        source = self.temp / "studio.spc"
        source.write_text(
            "chart(120,4)\n"
            "skyarea(1000,50,100,10,50,100,10,0,0,500,-1)\n"
            "skyarea(1500,50,100,10,50,100,10,0,0,500,-1)\n"
            "skyarea(2000,50,100,10,50,100,10,0,0,500,7)\n",
            encoding="utf-8",
        )
        chart = read_spc_v0_chart(str(source))
        areas = [note for note in chart["notes"] if note["type"] == 5]
        self.assertEqual([note["line"] for note in areas], [0, 0, 7])


if __name__ == "__main__":
    unittest.main()

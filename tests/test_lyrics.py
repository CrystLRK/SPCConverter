from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import spc_converter as converter  # noqa: E402
from lyric_statement import (  # noqa: E402
    extract_from_content,
    parse_statement,
    sidecar_candidates,
)
from spc_v0_chart_decoder import read_spc_v0_chart  # noqa: E402
from song_package import (  # noqa: E402
    DifficultyInput,
    SongPackageSpec,
    ensure_default_pack,
    export_song_package,
)


class LyricStatementTests(unittest.TestCase):
    def test_quoted_text_keeps_commas_and_comment_markers(self) -> None:
        statement = parse_statement(
            'text(12000, "Hello, world // not a comment", side=right)'
        )
        self.assertIsNotNone(statement)
        assert statement is not None
        self.assertEqual(statement.time_ms, 12000.0)
        self.assertEqual(statement.text, "Hello, world // not a comment")
        self.assertEqual(statement.options["side"], "right")

    def test_fullwidth_syntax_and_unquoted_text(self) -> None:
        statement = parse_statement(
            "text（0:12:500，你好，世界，size=48，pos=10,20,300,80）"
        )
        self.assertIsNotNone(statement)
        assert statement is not None
        self.assertEqual(statement.time_ms, 12500.0)
        self.assertEqual(statement.text, "你好，世界")
        self.assertEqual(statement.options["size"], "48")
        self.assertEqual(statement.options["pos"], "10,20,300,80")

    def test_end_and_last_hold(self) -> None:
        statements = extract_from_content(
            'chart(120,4)\n'
            'text(1000, "A")\n'
            'text(2000, "B", end=5000)\n'
            'text(3000, "C")\n'
        )
        self.assertEqual([item.end_ms for item in statements], [2000.0, 5000.0, 9000.0])

    def test_same_time_lines_share_the_next_distinct_tick(self) -> None:
        statements = extract_from_content(
            'text(1000, "A")\n'
            'text(1000, "B")\n'
            'text(3000, "C")\n'
        )
        self.assertEqual(
            [item.end_ms for item in statements],
            [3000.0, 3000.0, 9000.0],
        )

    def test_decoder_translates_text_statement_to_showtext(self) -> None:
        statements = extract_from_content(
            'text(1000, "quoted, comma")'
        )
        self.assertEqual(len(statements), 1)
        with tempfile.TemporaryDirectory() as temp:
            chart = Path(temp) / "chart.spc"
            chart.write_text(
                'chart(120,4)\n'
                'tap(100,1,2)\n'
                'text(1000, "quoted, comma")\n',
                encoding="utf-8",
            )
            parsed = read_spc_v0_chart(str(chart))
            self.assertEqual(len(parsed["notes"]), 2)
            marker = parsed["notes"][1]
            self.assertEqual(marker["type"], 3)
            self.assertEqual(
                (int(marker["flags"]) >> 8) & 0xFFFFFF,
                0x545854,
            )
            self.assertEqual(int(marker["line"]), 1_000_000)


class LyricConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="spc-lyrics-tests-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_convert_file_writes_lyrics_sidecar(self) -> None:
        source = self.temp / "source.spc"
        source.write_text(
            'chart(120,4)\n'
            'tap(100,1,2)\n'
            'text(1000, "Line A", side=left)\n'
            'text(2500, "Line B", end=4000)\n',
            encoding="utf-8",
        )
        output = self.temp / "out" / "chart.spc"
        converter.convert_file(source, "v1", output=output)
        sidecar = output.with_suffix(".text.json")
        self.assertTrue(sidecar.is_file())
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["texts"]), 2)
        self.assertEqual(payload["texts"][0]["id"], 1_000_000)
        self.assertEqual(payload["texts"][0]["duration"], 1500.0)
        self.assertEqual(payload["texts"][1]["duration"], 1500.0)
        self.assertEqual(payload["texts"][0]["side"], "left")
        self.assertEqual(payload["texts"][0]["text"], "Line A")

    def test_convert_file_without_lyrics_removes_no_sidecar(self) -> None:
        source = self.temp / "plain.spc"
        source.write_text(
            'chart(120,4)\n'
            'tap(100,1,2)\n',
            encoding="utf-8",
        )
        output = self.temp / "out" / "plain.spc"
        converter.convert_file(source, "v1", output=output)
        self.assertFalse(output.with_suffix(".text.json").exists())

    def test_existing_sidecar_is_reused_for_binary_source(self) -> None:
        source = self.temp / "source.spc"
        source.write_text(
            'chart(120,4)\n'
            'tap(100,1,2)\n'
            'text(1000, "Line A")\n',
            encoding="utf-8",
        )
        binary = self.temp / "chart.spc"
        converter.convert_file(source, "v1", output=binary)
        copied = self.temp / "copy" / "chart.spc"
        converter.convert_file(binary, "v1", output=copied)
        sidecar = copied.with_suffix(".text.json")
        self.assertTrue(sidecar.is_file())
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(payload["texts"][0]["text"], "Line A")

    def test_package_keeps_lyrics_sidecar_and_sp(self) -> None:
        audio = (
            Path(__file__).resolve().parents[2]
            / "In Falsus 1.0.5"
            / "CustomCharts"
            / "User Charts"
            / "When_the_rain"
            / "audio.ogg"
        )
        source = self.temp / "lyrics_source.spc"
        source.write_text(
            'chart(120,4)\n'
            'tap(100,1,2)\n'
            'text(1000, "打包歌词")\n',
            encoding="utf-8",
        )
        output_root = self.temp / "Charts"
        ensure_default_pack(output_root)
        spec = SongPackageSpec(
            output_root=output_root,
            folder_name="lyric_song",
            chart_id="lyric_song",
            base_name="lyric_song",
            song_title="Lyric Song",
            artist="Tester",
            chart_designer="Tester",
            jacket_designer="Tester",
            audio=audio,
            jacket_large=None,
            jacket_small=None,
            difficulties=[
                DifficultyInput(0, source, 1, "EX"),
            ],
        )
        result = export_song_package(spec)
        sidecar = result.folder / "lyric_song0.text.json"
        self.assertTrue(sidecar.is_file())
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(payload["texts"][0]["text"], "打包歌词")
        config = json.loads(result.config.read_text(encoding="utf-8"))
        self.assertEqual(config["sp"][0], "EX")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from studio_project import (  # noqa: E402
    find_chart_name_conflicts,
    infer_internal_code,
    load_studio_project,
    load_studio_source,
    validate_internal_code,
)


class StudioProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="studio-project-tests-"))
        self.root = self.temp / "Testify"
        charts = self.root / ".ifstudio" / "Charts"
        charts.mkdir(parents=True)
        (self.root / "audio.ogg").write_bytes(b"OggS")
        (self.root / "jacket.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        chart_sources = {
            "0_Minimal.spc": (
                "chart(120,4)\n"
                "tap(100,1,2)\n"
                "skyarea(200,50,100,10,50,100,10,0,0,500,-1)\n"
            ),
            "1_Evolved.spc": "chart(100,4)\n",
            "2_Ultimate.spc": "chart(120,4)\ntap(300,2,1)\n",
            "3_Forbidden.spc": "chart(120,4)\ntap(400,3,1)\n",
        }
        for name, text in chart_sources.items():
            (charts / name).write_text(text, encoding="utf-8")
        manifest = {
            "format": "InFalsusStudioSong",
            "version": 1,
            "title": "Testify",
            "artist": "Composer",
            "audio": "audio.ogg",
            "audioOffsetMs": 0,
            "jacket": "jacket.png",
            "activeDifficulty": 2,
            "previewStartSeconds": 0,
            "previewEndSeconds": 0,
            "difficulties": [
                {
                    "index": index,
                    "name": name,
                    "rating": "?",
                    "designer": f"Designer {index}",
                    "jacketDesigner": f"Artist {index}",
                    "working": f".ifstudio/Charts/{index}_{name}.spc",
                }
                for index, name in enumerate(
                    ("Minimal", "Evolved", "Ultimate", "Forbidden")
                )
            ],
        }
        (self.root / "infalsus.studio.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_loads_metadata_empty_difficulty_and_studio_grouping(self) -> None:
        project = load_studio_project(self.root)
        self.assertEqual(project.title, "Testify")
        self.assertEqual(project.artist, "Composer")
        self.assertEqual(
            [item.index for item in project.non_empty_difficulties],
            [0, 2, 3],
        )
        self.assertEqual(project.active_difficulty, 2)
        self.assertEqual(project.author_defaults(), ("Designer 2", "Artist 2"))
        self.assertEqual(project.difficulties[0].rating, 0)
        self.assertEqual(project.difficulties[0].level_section_indicator, "?")

        spec = project.to_song_spec(
            "testify",
            self.temp / "output",
        )
        self.assertEqual(spec.song_title, "Testify")
        self.assertEqual(spec.folder_name, "testify")
        self.assertEqual(spec.chart_id, "testify")
        self.assertEqual([item.slot for item in spec.difficulties], [0, 2, 3])

    def test_internal_code_rules(self) -> None:
        self.assertEqual(infer_internal_code("Testify"), "testify")
        self.assertEqual(infer_internal_code("Song123"), "song123")
        self.assertEqual(infer_internal_code("When the rain"), "")
        self.assertEqual(infer_internal_code("符号"), "")
        self.assertEqual(validate_internal_code("my_song-2"), "my_song-2")
        with self.assertRaisesRegex(Exception, "内部代号"):
            validate_internal_code("bad name")

    def test_chart_name_conflicts_are_found_across_packs(self) -> None:
        charts_root = self.temp / "CustomCharts"
        first = charts_root / "Pack A" / "Song A"
        second = charts_root / "Pack B" / "Song B"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        (first / "testify0.spc").write_bytes(b"x")
        (second / "testify2.spc").write_bytes(b"x")
        conflicts = find_chart_name_conflicts(
            charts_root,
            "Testify",
            [0, 1, 2, 3],
        )
        self.assertEqual(
            [path.name for path in conflicts],
            ["testify0.spc", "testify2.spc"],
        )

    def test_missing_working_path_falls_back_to_unique_name_in_project(self) -> None:
        chart = self.root / ".ifstudio" / "Charts" / "2_Ultimate.spc"
        moved = self.root / "2_Ultimate.spc"
        shutil.move(str(chart), str(moved))
        project = load_studio_project(self.root)
        self.assertEqual(project.difficulties[2].chart, moved.resolve())

    def test_missing_working_path_becomes_empty_difficulty(self) -> None:
        chart = self.root / ".ifstudio" / "Charts" / "2_Ultimate.spc"
        chart.unlink()
        project = load_studio_project(self.root)
        self.assertTrue(project.difficulties[2].is_empty)
        self.assertEqual(project.difficulties[2].note_count, 0)
        self.assertEqual(
            [item.index for item in project.non_empty_difficulties],
            [0, 3],
        )

    def test_missing_audio_uses_unique_discovered_file(self) -> None:
        manifest_path = self.root / "infalsus.studio.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["audio"] = "missing/song.ogg"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        (self.root / "audio.ogg").unlink()
        discovered = self.root / "resources" / "found.mp3"
        discovered.parent.mkdir()
        discovered.write_bytes(b"ID3\4\0\0")
        observed = []

        def choose(configured, label, candidates):
            observed.append((configured, label, candidates))
            return candidates[0]

        project = load_studio_project(
            self.root,
            audio_chooser=choose,
        )
        self.assertEqual(project.audio, discovered.resolve())
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], "missing/song.ogg")

    def test_zip_project_with_extra_top_level_folder(self) -> None:
        archive_path = self.temp / "project.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            for source in self.root.rglob("*"):
                if source.is_file():
                    archive.write(
                        source,
                        Path("downloaded") / "project" / source.relative_to(self.root),
                    )
        project, session = load_studio_source(archive_path)
        self.assertIsNotNone(session)
        try:
            self.assertEqual(project.title, "Testify")
            self.assertEqual(
                [item.index for item in project.non_empty_difficulties],
                [0, 2, 3],
            )
            self.assertTrue(project.manifest.is_file())
            self.assertTrue(project.audio.is_file())
            self.assertTrue(project.difficulties[3].chart.is_file())
        finally:
            assert session is not None
            project_root = project.root
            session.close()
        self.assertFalse(project_root.exists())

    def test_zip_rejects_multiple_studio_projects(self) -> None:
        archive_path = self.temp / "ambiguous.zip"
        manifest = (self.root / "infalsus.studio.json").read_bytes()
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("first/infalsus.studio.json", manifest)
            archive.writestr("second/infalsus.studio.json", manifest)
        with self.assertRaisesRegex(Exception, "多个 Studio 工程"):
            load_studio_source(archive_path)


if __name__ == "__main__":
    unittest.main()

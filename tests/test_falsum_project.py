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

from falsum_project import load_falsum_project  # noqa: E402
from song_package import export_song_folder  # noqa: E402
from spc_converter import read_source  # noqa: E402
from spc_semantic import semantic_digest  # noqa: E402


class FalsumProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="falsum-project-tests-"))
        self.archive = self.temp / "song.falsum"
        song = {
            "format": "falsum",
            "version": 1,
            "songId": "zurevixen",
            "title": "Λzure Vixen",
            "artist": "Camellia",
            "background": "missing-game-background.png",
            "audio": {"file": "audio.ogg"},
            "cover": {"file": "cover.png"},
            "difficulties": [
                {
                    "index": index,
                    "name": name,
                    "available": index == 3,
                    "chart": f"charts/{name.lower()}.chart.txt",
                    "rating": 1 if index != 3 else 14,
                    "chartDesigner": "",
                    "jacketDesigner": "",
                }
                for index, name in enumerate(("MIN", "EVO", "ULT", "FBD"))
            ],
        }
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr(
                "song.json",
                json.dumps(song, ensure_ascii=False),
            )
            archive.writestr("audio.ogg", b"OggS")
            archive.writestr("cover.png", b"\x89PNG\r\n\x1a\n")
            archive.writestr(
                "charts/fbd.chart.txt",
                "chart(163,4)\n"
                "track(1000,0.5)\n"
                "skyarea(1104,24,48,8,24,48,1,0,0,46,-1)\n"
                "tap(1200,1,2)\n",
            )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_parses_song_metadata_and_only_available_difficulty(self) -> None:
        session = load_falsum_project(self.archive)
        self.addCleanup(session.close)
        project = session.project
        self.assertEqual(project.title, "Λzure Vixen")
        self.assertEqual(project.source_code_hint, "zurevixen")
        self.assertEqual(project.artist, "Camellia")
        self.assertIsNone(project.background)
        self.assertEqual(
            [item.index for item in project.non_empty_difficulties],
            [3],
        )
        self.assertEqual(project.difficulties[3].rating, 14)
        self.assertEqual(project.difficulties[3].note_count, 2)
        self.assertEqual(project.difficulties[3].event_count, 1)

    def test_rejects_zip_path_traversal(self) -> None:
        bad = self.temp / "bad.falsum"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("../song.json", "{}")
        with self.assertRaisesRegex(Exception, "非法路径"):
            load_falsum_project(bad)

    def test_missing_audio_uses_discovered_file(self) -> None:
        archive_path = self.temp / "missing-audio.falsum"
        song = {
            "format": "falsum",
            "version": 1,
            "songId": "missing_audio",
            "title": "Missing Audio",
            "artist": "Tester",
            "audio": {"file": "audio/missing.ogg"},
            "difficulties": [
                {
                    "index": 3,
                    "name": "FBD",
                    "available": True,
                    "chart": "charts/fbd.chart.txt",
                    "rating": 1,
                    "chartDesigner": "",
                    "jacketDesigner": "",
                }
            ],
        }
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("song.json", json.dumps(song))
            archive.writestr("resources/found.ogg", b"OggS\0\0\0\0")
            archive.writestr(
                "charts/fbd.chart.txt",
                "chart(120,4)\ntap(100,1,2)\n",
            )
        observed = []

        def choose(configured, label, candidates):
            observed.append((configured, label, candidates))
            return candidates[0]

        session = load_falsum_project(
            archive_path,
            audio_chooser=choose,
        )
        try:
            self.assertEqual(
                session.project.audio.name,
                "found.ogg",
            )
            self.assertEqual(len(observed), 1)
        finally:
            session.close()

    def test_available_chart_exports_without_semantic_change(self) -> None:
        session = load_falsum_project(self.archive)
        try:
            project = session.project
            spec = project.to_song_spec(
                project.source_code_hint,
                self.temp / "output",
            )
            result = export_song_folder(spec)
            source_chart = project.difficulties[3].chart
            self.assertIsNotNone(source_chart)
            self.assertEqual(
                semantic_digest(read_source(source_chart).standard),
                semantic_digest(read_source(result.charts[0]).standard),
            )
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import tkinter as tk
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pack_library import GameInstall  # noqa: E402
from studio_project_tab import StudioProjectTab  # noqa: E402


class StudioProjectTabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="studio-tab-tests-"))
        self.project_root = self.temp / "Studio"
        charts = self.project_root / ".ifstudio" / "Charts"
        charts.mkdir(parents=True)
        (self.project_root / "audio.ogg").write_bytes(b"OggS")
        (self.project_root / "jacket.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        for index, name in enumerate(
            ("Minimal", "Evolved", "Ultimate", "Forbidden")
        ):
            body = "chart(120,4)\ntap(100,1,2)\n" if index != 1 else "chart(100,4)\n"
            (charts / f"{index}_{name}.spc").write_text(body, encoding="utf-8")
        manifest = {
            "format": "InFalsusStudioSong",
            "version": 1,
            "title": "Testify",
            "artist": "Composer",
            "audio": "audio.ogg",
            "audioOffsetMs": 0,
            "jacket": "jacket.png",
            "activeDifficulty": 3,
            "previewStartSeconds": 0,
            "previewEndSeconds": 0,
            "difficulties": [
                {
                    "index": index,
                    "name": name,
                    "rating": "15" if index == 3 else "?",
                    "designer": f"Designer {index}",
                    "jacketDesigner": f"Artist {index}",
                    "working": f".ifstudio/Charts/{index}_{name}.spc",
                }
                for index, name in enumerate(
                    ("Minimal", "Evolved", "Ultimate", "Forbidden")
                )
            ],
        }
        (self.project_root / "infalsus.studio.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        self.game_root = self.temp / "In Falsus"
        self.charts_root = self.game_root / "CustomCharts"
        self.charts_root.mkdir(parents=True)
        install = GameInstall("3971950", self.game_root, self.charts_root)
        self.root = tk.Tk()
        self.root.withdraw()
        with mock.patch(
            "gui.discover_game_installs",
            return_value=(install,),
        ), mock.patch(
            "gui.load_preferred_game_root",
            return_value=None,
        ):
            self.tab = StudioProjectTab(self.root)
        self.root.update_idletasks()

    def tearDown(self) -> None:
        self.root.destroy()
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_project_autofills_editable_fields(self) -> None:
        self.tab.project_path.set(str(self.project_root))
        self.tab.load_project()
        self.assertEqual(self.tab.vars["song_title"].get(), "Testify")
        self.assertEqual(self.tab.vars["internal_code"].get(), "testify")
        self.assertEqual(self.tab.vars["chart_designer"].get(), "Designer 3")
        self.assertTrue(self.tab.diffs[0]["include"].get())
        self.assertFalse(self.tab.diffs[1]["include"].get())
        self.assertEqual(self.tab.diffs[0]["rating"].get(), "0")
        self.assertEqual(self.tab.diffs[0]["indicator"].get(), "?")
        self.assertEqual(self.tab.diffs[3]["rating"].get(), "15")

    def test_manual_edits_are_used_in_spec(self) -> None:
        self.tab.project_path.set(str(self.project_root))
        self.tab.load_project()
        self.tab.vars["internal_code"].set("my_test")
        self.tab.vars["song_title"].set("Edited Title")
        self.tab.vars["song_background"].set(
            str(self.project_root / "jacket.png")
        )
        self.tab.diffs[0]["background"].set(
            str(self.project_root / "jacket.png")
        )
        self.tab.diffs[0]["rating"].set("7")
        self.tab.diffs[0]["indicator"].set("R7")
        spec = self.tab._edited_spec(self.temp / "out", "User Charts")
        self.assertEqual(spec.folder_name, "my_test")
        self.assertEqual(spec.song_title, "Edited Title")
        self.assertEqual(spec.background, self.project_root / "jacket.png")
        self.assertEqual(
            spec.difficulties[0].background,
            self.project_root / "jacket.png",
        )
        self.assertEqual(spec.difficulties[0].rating, 7)
        self.assertEqual(spec.difficulties[0].level_section_indicator, "R7")
        self.assertEqual(
            [item.slot for item in spec.difficulties],
            [0, 2, 3],
        )

    def test_zip_project_autofills_through_extra_folder(self) -> None:
        archive_path = self.temp / "studio-project.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            for source in self.project_root.rglob("*"):
                if source.is_file():
                    archive.write(
                        source,
                        Path("outer") / "inner" / source.relative_to(
                            self.project_root
                        ),
                    )
        self.tab.project_path.set(str(archive_path))
        self.tab.load_project()
        self.assertEqual(self.tab.vars["song_title"].get(), "Testify")
        self.assertEqual(self.tab.diffs[3]["rating"].get(), "15")
        self.assertIsNotNone(self.tab._source_session)

    def test_single_discovered_audio_is_reported_and_selected(self) -> None:
        candidate = self.project_root / "audio.ogg"
        with mock.patch(
            "studio_project_tab.messagebox.showinfo"
        ) as showinfo:
            selected = self.tab._choose_discovered_audio(
                "old/audio.ogg",
                "歌曲音频",
                (candidate,),
            )
        self.assertEqual(selected, candidate)
        showinfo.assert_called_once()

    def test_import_confirmation_contains_internal_code(self) -> None:
        pack = SimpleNamespace(
            title="User Charts",
            folder=self.charts_root / "User Charts",
        )
        spec = SimpleNamespace(
            chart_id="my_code",
            folder_name="my_code",
        )
        with mock.patch(
            "studio_project_tab.messagebox.askokcancel",
            return_value=False,
        ) as confirm:
            accepted = self.tab._confirm_game_import(pack, spec)
        self.assertFalse(accepted)
        message = confirm.call_args.args[1]
        self.assertIn("my_code", message)
        self.assertIn("User Charts", message)

    def test_import_confirmation_lists_overwrite_conflicts(self) -> None:
        pack = SimpleNamespace(
            title="User Charts",
            folder=self.charts_root / "User Charts",
        )
        spec = SimpleNamespace(
            chart_id="my_code",
            folder_name="my_code",
        )
        conflicts = [Path("my_code2.spc"), Path("my_code3.spc")]
        with mock.patch(
            "studio_project_tab.messagebox.askokcancel",
            return_value=False,
        ) as confirm:
            accepted = self.tab._confirm_game_import(
                pack,
                spec,
                conflicts,
            )
        self.assertFalse(accepted)
        message = confirm.call_args.args[1]
        self.assertIn("my_code2.spc", message)
        self.assertIn("my_code3.spc", message)
        self.assertIn("覆盖", message)

    def test_cancelled_import_creates_no_pack_or_thread(self) -> None:
        self.tab.project_path.set(str(self.project_root))
        self.tab.load_project()
        self.tab.vars["output_root"].set(str(self.charts_root))
        default_pack = self.charts_root / "User Charts"
        with mock.patch(
            "studio_project_tab.messagebox.askokcancel",
            return_value=False,
        ), mock.patch(
            "studio_project_tab.threading.Thread.start"
        ) as start:
            self.tab.start_export("game")
        start.assert_not_called()
        self.assertFalse(default_pack.exists())


if __name__ == "__main__":
    unittest.main()

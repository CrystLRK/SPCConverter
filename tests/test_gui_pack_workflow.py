from __future__ import annotations

import shutil
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import gui  # noqa: E402
from pack_library import GameInstall, scan_pack_directories  # noqa: E402


class GuiPackWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="ifce-gui-pack-"))
        self.game_root = self.temp / "In Falsus"
        self.charts_root = self.game_root / "Charts"
        self.game_root.mkdir()
        install = GameInstall(
            "3971950",
            self.game_root,
            self.charts_root,
        )
        self.root = tk.Tk()
        self.root.withdraw()
        with mock.patch.object(
            gui,
            "discover_game_installs",
            return_value=(install,),
        ), mock.patch.object(gui, "load_preferred_game_root", return_value=None):
            self.tab = gui.SongPackageTab(self.root)
        self.root.update_idletasks()

    def tearDown(self) -> None:
        self.root.destroy()
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_no_pack_creates_default_after_popup(self) -> None:
        with mock.patch.object(gui.messagebox, "showinfo") as showinfo:
            pack = self.tab._pack_for_export()
        self.assertTrue(showinfo.called)
        self.assertEqual(pack.folder.name, "User Charts")
        self.assertTrue(
            (self.charts_root / "User Charts" / "pack.json").is_file()
        )

    def test_create_pack_control_creates_and_selects_pack(self) -> None:
        self.tab.vars["pack_folder_name"].set("Created Pack")
        self.tab.vars["pack_id"].set("created-pack")
        self.tab.vars["pack_title"].set("Created Pack")
        with mock.patch.object(gui.messagebox, "showinfo"):
            self.tab.create_pack()
        scan = scan_pack_directories(self.charts_root)
        self.assertEqual([pack.pack_id for pack in scan.packs], ["created-pack"])
        self.assertEqual(self.tab._selected_pack().folder.name, "Created Pack")

    def test_external_profile_shows_warning(self) -> None:
        label = next(
            label
            for label, key in self.tab.profiles_by_label.items()
            if key == "in_falsus_chart_loader"
        )
        self.tab.profile_label.set(label)
        self.tab._profile_changed()
        text = self.tab.profile_description.cget("text")
        self.assertIn("InFalsusChartLoader", text)

    def test_malformed_profile_mapping_shows_popup(self) -> None:
        profiles = self.tab.profiles
        with mock.patch.object(
            gui,
            "load_profiles",
            return_value=profiles,
        ), mock.patch.object(
            gui,
            "last_profile_error",
            return_value="broken profile json",
        ), mock.patch.object(gui.messagebox, "showerror") as showerror:
            self.tab._reload_profiles()
        self.assertTrue(showerror.called)
        self.assertIn("broken profile json", showerror.call_args.args[1])


if __name__ == "__main__":
    unittest.main()

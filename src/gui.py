from __future__ import annotations

import os
import queue
import re
import sys
import threading
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from config_profiles import (
    CURRENT_CONFIG_FILE,
    find_profile_file,
    last_profile_error,
    load_profiles,
    profile_label_map,
)
from pack_library import (
    DEFAULT_PACK_FOLDER,
    DEFAULT_PACK_ID,
    DEFAULT_PACK_STYLE,
    DEFAULT_PACK_TITLE,
    PackDirectory,
    discover_game_installs,
    load_preferred_game_root,
    save_preferred_game_root,
    scan_pack_directories,
)
from song_package import (
    CURRENT_PROFILE,
    DIFFICULTY_SLOTS,
    DifficultyInput,
    SongPackageSpec,
    create_pack_folder,
    ensure_default_pack,
    export_song_package,
)
from spc_converter import (
    FORMAT_NAMES,
    batch_convert,
    collect_inputs,
    default_output_dir,
    executable_dir,
)


SLOT_LABEL = {int(slot): name for slot, name in DIFFICULTY_SLOTS}


class ScrollFrame(ttk.Frame):
    """Vertical scrolling frame for smaller screens."""

    def __init__(self, master: tk.Misc, **kwargs):
        super().__init__(master, **kwargs)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas)
        self.body.bind(
            "<Configure>",
            lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")
        self.canvas.bind(
            "<Configure>",
            lambda event: self.canvas.itemconfigure(self.window, width=event.width),
        )
        self.canvas.bind_all("<MouseWheel>", self._wheel, add="+")

    def _wheel(self, event: tk.Event) -> None:
        if self.winfo_containing(event.x_root, event.y_root) is None:
            return
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class SongPackageTab(ttk.Frame):
    def __init__(self, master: tk.Misc):
        super().__init__(master, padding=12)
        self.vars: dict[str, tk.StringVar] = {}
        self.diffs: list[dict[str, tk.StringVar]] = []
        self.result_queue: queue.Queue[tuple[bool, object]] = queue.Queue()
        self.profiles = {}
        self.profiles_by_label: dict[str, str] = {}
        self.profile_label = tk.StringVar()
        self.profile_description: ttk.Label | None = None
        self.game_installs = ()
        self.game_install_by_label = {}
        self.game_label = tk.StringVar()
        self.pack_by_label: dict[str, PackDirectory] = {}
        self.pack_label = tk.StringVar()
        self._build()
        self._initialize_game_install()
        self._reload_profiles(initial=True)
        self.refresh_pack_list(show_errors=False)

    def _var(self, key: str, value: str = "") -> tk.StringVar:
        var = tk.StringVar(value=value)
        self.vars[key] = var
        return var

    def _build(self) -> None:
        info = ttk.LabelFrame(self, text="歌曲信息", padding=10)
        info.pack(fill="x")
        fields = [
            ("song_title", "歌曲标题", 0, 0),
            ("artist", "艺术家", 0, 2),
            ("chart_designer", "谱师", 1, 0),
            ("jacket_designer", "封面设计", 1, 2),
            ("folder_name", "导出文件夹名", 2, 0),
            ("chart_id", "Chart ID / 谱面标识", 2, 2),
            ("base_name", "Base Name（新结构忽略）", 3, 0),
            ("template_song_id", "模板 ID（新结构忽略）", 3, 2),
        ]
        for key, label, row, col in fields:
            ttk.Label(info, text=label).grid(row=row, column=col, sticky="w", padx=(0, 6), pady=4)
            ttk.Entry(info, textvariable=self._var(key), width=38).grid(
                row=row, column=col + 1, sticky="ew", pady=4
            )
        self.vars["template_song_id"].set("10")
        ttk.Label(info, text="旧游戏背景（忽略）").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(info, textvariable=self._var("gameplay_background"), width=12).grid(
            row=4, column=1, sticky="w", pady=4
        )
        self.vars["gameplay_background"].set("")
        ttk.Label(info, text="旧角色标识（忽略）").grid(row=4, column=2, sticky="w", pady=4)
        ttk.Entry(info, textvariable=self._var("character_identifier"), width=28).grid(
            row=4, column=3, sticky="ew", pady=4
        )
        ttk.Label(info, text="预览开始（秒）").grid(
            row=5, column=0, sticky="w", pady=4
        )
        ttk.Entry(
            info, textvariable=self._var("preview_start", "0"), width=12
        ).grid(row=5, column=1, sticky="w", pady=4)
        ttk.Label(info, text="预览结束（秒，可空）").grid(
            row=5, column=2, sticky="w", pady=4
        )
        ttk.Entry(
            info, textvariable=self._var("preview_end", ""), width=12
        ).grid(row=5, column=3, sticky="w", pady=4)
        info.columnconfigure(1, weight=1)
        info.columnconfigure(3, weight=1)

        paths = ttk.LabelFrame(self, text="资源与输出", padding=10)
        paths.pack(fill="x", pady=(10, 0))
        rows = [
            ("audio", "歌曲音频（必填）", "audio"),
            ("preview_audio", "独立预览音频（可空）", "audio"),
            ("song_background", "歌曲默认背景（可空）", "background"),
            ("jacket_large", "封面大图 PNG（可空，自动生成）", "image"),
            ("jacket_small", "封面小图 PNG（可空，自动生成）", "image"),
        ]
        for row, (key, label, browse_kind) in enumerate(rows):
            ttk.Label(paths, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(paths, textvariable=self._var(key)).grid(
                row=row, column=1, sticky="ew", pady=4, padx=(6, 6)
            )
            ttk.Button(
                paths,
                text="浏览…",
                command=lambda k=key, kind=browse_kind: self.browse(k, kind),
            ).grid(row=row, column=2, pady=4)
        paths.columnconfigure(1, weight=1)

        pack_frame = ttk.LabelFrame(self, text="曲包目录", padding=10)
        pack_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(pack_frame, text="Steam 正式版").grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=4
        )
        self.game_combo = ttk.Combobox(
            pack_frame,
            textvariable=self.game_label,
            state="readonly",
        )
        self.game_combo.grid(row=0, column=1, columnspan=3, sticky="ew", pady=4)
        self.game_combo.bind("<<ComboboxSelected>>", self._game_install_changed)

        ttk.Label(pack_frame, text="输出根目录").grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Entry(
            pack_frame,
            textvariable=self._var("output_root"),
        ).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(
            pack_frame,
            text="浏览…",
            command=lambda: self.browse("output_root", "directory"),
        ).grid(row=1, column=2, padx=6, pady=4)
        ttk.Button(
            pack_frame,
            text="刷新曲包",
            command=self.refresh_pack_list,
        ).grid(row=1, column=3, pady=4)

        ttk.Label(pack_frame, text="现有曲包").grid(
            row=2, column=0, sticky="w", padx=(0, 6), pady=4
        )
        self.pack_combo = ttk.Combobox(
            pack_frame,
            textvariable=self.pack_label,
            state="readonly",
        )
        self.pack_combo.grid(row=2, column=1, columnspan=3, sticky="ew", pady=4)
        pack_frame.columnconfigure(1, weight=1)

        create_pack = ttk.LabelFrame(
            pack_frame,
            text="创建曲包文件夹",
            padding=8,
        )
        create_pack.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        pack_fields = [
            ("pack_folder_name", "曲包文件夹", "User Charts"),
            ("pack_id", "曲包 ID", "user-charts"),
            ("pack_title", "曲包标题", "User Charts"),
            ("pack_artist", "曲包艺术家", ""),
            ("style_pack_slug", "游戏曲包样式", "act-4"),
            ("pack_sort_order", "曲包排序", "0"),
        ]
        for index, (key, label, default) in enumerate(pack_fields):
            row = index // 2
            col = (index % 2) * 2
            ttk.Label(create_pack, text=label).grid(
                row=row, column=col, sticky="w", padx=(0, 6), pady=4
            )
            ttk.Entry(
                create_pack, textvariable=self._var(key, default), width=30
            ).grid(row=row, column=col + 1, sticky="ew", pady=4)
        ttk.Label(create_pack, text="曲包封面（可空）").grid(
            row=3, column=0, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Entry(
            create_pack,
            textvariable=self._var("pack_jacket"),
        ).grid(row=3, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Button(
            create_pack,
            text="浏览…",
            command=lambda: self.browse("pack_jacket", "image"),
        ).grid(row=3, column=3, padx=(6, 0), pady=4)
        ttk.Button(
            create_pack,
            text="创建曲包文件夹",
            command=self.create_pack,
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        create_pack.columnconfigure(1, weight=1)
        create_pack.columnconfigure(3, weight=1)

        profile = ttk.LabelFrame(self, text="配置结构", padding=10)
        profile.pack(fill="x", pady=(10, 0))
        ttk.Label(profile, text="模板结构").grid(row=0, column=0, sticky="w")
        self.profile_combo = ttk.Combobox(
            profile,
            textvariable=self.profile_label,
            state="readonly",
        )
        self.profile_combo.grid(row=0, column=1, sticky="ew", padx=6)
        self.profile_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._profile_changed(),
        )
        ttk.Button(
            profile,
            text="打开配置映射",
            command=self.open_profile_file,
        ).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(
            profile,
            text="重载映射",
            command=self.reload_profiles,
        ).grid(row=0, column=3)
        self.profile_description = ttk.Label(
            profile,
            text="",
            foreground="#666666",
        )
        self.profile_description.grid(
            row=1,
            column=0,
            columnspan=4,
            sticky="w",
            pady=(6, 0),
        )
        profile.columnconfigure(1, weight=1)

        diff_frame = ttk.LabelFrame(self, text="谱面与难度（0=MIN，1=EVO，2=ULT，3=FBD）", padding=10)
        diff_frame.pack(fill="x", pady=(10, 0))
        headers = ("槽位", "谱面文件", "选择", "Rating", "Section", "游戏内背景", "选择背景")
        for col, text in enumerate(headers):
            ttk.Label(diff_frame, text=text).grid(row=0, column=col, sticky="w", padx=(0, 6))
        for slot in range(4):
            vars_ = {
                "chart": tk.StringVar(),
                "rating": tk.StringVar(value="1"),
                "indicator": tk.StringVar(value="1"),
                "background": tk.StringVar(),
            }
            self.diffs.append(vars_)
            row = slot + 1
            ttk.Label(diff_frame, text=SLOT_LABEL[slot]).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(diff_frame, textvariable=vars_["chart"]).grid(
                row=row, column=1, sticky="ew", pady=3, padx=(0, 6)
            )
            ttk.Button(
                diff_frame,
                text="选择谱面",
                command=lambda s=slot: self.browse_diff(s),
            ).grid(row=row, column=2, sticky="w", pady=3)
            ttk.Entry(diff_frame, textvariable=vars_["rating"], width=7).grid(
                row=row, column=3, sticky="w", padx=(6, 6), pady=3
            )
            ttk.Entry(diff_frame, textvariable=vars_["indicator"], width=12).grid(
                row=row, column=4, sticky="w", pady=3
            )
            ttk.Entry(diff_frame, textvariable=vars_["background"]).grid(
                row=row, column=5, sticky="ew", pady=3, padx=(6, 6)
            )
            ttk.Button(
                diff_frame,
                text="选择背景",
                command=lambda s=slot: self.browse_background(s),
            ).grid(row=row, column=6, sticky="w", pady=3)
        diff_frame.columnconfigure(1, weight=1)
        diff_frame.columnconfigure(5, weight=1)

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(12, 8))
        self.export_button = ttk.Button(
            actions,
            text="导出歌曲到所选曲包",
            command=self.export,
        )
        self.export_button.pack(side="left")
        ttk.Button(actions, text="填充标识", command=self.fill_identity).pack(side="left", padx=8)
        self.open_after = tk.BooleanVar(value=True)
        ttk.Checkbutton(actions, text="完成后打开输出目录", variable=self.open_after).pack(side="left")
        ttk.Button(actions, text="打开输出目录", command=self.open_output).pack(side="right")

        self.log_text = tk.Text(self, height=9, wrap="word")
        self.log_text.pack(fill="both", expand=True, pady=(2, 0))

        self.vars["output_root"].set(
            str(executable_dir() / "output" / "CustomCharts")
        )

    def _initialize_game_install(self) -> None:
        installs = discover_game_installs()
        self.game_installs = installs
        self.game_install_by_label = {
            install.label: install for install in installs
        }
        labels = list(self.game_install_by_label)
        self.game_combo.configure(values=labels)
        if not installs:
            self.game_label.set("未检测到 Steam 正式版")
            self.log("[信息] 未检测到 Steam 正式版，使用手动输出目录。")
            return

        preferred = load_preferred_game_root()
        selected = next(
            (
                install
                for install in installs
                if preferred is not None and install.install_dir == preferred
            ),
            installs[0],
        )
        self.game_label.set(selected.label)
        self.vars["output_root"].set(str(selected.charts_root))

    def _game_install_changed(self, _event: tk.Event | None = None) -> None:
        install = self.game_install_by_label.get(self.game_label.get())
        if install is None:
            return
        self.vars["output_root"].set(str(install.charts_root))
        try:
            save_preferred_game_root(install.install_dir)
        except Exception as exc:
            self.log(f"[警告] 无法保存上次游戏目录：{exc}")
        self.refresh_pack_list(show_errors=False)

    def refresh_pack_list(
        self,
        show_errors: bool = True,
        preferred_folder: str | None = None,
    ) -> None:
        root_text = self.vars["output_root"].get().strip()
        if not root_text:
            root = Path.cwd()
        else:
            root = Path(root_text)
        try:
            scan = scan_pack_directories(root)
        except Exception as exc:
            if show_errors:
                messagebox.showerror("曲包扫描失败", str(exc), parent=self)
            self.log(f"[失败] 曲包扫描失败：{exc}")
            return

        previous = self.pack_by_label.get(self.pack_label.get())
        desired_folder = (
            preferred_folder
            or (previous.folder.name if previous is not None else None)
        )
        self.pack_by_label = {pack.label: pack for pack in scan.packs}
        labels = list(self.pack_by_label)
        self.pack_combo.configure(values=labels)
        selected_label = ""
        if desired_folder:
            selected_label = next(
                (
                    label
                    for label, pack in self.pack_by_label.items()
                    if pack.folder.name.casefold() == desired_folder.casefold()
                ),
                "",
            )
        if not selected_label and labels:
            selected_label = labels[0]
        self.pack_label.set(selected_label)

        for item in scan.invalid:
            self.log(f"[警告] 曲包不可用：{item.folder}：{item.reason}")
        if labels:
            self.log(f"[信息] 检测到 {len(labels)} 个可用曲包。")
        else:
            self.log("[信息] 当前输出目录下没有可用曲包。")
        if scan.invalid and show_errors:
            details = "\n".join(
                f"{item.folder.name}: {item.reason}"
                for item in scan.invalid[:8]
            )
            if len(scan.invalid) > 8:
                details += f"\n……另有 {len(scan.invalid) - 8} 个无效曲包"
            messagebox.showerror(
                "存在无效曲包",
                details,
                parent=self,
            )

    def _selected_pack(self) -> PackDirectory | None:
        return self.pack_by_label.get(self.pack_label.get())

    def _pack_for_export(self) -> PackDirectory:
        pack = self._selected_pack()
        if pack is not None:
            return pack
        root = Path(
            self.vars["output_root"].get().strip()
            or executable_dir() / "output" / "CustomCharts"
        )
        try:
            scan = scan_pack_directories(root)
        except Exception as exc:
            self.log(f"[失败] 曲包扫描失败：{exc}")
            messagebox.showerror("曲包扫描失败", str(exc), parent=self)
            raise
        if scan.packs:
            self.refresh_pack_list(show_errors=False)
            return scan.packs[0]
        messagebox.showinfo(
            "未检测到曲包",
            "未检测到曲包，将创建默认曲包。",
            parent=self,
        )
        self.log("[信息] 未检测到曲包，创建默认曲包 User Charts。")
        try:
            pack, _created = ensure_default_pack(root)
        except Exception as exc:
            messagebox.showerror("创建默认曲包失败", str(exc), parent=self)
            raise
        self.refresh_pack_list(
            show_errors=True,
            preferred_folder=pack.folder.name,
        )
        return pack

    def create_pack(self) -> None:
        try:
            sort_order = int(
                self.vars["pack_sort_order"].get().strip() or "0"
            )
            output_root = Path(
                self.vars["output_root"].get().strip()
                or executable_dir() / "output" / "CustomCharts"
            )
            jacket_text = self.vars["pack_jacket"].get().strip()
            pack = create_pack_folder(
                output_root,
                self.vars["pack_folder_name"].get().strip() or "User Charts",
                self.vars["pack_id"].get().strip() or "user-charts",
                self.vars["pack_title"].get().strip() or "User Charts",
                artist=self.vars["pack_artist"].get().strip(),
                style_pack_slug=(
                    self.vars["style_pack_slug"].get().strip() or "act-4"
                ),
                sort_order=sort_order,
                jacket=Path(jacket_text) if jacket_text else None,
            )
        except Exception as exc:
            self.log(f"[失败] 创建曲包失败：{exc}")
            messagebox.showerror("创建曲包失败", str(exc), parent=self)
            return
        self.refresh_pack_list(
            show_errors=True,
            preferred_folder=pack.folder.name,
        )
        self.log(f"[完成] 已创建曲包：{pack.folder}")
        messagebox.showinfo(
            "曲包已创建",
            f"曲包文件夹：{pack.folder}",
            parent=self,
        )

    def _reload_profiles(self, initial: bool = False) -> None:
        current_key = self.profiles_by_label.get(self.profile_label.get())
        profiles = load_profiles()
        error = last_profile_error()
        self.profiles = profiles
        self.profiles_by_label = profile_label_map(profiles)
        labels = list(self.profiles_by_label)
        self.profile_combo.configure(values=labels)
        selected = next(
            (
                label
                for label, key in self.profiles_by_label.items()
                if key == (current_key or CURRENT_PROFILE)
            ),
            labels[0] if labels else "",
        )
        self.profile_label.set(selected)
        self._profile_changed()
        if error:
            self.log(f"[失败] 配置映射加载失败：{error}")
            messagebox.showerror(
                "配置映射无效",
                error + "\n\n已回退到内置模板。",
                parent=self,
            )
        elif not initial:
            self.log("[信息] 配置映射已重新加载。")

    def _profile_changed(self) -> None:
        if self.profile_description is None:
            return
        key = self.profiles_by_label.get(self.profile_label.get(), "")
        profile = self.profiles.get(key)
        if profile is None:
            self.profile_description.configure(text="")
            return
        text = f"配置文件名：{profile.config_file}    {profile.description}"
        external = profile.config_file != CURRENT_CONFIG_FILE
        if external:
            text += "\n外部加载器兼容模板；当前 Mod 不会读取该配置文件名。"
        self.profile_description.configure(
            text=text,
            foreground="#a00000" if external else "#666666",
        )

    def reload_profiles(self) -> None:
        self._reload_profiles(initial=False)

    def open_profile_file(self) -> None:
        path = find_profile_file()
        if path is None:
            messagebox.showwarning(
                "未找到配置文件",
                "程序目录下没有 config_profiles.json。",
                parent=self,
            )
            return
        os.startfile(path)  # type: ignore[attr-defined]

    def browse(self, key: str, kind: str) -> None:
        current = self.vars[key].get().strip()
        if kind == "directory":
            selected = filedialog.askdirectory(initialdir=current or str(Path.cwd()))
        elif kind == "image":
            selected = filedialog.askopenfilename(
                filetypes=(("图片", "*.png *.jpg *.jpeg *.webp"), ("所有文件", "*.*")),
                initialdir=str(Path(current).parent) if current else str(Path.cwd()),
            )
        elif kind == "audio":
            selected = filedialog.askopenfilename(
                filetypes=(("音频", "*.ogg *.wav *.mp3"), ("所有文件", "*.*")),
                initialdir=str(Path(current).parent) if current else str(Path.cwd()),
            )
        elif kind == "background":
            selected = filedialog.askopenfilename(
                filetypes=(
                    ("背景图片/视频", "*.png *.jpg *.jpeg *.webp *.mp4"),
                    ("所有文件", "*.*"),
                ),
                initialdir=str(Path(current).parent) if current else str(Path.cwd()),
            )
        else:
            selected = filedialog.askopenfilename(
                filetypes=(("JSON", "*.json"), ("所有文件", "*.*")),
                initialdir=str(Path(current).parent) if current else str(Path.cwd()),
            )
        if selected:
            self.vars[key].set(selected)
            if key == "output_root":
                self.refresh_pack_list(show_errors=False)

    def browse_diff(self, slot: int) -> None:
        selected = filedialog.askopenfilename(
            title=f"选择 {SLOT_LABEL[slot]} 谱面",
            filetypes=(("SPC / JSON", "*.spc *.json"), ("所有文件", "*.*")),
        )
        if selected:
            self.diffs[slot]["chart"].set(selected)

    def browse_background(self, slot: int) -> None:
        selected = filedialog.askopenfilename(
            title=f"选择 {SLOT_LABEL[slot]} 游戏内背景",
            filetypes=(("背景图片/视频", "*.png *.jpg *.jpeg *.webp *.mp4"), ("所有文件", "*.*")),
        )
        if selected:
            self.diffs[slot]["background"].set(selected)

    def fill_identity(self) -> None:
        title = self.vars["song_title"].get().strip()
        folder = self.vars["folder_name"].get().strip()
        if not folder:
            folder = title or "custom_song"
            self.vars["folder_name"].set(folder)
        safe = re_safe(folder)
        if not self.vars["chart_id"].get().strip():
            self.vars["chart_id"].set(safe)
        if not self.vars["base_name"].get().strip():
            self.vars["base_name"].set(safe)

    def _make_spec(self) -> SongPackageSpec:
        try:
            template_id = int(self.vars["template_song_id"].get().strip() or "10")
        except ValueError as exc:
            raise ValueError("模板歌曲 ID 必须是整数") from exc
        difficulties = []
        for slot, item in enumerate(self.diffs):
            text = item["chart"].get().strip()
            if not text:
                continue
            try:
                rating = int(item["rating"].get().strip() or "1")
            except ValueError as exc:
                raise ValueError(f"{SLOT_LABEL[slot]} 的 Rating 必须是整数") from exc
            difficulties.append(
                DifficultyInput(
                    slot=slot,
                    chart=Path(text),
                    rating=rating,
                    level_section_indicator=item["indicator"].get().strip() or "1",
                    background=(
                        Path(item["background"].get().strip())
                        if item["background"].get().strip() else None
                    ),
                )
            )
        try:
            preview_start = float(
                self.vars["preview_start"].get().strip() or "0"
            )
            preview_end_text = self.vars["preview_end"].get().strip()
            preview_end = (
                float(preview_end_text)
                if preview_end_text
                else None
            )
        except ValueError as exc:
            raise ValueError("预览开始/结束必须是数字") from exc
        if not difficulties:
            raise ValueError("至少选择一张谱面")
        audio_text = self.vars["audio"].get().strip()
        if not audio_text:
            raise ValueError("请选择歌曲音频")
        pack = self._selected_pack()
        profile_key = self.profiles_by_label.get(self.profile_label.get())
        if not profile_key:
            raise ValueError("请选择配置结构")
        return SongPackageSpec(
            output_root=Path(
                self.vars["output_root"].get().strip()
                or executable_dir() / "output" / "CustomCharts"
            ),
            pack_folder_name=pack.folder.name if pack else DEFAULT_PACK_FOLDER,
            pack_id=pack.pack_id if pack else DEFAULT_PACK_ID,
            pack_title=pack.title if pack else DEFAULT_PACK_TITLE,
            pack_artist=pack.artist if pack else "",
            pack_description=pack.description if pack else "",
            style_pack_slug=pack.style_pack_slug if pack else DEFAULT_PACK_STYLE,
            pack_sort_order=pack.sort_order if pack else 0,
            folder_name=self.vars["folder_name"].get().strip() or "custom_song",
            chart_id=self.vars["chart_id"].get().strip() or self.vars["folder_name"].get().strip(),
            base_name=self.vars["base_name"].get().strip() or self.vars["chart_id"].get().strip(),
            song_title=self.vars["song_title"].get().strip() or "Custom Song",
            artist=self.vars["artist"].get().strip() or "Custom Artist",
            chart_designer=self.vars["chart_designer"].get().strip() or "Custom Chart",
            jacket_designer=self.vars["jacket_designer"].get().strip() or "Custom Jacket",
            audio=Path(audio_text),
            jacket_large=Path(self.vars["jacket_large"].get().strip()) if self.vars["jacket_large"].get().strip() else None,
            jacket_small=Path(self.vars["jacket_small"].get().strip()) if self.vars["jacket_small"].get().strip() else None,
            background=(
                Path(self.vars["song_background"].get().strip())
                if self.vars["song_background"].get().strip()
                else None
            ),
            template_song_id=template_id,
            gameplay_background=self.vars["gameplay_background"].get().strip() or None,
            character_identifier=self.vars["character_identifier"].get().strip() or None,
            preview_start_seconds=preview_start,
            preview_end_seconds=preview_end,
            preview_audio=(
                Path(self.vars["preview_audio"].get().strip())
                if self.vars["preview_audio"].get().strip()
                else None
            ),
            difficulties=difficulties,
            profile=profile_key,
            profile_config=find_profile_file(),
        )

    def export(self) -> None:
        self._reload_profiles(initial=True)
        try:
            spec = self._make_spec()
        except Exception as exc:
            self.log(f"[失败] {exc}")
            messagebox.showerror("参数错误", str(exc), parent=self)
            return
        try:
            pack = self._pack_for_export()
        except Exception as exc:
            self.log(f"[失败] 无法准备曲包：{exc}")
            return
        spec.output_root = Path(
            self.vars["output_root"].get().strip()
            or executable_dir() / "output" / "CustomCharts"
        )
        spec.pack_folder_name = pack.folder.name
        spec.pack_id = pack.pack_id
        spec.pack_title = pack.title
        spec.pack_artist = pack.artist
        spec.pack_description = pack.description
        spec.style_pack_slug = pack.style_pack_slug
        spec.pack_sort_order = pack.sort_order
        self.export_button.configure(state="disabled")
        self.log("开始导出游戏歌曲文件夹…")

        def worker() -> None:
            try:
                result = export_song_package(spec)
                self.result_queue.put((True, result))
            except Exception as exc:
                self.result_queue.put((False, exc))

        threading.Thread(target=worker, daemon=True).start()
        self.after(100, self._poll_export)

    def _poll_export(self) -> None:
        try:
            ok, value = self.result_queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_export)
            return
        self.export_button.configure(state="normal")
        if not ok:
            self.log(f"[失败] {value}")
            messagebox.showerror("导出失败", str(value), parent=self)
            return
        result = value
        self.log(f"[完成] 歌曲文件夹：{result.folder}")
        self.log(f"        config：{result.config}")
        self.log(f"        配置结构：{result.profile}")
        self.log(f"        谱面：{[p.name for p in result.charts]}")
        if self.open_after.get():
            os.startfile(result.folder)  # type: ignore[attr-defined]

    def open_output(self) -> None:
        path = Path(self.vars["output_root"].get().strip() or ".")
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)  # type: ignore[attr-defined]

    def log(self, message: str) -> None:
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")


class ConverterTab(ttk.Frame):
    def __init__(self, master: tk.Misc):
        super().__init__(master, padding=12)
        self.paths: list[Path] = []
        self.result_queue: queue.Queue[tuple[bool, object]] = queue.Queue()
        self._build()

    def _build(self) -> None:
        ttk.Label(self, text="输入文件或文件夹（可重复添加）").pack(anchor="w")
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(4, 8))
        ttk.Button(bar, text="添加文件", command=self.add_files).pack(side="left")
        ttk.Button(bar, text="添加文件夹", command=self.add_folder).pack(side="left", padx=6)
        ttk.Button(bar, text="清空", command=self.clear).pack(side="left")
        self.listbox = tk.Listbox(self, height=5)
        self.listbox.pack(fill="x")

        opts = ttk.LabelFrame(self, text="转换设置", padding=10)
        opts.pack(fill="x", pady=10)
        ttk.Label(opts, text="目标格式").grid(row=0, column=0, sticky="w")
        self.target = tk.StringVar(value="auto")
        for i, (value, label) in enumerate(
            (("auto", "自动"), ("v1", "SPC V1 二进制"), ("v0", "SPC V0 明文"), ("json", "标准 JSON")),
            start=1,
        ):
            ttk.Radiobutton(opts, text=label, value=value, variable=self.target).grid(
                row=0, column=i, sticky="w", padx=5
            )
        ttk.Label(opts, text="输出目录").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.output_dir = tk.StringVar(value=str(default_output_dir()))
        ttk.Entry(opts, textvariable=self.output_dir).grid(
            row=1, column=1, columnspan=3, sticky="ew", pady=(8, 0)
        )
        ttk.Button(opts, text="浏览…", command=self.browse_output).grid(
            row=1, column=4, padx=(6, 0), pady=(8, 0)
        )
        ttk.Label(opts, text="源 V1 密钥").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.source_key = tk.StringVar()
        ttk.Entry(opts, textvariable=self.source_key).grid(
            row=2, column=1, columnspan=4, sticky="ew", pady=(8, 0)
        )
        ttk.Label(opts, text="目标 V1 密钥").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.target_key = tk.StringVar()
        ttk.Entry(opts, textvariable=self.target_key).grid(
            row=3, column=1, columnspan=4, sticky="ew", pady=(8, 0)
        )
        self.overwrite = tk.BooleanVar(value=False)
        self.sidecars = tk.BooleanVar(value=True)
        check = ttk.Frame(opts)
        check.grid(row=4, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Checkbutton(check, text="覆盖已有文件", variable=self.overwrite).pack(side="left")
        ttk.Checkbutton(check, text="复制同名 .text.json", variable=self.sidecars).pack(side="left", padx=14)
        opts.columnconfigure(1, weight=1)

        action = ttk.Frame(self)
        action.pack(fill="x")
        self.convert_button = ttk.Button(action, text="开始转换", command=self.convert)
        self.convert_button.pack(side="left")
        ttk.Button(action, text="校验选中文件", command=self.verify).pack(side="left", padx=8)
        self.log_text = tk.Text(self, height=10, wrap="word")
        self.log_text.pack(fill="both", expand=True, pady=(10, 0))

    def add_files(self) -> None:
        selected = filedialog.askopenfilenames(
            filetypes=(("SPC / JSON", "*.spc *.json"), ("所有文件", "*.*"))
        )
        self.add_paths([Path(x) for x in selected])

    def add_folder(self) -> None:
        selected = filedialog.askdirectory()
        if selected:
            self.add_paths([Path(selected)])

    def add_paths(self, paths: list[Path]) -> None:
        for path in paths:
            path = path.resolve()
            if path not in self.paths:
                self.paths.append(path)
                self.listbox.insert("end", str(path))

    def clear(self) -> None:
        self.paths.clear()
        self.listbox.delete(0, "end")

    def browse_output(self) -> None:
        selected = filedialog.askdirectory()
        if selected:
            self.output_dir.set(selected)

    def convert(self) -> None:
        try:
            inputs = collect_inputs([str(p) for p in self.paths])
        except Exception as exc:
            self.log(f"[失败] {exc}")
            return
        self.convert_button.configure(state="disabled")
        self.log(f"开始转换 {len(inputs)} 个文件…")

        def worker() -> None:
            try:
                results = batch_convert(
                    inputs,
                    self.target.get(),
                    Path(self.output_dir.get().strip() or default_output_dir()),
                    source_key=self.source_key.get().strip() or None,
                    target_key=self.target_key.get().strip() or None,
                    overwrite=self.overwrite.get(),
                    copy_sidecars=self.sidecars.get(),
                    unique_names=not self.overwrite.get(),
                )
                self.result_queue.put((True, results))
            except Exception as exc:
                self.result_queue.put((False, exc))

        threading.Thread(target=worker, daemon=True).start()
        self.after(100, self._poll)

    def _poll(self) -> None:
        try:
            ok, value = self.result_queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll)
            return
        self.convert_button.configure(state="normal")
        if not ok:
            self.log(f"[失败] {value}")
            return
        for result in value:
            self.log(
                f"[完成] {result.source.name} → {result.output.name}  "
                f"({FORMAT_NAMES[result.source_format]} → {FORMAT_NAMES[result.target_format]}, "
                f"notes={result.notes}, events={result.events})"
            )
        self.log("全部转换完成。")

    def verify(self) -> None:
        try:
            from spc_converter import verify_path

            for path in collect_inputs([str(p) for p in self.paths]):
                self.log(verify_path(path, self.source_key.get().strip() or None))
        except Exception as exc:
            self.log(f"[失败] {exc}")

    def log(self, message: str) -> None:
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")


def re_safe(value: str) -> str:
    value = value.strip()
    value = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in value)
    value = re.sub(r"\s+", "_", value).strip("._")
    return value or "custom_song"


def launch_gui(startup_paths: list[str] | None = None) -> None:
    from falsum_project_tab import FalsumProjectTab
    from studio_project_tab import StudioProjectTab

    root = tk.Tk()
    root.title("SPC Converter - In Falsus 歌曲打包")
    root.geometry("1180x900")
    root.minsize(900, 650)

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=10, pady=10)
    studio_scroll = ScrollFrame(notebook)
    studio_tab = StudioProjectTab(studio_scroll.body)
    studio_tab.pack(fill="both", expand=True)
    falsum_scroll = ScrollFrame(notebook)
    falsum_tab = FalsumProjectTab(falsum_scroll.body)
    falsum_tab.pack(fill="both", expand=True)
    package_scroll = ScrollFrame(notebook)
    package_tab = SongPackageTab(package_scroll.body)
    package_tab.pack(fill="both", expand=True)
    converter_tab = ConverterTab(notebook)
    notebook.add(studio_scroll, text="导入 InFalsus Studio 工程")
    notebook.add(falsum_scroll, text="导入 Falsum 歌曲包")
    # The manual package page remains instantiated for future restoration,
    # but it is intentionally not registered in the current Notebook.
    notebook.add(converter_tab, text="SPC 格式互转")

    if startup_paths:
        converter_tab.add_paths([Path(p) for p in startup_paths if Path(p).exists()])
        notebook.select(converter_tab)

    root.mainloop()

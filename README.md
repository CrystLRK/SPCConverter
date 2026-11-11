# SPCConverter

**版本：1.2.3**

SPCConverter 是用于 *In Falsus* 自制歌曲的 Windows 图形化导入器、SPC 格式
转换器和歌曲包打包工具。

## 功能

- 读取 In Falsus Studio 工程文件夹、`infalsus.studio.json` 或工程 ZIP；
- 读取 Falsum `.falsum` 歌曲包；
- 将歌曲和选定难度导入现有游戏曲包；
- 将单首歌曲导出为独立歌曲文件夹；
- SPC V0 明文、SPC V1/ICP1 二进制和标准 JSON 互转；
- 自动识别 Steam 正式版 `CustomCharts` 目录；
- 支持 OGG、WAV、MP3、独立 preview、图片/视频背景和音频缺失扫描；
- 导入时按难度原子合并，保留未选难度；
- 空难度警告并跳过，所有选择难度为空时停止导入；
- 同难度覆盖使用单一确认框，取消时不写入任何内容。

## 快速开始

1. 解压发布 ZIP；
2. 双击 `SPCConverter.exe`；
3. 选择 Studio 工程或 Falsum 歌曲包；
4. 在可编辑界面中检查歌曲、封面、音频、难度和谱面路径；
5. 点击“导入游戏”，或使用“导出歌曲文件夹”生成独立目录；
6. 完全退出游戏后重新启动，使新曲目进入游戏曲库。

## In Falsus Studio 工程

可选择：

- Studio 工程文件夹；
- `infalsus.studio.json`；
- 由工程文件夹打包出的 ZIP。

程序会安全解压 ZIP，并自动搜索唯一的 `infalsus.studio.json`。

## Falsum 歌曲包

支持 `.falsum` 文件，或只包含一个 `.falsum` 的文件夹。

## 背景、封面和音频

背景支持：

- PNG/JPG/JPEG/WEBP；
- MP4。


歌曲主音频和独立 preview 支持：
- OGG、WAV、MP3。

## 游戏目录
游戏读取的曲包结构：

```text
CustomCharts/
  PackName/
    pack_config.json
    pack_jacket.png
    SongName/
      config.json
      audio.ogg
      preview.mp3
      jacketLarge.png
      jacketSmall.png
      chart.spc
      chart.text.json
```

## SPC 格式互转

支持：

- SPC V0 明文；
- SPC V1/ICP1 二进制；
- 标准 JSON。

自动模式：

- V1 → V0；
- V0/JSON → V1。

SPC V1 使用完整文件名（包含 `.spc` 和大小写）作为滚动加密种子。打包器会
按最终输出文件名重新加密；不要手动重命名已生成的 V1 文件。

## CLI

```powershell
SPCConverterCLI.exe --help
SPCConverterCLI.exe --version
SPCConverterCLI.exe --to v1 input.spc --output output\input.spc
SPCConverterCLI.exe --to json input.spc
SPCConverterCLI.exe --verify input.spc
SPCConverterCLI.exe --interactive
```

## 从源码运行

```powershell
python src\spc_converter.py --gui
python src\spc_converter.py --interactive
```

## 开源与许可

本项目使用 **GNU General Public License v3.0（GPL-3.0）**。

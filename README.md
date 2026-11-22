# SPCConverter

------

**当前 main 分支版本：1.2.4-patch2**

**当前 dev 分支版本：1.2.4-patch2**


**[InFalsusExtension](https://github.com/CrystLRK/InFalsusExtension) 当前 main 分支版本：0.3.0，最低兼容版本：1.2.4-patch2**

**[InFalsusExtension](https://github.com/CrystLRK/InFalsusExtension) 当前 dev 分支版本：0.3.0，最低兼容版本：1.2.4-patch2**

------

SPCConverter 是用于 *In Falsus* 的 Windows 自制歌曲导入与导出工具，适配 InFalsusExtension 的自制谱加载系统。

## 主要功能

- 导入 In Falsus Studio 工程；
- 导入 Falsum 歌曲包；
- 将歌曲导入游戏内的现有曲包；
- 将歌曲导出为独立歌曲文件夹；
- 选择并编辑歌曲名称、作者、封面、音频、难度和背景；

## 使用要求

- Windows 版 *In Falsus* 正式版；不支持 Demo 版；
- 导入游戏前请完全退出游戏；
- 游玩带扩展内容的自制谱时，需要安装与游戏版本匹配的
  InFalsusExtension。
- **从 SPCConverter 1.2.4 开始，导入结果使用 `Charts/` + `pack.json` +
  `if` 格式，不兼容 InFalsusExtension 0.2.0 以下的插件版本；推荐使用
  InFalsusExtension 0.3.0。**

## 快速开始

1. 解压 SPCConverter 发行包；
2. 双击 `SPCConverter.exe`；
3. 选择“导入 InFalsus Studio 工程”或“导入 Falsum 歌曲包”；
4. 选择工程文件夹、工程文件或歌曲包；
5. 检查并修改自动填入的歌曲信息；
6. 选择需要导入的难度和对应背景；
7. 点击“导入游戏”；
8. 完全退出并重新启动游戏。

## 可导入的内容

### In Falsus Studio

- Studio 工程文件夹；
- Studio 工程压缩包；

### Falsum

- `.falsum` 文件；

## 支持的文件

歌曲主音频和独立试听音频：

- OGG；
- WAV；
- MP3。

背景：

- PNG；
- JPG / JPEG；
- WEBP；
- MP4。

## 导入行为

- 会自动尝试定位 Steam 正式版中的自制谱目录；
- 导入到游戏的 `Charts/` 目录，曲包使用 `pack.json`，歌曲使用 `if`；
- 也可以手动创建或选择曲包；
- 只导入本次勾选的难度。

## 许可证

本项目使用 **GNU General Public License v3.0（GPL-3.0）**。

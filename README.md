# 明日方舟剧情阅读器 ver0.8

ver0.8 修复了个性化图片不显示、生成暂停/退出后的进度恢复、误用旧音频，以及 OCR 回调和选区生命周期问题。完整变更见 [ver0.8 更新说明](docs/RELEASE_0.8.md)。

一个面向剧情阅读和无障碍使用场景的 Windows 桌面工具：下载《明日方舟》剧情文本，使用 TTS 预生成语音，再通过 OCR 识别屏幕上的台词并自动播放对应语音。

> 本项目主要由 AI 协助开发，仍可能存在未发现的问题。使用前请自行备份剧情、语音和配置数据。

## 当前状态

- UI 已从 Tkinter 迁移到 PySide6 / Qt。
- 主要验证环境：Windows 10/11。
- 支持 Edge-TTS、Windows SAPI 和可选的 Qwen3-TTS。
- Qwen3-TTS 支持 0.6B、1.7B、声音克隆、AMD ROCm、NVIDIA CUDA 和自动后端检测。
- 源代码仓库不包含 EXE、portable Python、GPU runtime、PyTorch 或模型权重。

## 功能

### 剧情下载

- 从 PRTS Wiki 获取剧情目录和文本。
- 支持主题曲、别传、故事集。
- 支持多选、下载队列、暂停、停止和下载日志。
- 剧情文件按分类、活动和剧情段落保存。

### 语音生成

- Edge-TTS 在线神经语音。
- Windows SAPI 本地语音。
- Qwen3-TTS 0.6B / 1.7B。
- Qwen3-TTS 预设音色和声音克隆。
- AMD ROCm、NVIDIA CUDA、自动检测。
- 支持 TXT、Markdown、LOG 自定义文本。
- 自定义文本同名导入时自动添加“（1）”“（2）”后缀。
- 任务会保存引擎、模型、显卡后端、音色、语速和参考音频。
- 生成队列持久化，关闭软件后可以继续。
- 每次队列运行中每条剧情只尝试一次；失败或部分完成的任务不会自动循环重试。
- 所有语音片段写入 manifest.json，成功片段可以断点复用，失败片段可以查看日志后手动重试。
- 队列支持按未完成、已完成、部分完成、失败/中断和全部筛选。

### OCR 朗读监听

- 自定义框选屏幕上的游戏台词区域。
- 使用 RapidOCR 识别台词。
- 根据当前语音包附近文本进行匹配，降低短句误匹配。
- 支持选择语音包、开始/停止监听；播放使用音频原速，语速需在生成时设置。

### 资源管理

- 按剧情、语音、自定义语音、自定义文本、下载缓存和运行缓存分类。
- 支持多选删除。
- 自定义语音可以按语音名导出到指定目录。
- 监听语音包统一在“朗读监听”模块中选择。

## 快速开始

### 环境要求

- Windows 10 或 Windows 11
- Python 3.12
- Edge-TTS 需要网络连接
- OCR 需要 Windows 屏幕捕获和 RapidOCR 依赖
- Qwen3-TTS 需要单独准备匹配的模型和 GPU runtime

### 安装依赖

~~~powershell
py -3.12 -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
~~~

### 启动

~~~powershell
python .\\main.py
~~~

检查模块：

~~~powershell
python .\\main.py --check
~~~

### 推荐使用流程

1. 在“剧情下载”中刷新目录并加入下载队列。
2. 下载完成后，在“语音生成”中选择语音引擎和音色。
3. 将剧情或自定义文本加入语音生成队列。
4. 生成完成后，在“朗读监听”中选择语音包。
5. 点击“选择台词区域”，框选游戏台词位置。
6. 开始游戏剧情并启动朗读监听。

## TTS 配置

### Edge-TTS

Edge-TTS 不需要 API Key，但需要网络。常用中文音色包括：

| 音色 | 说明 |
| --- | --- |
| zh-CN-XiaoxiaoNeural | 通用女声 |
| zh-CN-YunxiNeural | 年轻男声 |
| zh-CN-YunjianNeural | 沉稳男声 |
| zh-CN-XiaoyiNeural | 轻快女声 |

### Windows SAPI

Windows SAPI 使用系统已安装的语音包，不需要额外模型或网络。可用音色由 Windows 系统决定。

### Qwen3-TTS

Qwen3-TTS 使用独立服务进程，GUI 不直接导入大型模型。

| 模型 | 建议 |
| --- | --- |
| 0.6B | 适合 6GB 显存设备，优先用于 RTX 3060 Laptop 等设备 |
| 1.7B | 需要更多显存，实际占用取决于精度、后端、句子长度和服务配置 |

声音克隆需要对应的 Base 模型和参考音频。任务会锁定当时使用的模型、后端和参考音频；如果参考音频路径失效，继续任务时会提示重新选择。

Qwen runtime 和模型不提交到 GitHub。可参考：

- docs/OPTIONAL_TTS_GUIDE.md
- docs/WINDOWS_PACKAGING.md

## GPU 后端

| 后端 | 状态 | 说明 |
| --- | --- | --- |
| NVIDIA CUDA | 支持 | 需要 NVIDIA 驱动、CUDA 对应 PyTorch 和 Qwen runtime |
| AMD ROCm | 支持 | 需要 AMD 驱动、ROCm 对应 PyTorch 和 Qwen runtime |
| 自动检测 | 支持 | 优先根据系统环境选择 CUDA 或 ROCm |

AMD 和 NVIDIA 的 PyTorch runtime 通常不能直接共用。建议分别维护 runtime 和 runtime_cuda，不要在同一个 Python 环境中混装两套 GPU PyTorch。

## 项目结构

~~~text
arknights-tts-reader/
├── main.py                  # Qt 应用入口
├── qt_main.py               # Qt 应用壳、导航和全局监听生命周期
├── qt_base.py               # Qt 面板基类、卡片和通用样式
├── qt_story.py              # 剧情下载界面
├── qt_voice.py              # 语音生成和队列界面
├── qt_voice_packs.py        # 资源管理界面
├── qt_reader.py             # 朗读监听界面
├── qt_settings.py           # 设置界面
├── app_runtime.py           # 便携目录、DPI 和设置读写
├── ui_config.py             # UI 配置常量
├── prts_catalog.py          # PRTS 剧情目录和下载
├── story_catalog.py         # 剧情数据模型和文本处理
├── voice_generation.py      # TTS 后端和逐句语音包生成
├── voice_queue.py           # 语音队列持久化
├── reader_engine.py         # OCR、文本匹配和音频播放
├── quality_analysis.py      # 可选 DeepSeek 情绪分析
├── runtime/
│   └── tts_server.py        # Qwen 服务端参考实现
├── assets/
│   └── app_icon.ico         # 应用图标
├── docs/
│   ├── OPTIONAL_TTS_GUIDE.md
│   └── WINDOWS_PACKAGING.md
├── requirements.txt
├── requirements-build.txt
└── data/
    └── .gitkeep             # 运行时数据目录占位文件
~~~

运行后会在 data/ 下生成：

~~~text
data/
├── stories/                 # 剧情 JSON
├── custom_texts/            # 自定义文本
├── voice_packs/             # 音频和 manifest.json
├── voice_generation_queue.json
├── cache/prts/              # PRTS 页面缓存
├── tts_analysis/            # DeepSeek 分析缓存
├── qwen_cache/              # Qwen 运行缓存
└── settings.json            # 本地设置
~~~

## Windows 封装

源码仓库不包含大型运行时。准备好 Python 3.12 和构建依赖后：

~~~powershell
py -3.12 -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install -r requirements-build.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\build_windows.ps1
~~~

脚本会在源码目录生成 ArknightsTTSReader.exe。发布时还需要根据目标机器准备：

- assets/
- runtime/ 或 runtime_cuda/
- 对应的 Python、PyTorch、ROCm/CUDA runtime
- Qwen3-TTS 模型和 tokenizer

模型、语音、剧情、参考音频、API Key 和用户数据不应放入 GitHub 仓库。

## 二次开发

- GUI 逻辑放在 qt_*.py，TTS 逻辑放在 voice_generation.py。
- 不要在 Qt GUI 进程中直接加载大型 TTS 模型。
- 新增 TTS 后端时，使用统一的 TTSBackendManager 合成接口。
- 每句生成后及时写入 manifest，失败不得丢失已经成功的片段。
- 队列状态、暂停、停止、重试和显存释放需要有明确的 UI 状态。
- 不要提交 API Key、模型权重、参考音频、生成音频、剧情数据和运行时缓存。

## 许可证与第三方资源

本项目不携带第三方模型和 GPU runtime。使用 Edge-TTS、RapidOCR、playsound3、Qwen3-TTS、模型权重、声音样本和剧情数据时，请分别遵守其许可证、服务条款和数据使用规则。

## 关于项目

这是一个由需求驱动、由 AI 协助完成主要设计与实现的实验性工具。欢迎提交问题、改进建议和适用于不同硬件环境的测试反馈。

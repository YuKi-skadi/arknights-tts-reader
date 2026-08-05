# 明日方舟剧情阅读器

⚠️ **免责声明**：本项目 100% 由 AI 构建，虽然已经经过实际测试，但仍可能存在不可预见的 Bug。使用前请自行评估风险，作者不对因使用本软件造成的任何损失负责。

[![Platform](https://img.shields.io/badge/Platform-Windows-blue.svg)](#平台与硬件支持)
[![GPU](https://img.shields.io/badge/GPU-AMD%20%7C%20ROCm-red.svg)](#平台与硬件支持)
[![AI](https://img.shields.io/badge/Made%20with-100%25%20AI-purple.svg)](#关于项目)

**将明日方舟剧情转换为可匹配播放的语音，支持剧情下载、预生成语音、OCR 监听和本地 TTS**

> 先下载剧情并生成语音，阅读时通过 OCR 匹配对应片段 | 支持 Edge-TTS 和 Windows 本地语音 | 面向无障碍阅读场景

## ✨ 核心功能

### 📚 剧情资源

- 从 [PRTS Wiki](https://prts.wiki/w/剧情一览) 获取剧情导航和剧情文本；
- 支持主题曲、别传、故事集；
- 自动处理角色名、旁白和对白文本；
- 支持 Ctrl/Shift 多选、任务队列、暂停、停止和下载日志；
- 剧情文件按“分类 → 活动 → 剧情段落”保存。

### 🔉 语音生成

- **Edge-TTS**：在线神经语音，音质自然，支持多种中文音色；
- **Windows 本地 TTS**：使用系统 SAPI，速度快、不依赖在线服务；
- 按剧情段落预生成音频，阅读时无需等待合成；
- 支持语速调节、队列排序、暂停后继续；
- 每句独立记录状态，失败片段可查看并单独重试；
- 生成结果包含 `manifest.json`，用于后续 OCR 精确匹配；
- 可选使用 DeepSeek 为 Qwen3-TTS 等大模型生成逐句情绪提示。

### 👁️ OCR 朗读监听

- 自定义框选游戏台词区域；
- RapidOCR 识别不同背景下的台词；
- 根据当前剧情位置附近的文本进行匹配，降低短句误匹配；
- 同一句 OCR 文本不会反复打断播放；
- 支持调速、开始/停止监听和当前语音包选择。

### 🧰 资源管理

- 统一管理剧情资源、语音资源、下载缓存和情绪分析缓存；
- 支持 Ctrl 多选；
- 支持右键删除资源；
- 可清理 Qwen/ROCm 运行缓存；
- 监听语音包选择放在“朗读监听”模块中，逻辑更直观。

## 🚀 快速开始

本仓库是**纯源代码版本**，不包含 EXE、portable Python、ROCm、PyTorch、Qwen3-TTS 模型或其他大型运行时文件。

### 1. 创建 Python 环境

建议使用 Python 3.11 或 3.12：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### 2. 启动程序

```powershell
python .\main.py
```

检查模块是否可以导入：

```powershell
python .\main.py --check
```

### 3. 推荐使用流程

1. 打开“剧情下载”，刷新剧情目录并选择剧情段落；
2. 下载并导出剧情；
3. 打开“语音生成”，选择 Edge-TTS 或 Windows 系统语音；
4. 将剧情加入生成队列，生成对应语音包；
5. 打开“朗读监听”，选择当前语音包并框选游戏台词区域；
6. 进入游戏剧情，点击开始监听。

## ⚙️ TTS 配置

### Edge-TTS

Edge-TTS 需要网络，不需要 API Key。当前代码内置的中文音色包括：

| 音色 | 适用场景 |
|---|---|
| `zh-CN-XiaoxiaoNeural` | 通用女声 |
| `zh-CN-YunxiNeural` | 年轻男声 |
| `zh-CN-YunjianNeural` | 沉稳男声 |
| `zh-CN-XiaoyiNeural` | 轻快女声 |

### Windows 本地 TTS

本地 TTS 使用 Windows SAPI，不需要网络或大模型。可用音色取决于系统中安装的语音包，适合追求稳定速度和低资源占用的场景。

## 🤖 可选大模型 TTS

Qwen3-TTS 的接口适配代码仍保留在 `voice_generation.py` 和 `runtime/tts_server.py` 中，但模型和运行环境没有放进本仓库。

启用本地大模型通常需要自行准备：

- 模型权重和 tokenizer；
- PyTorch 或其他推理框架；
- ROCm、CUDA 或 MPS 等硬件后端；
- 音频编解码和参考音频处理依赖；
- 与模型匹配的 Python 环境。

详细接入方法请阅读：

- [可选大模型 TTS 接入指南](docs/OPTIONAL_TTS_GUIDE.md)
- [Windows EXE 构建与发布说明](docs/WINDOWS_PACKAGING.md)
- [给开发者和 Agent 的项目说明](AGENTS.md)

## 💻 平台与硬件支持

当前项目以 **Windows + AMD 显卡 + ROCm** 为主要验证环境。

| 平台 | 当前状态 | 需要注意 |
|---|---|---|
| Windows + AMD | ✅ 主要验证平台 | Qwen3-TTS 可使用 ROCm，需匹配驱动和 PyTorch |
| Windows + NVIDIA | ⚠️ 可改造 | 将 ROCm/PyTorch 后端替换为 CUDA，并调整设备和显存释放逻辑 |
| macOS | ⚠️ 需要改造 | 屏幕捕获使用 ScreenCaptureKit，系统语音改用 `say`/AVFoundation，模型可尝试 MPS |
| Linux | ⚠️ 需要改造 | 需要替换屏幕捕获、系统语音和窗口相关逻辑 |

硬件相关代码应集中在 TTS 服务和平台适配层，不要把 CUDA、ROCm 或 MPS 判断散落到 GUI、剧情解析和 OCR 匹配逻辑中。

## 📂 项目结构

```text
arknights-tts-reader/
├── main.py                  # Tkinter GUI 和模块编排
├── prts_catalog.py          # PRTS 剧情目录与下载
├── story_catalog.py         # 剧情数据模型和文本预处理
├── voice_generation.py      # Edge/SAPI/Qwen 后端与语音包生成
├── reader_engine.py         # OCR、匹配和音频播放
├── quality_analysis.py      # 可选 DeepSeek 情绪分析
├── runtime/
│   └── tts_server.py        # 可选本地模型 HTTP 服务示例
├── docs/
│   └── OPTIONAL_TTS_GUIDE.md
├── requirements.txt         # 基础依赖
├── AGENTS.md                # 开发者和 Agent 使用说明
└── data/                    # 运行时自动生成，仓库只保留 .gitkeep
```

运行后会在 `data/` 下生成：

```text
data/
├── stories/       # 剧情 JSON
├── voice_packs/   # 音频和 manifest.json
├── cache/prts/    # PRTS 页面缓存
├── tts_analysis/  # DeepSeek 分析缓存
├── qwen_cache/    # 可选 Qwen/ROCm 缓存
└── settings.json  # 本地设置
```

## 🛠️ 二次开发

新增 TTS 后端时，建议遵循以下原则：

1. 通过统一的 `synthesize()` 接口返回音频；
2. 将大型模型放在独立服务进程中；
3. 每句生成后立即更新 manifest；
4. 失败句不能影响已完成片段；
5. 暂停、停止、重试和显存释放必须可观察；
6. API Key、模型权重和用户数据不能提交到仓库。

## 📜 许可证与第三方资源

本项目当前不携带第三方模型和运行时。使用 Edge-TTS、RapidOCR、playsound3、模型权重、声音样本和剧情数据时，请分别遵守其许可证、服务条款和数据使用规则。

## 🤝 关于项目

这是一个由需求驱动、由 AI 完成主要设计与实现的实验性无障碍工具。项目的代码、UI、文档、调试方案和跨平台建议均由 AI 生成或协助完成，人工负责实际运行测试、提出修改意见和确认功能结果。

> 让剧情阅读不再依赖视觉，也让本地 AI 工具保持开放和可改造。

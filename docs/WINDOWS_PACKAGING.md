# Windows EXE 构建说明

这份说明适用于直接从 GitHub 下载的源代码目录，不依赖任何其他版本目录或本地开发环境。

## 1. 准备环境

在项目根目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt
```

如果系统禁止运行 PowerShell 脚本，可以只对当前构建命令临时放宽策略：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build_windows.ps1
```

## 2. 构建 EXE

仍然在项目根目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build_windows.ps1
```

脚本会在项目根目录生成 `ArknightsTTSReader.exe`，并把 Edge-TTS、aiohttp、RapidOCR 和必要的 Pillow 子模块封装进去。这样换到另一台机器时，不会因为缺少 `http.cookies` 或动态导入模块而失败。

构建前请关闭正在运行的 `ArknightsTTSReader.exe`，否则 Windows 可能拒绝覆盖旧文件。

## 3. 构建产物与外置资源

可以复制下列内容给最终用户：

- `ArknightsTTSReader.exe`
- `assets/`
- `data/`（如果需要保留剧情、语音包和设置）
- 可选的 `runtime/`（仅当已经准备了本地 Qwen3-TTS 服务环境）

不建议提交或封装进公共源代码仓库的内容：

- Qwen3-TTS 模型权重
- ROCm、CUDA、PyTorch 等大型 GPU 运行时
- API Key、参考音频、生成音频和个人剧情数据

Qwen3-TTS 是可选功能。模型和 GPU 环境应放在应用目录的 `runtime/` 或由用户自行配置；Edge-TTS、Windows SAPI、剧情下载和 OCR 不应依赖 Qwen/ROCm 才能工作。

## 4. 修改代码后重新构建

修改 Python 文件后，先运行基础检查：

```powershell
python -m py_compile main.py prts_catalog.py quality_analysis.py reader_engine.py story_catalog.py voice_generation.py
python main.py --check
```

确认无语法错误后，再执行 `build_windows.ps1`。建议至少手动测试：程序启动、剧情下载、Edge-TTS 生成、Windows 本地语音、OCR 监听和失败片段重试。

## 5. 增加其他 TTS 引擎

在 `voice_generation.py` 中增加引擎标识、音色选项和统一的合成分支；大型模型最好通过独立服务进程调用。对于运行时动态导入的依赖，在构建脚本加入 `--collect-all 包名` 或 `--hidden-import 模块名`，并针对单句、长句、超时、显存不足和释放资源进行测试。

本项目主要验证环境是 Windows + AMD + ROCm。迁移到 NVIDIA 时，需要使用 CUDA 对应的 PyTorch 和设备逻辑；迁移到 macOS 时，需要改造屏幕捕获、系统语音和 GPU 后端。Edge-TTS、剧情解析和 OCR 匹配代码应尽量保持平台无关。

# 给开发者和 Agent 的项目说明

这是一个以 Windows + AMD/ROCm 为主要验证环境的 Python/Tkinter 项目。项目由 AI 生成和维护，当前基础功能已经可以使用；修改时应优先保持现有功能稳定，再进行局部改动。

## 重要边界

- 不要提交 API Key、模型权重、参考音频、生成音频或用户剧情数据。
- 不要把 portable Python、ROCm、CUDA、PyTorch 或大型模型加入源码仓库。
- 不要在 GUI 进程中直接导入大型 TTS 模型；模型应放到独立服务或后端适配层。
- `data/` 是运行时目录，下载内容、语音包、缓存和设置都属于用户数据。
- 当前主要目标是 Windows；修改 macOS、Linux、NVIDIA 时要明确加入平台分支和测试。

## 模块职责

- `main.py`：Tkinter 界面、模块导航、队列和用户操作；
- `prts_catalog.py`：PRTS Wiki 目录、页面获取和任务控制；
- `story_catalog.py`：剧情文本规范化、角色名过滤和匹配文本；
- `voice_generation.py`：TTS 后端、预生成语音包、逐句状态和断点复用；
- `reader_engine.py`：截图、OCR、附近剧情匹配和音频播放；
- `quality_analysis.py`：可选 DeepSeek 模型列表和逐句情绪提示；
- `runtime/tts_server.py`：可选 Qwen 服务端参考实现。

## 增加新 TTS 的推荐步骤

1. 在 `voice_generation.py` 增加引擎标识、显示名和音色选项。
2. 让 `TTSBackendManager` 通过统一输入返回音频字节和扩展名。
3. 把模型加载和释放放到独立进程或独立后端。
4. 让每句生成结果写入 manifest，失败不得丢失已经成功的片段。
5. 只在 GUI 中增加必要的选项，不把硬件细节写进 OCR/匹配模块。
6. 在 `docs/OPTIONAL_TTS_GUIDE.md` 补充安装、许可证和硬件要求。
7. 用 Edge-TTS/SAPI 回归测试，再测试新后端的单句、长句、失败和释放流程。

## 验证命令

在安装基础依赖后，可以先运行：

```powershell
python -m py_compile main.py prts_catalog.py quality_analysis.py reader_engine.py story_catalog.py voice_generation.py
python main.py --check
```

源代码仓库提供通用的 `build_windows.ps1` 打包脚本，但不提供 portable Python、模型或 GPU 运行时。构建前请按 `docs/WINDOWS_PACKAGING.md` 创建虚拟环境并安装构建依赖；EXE 和构建目录不要提交到本仓库。

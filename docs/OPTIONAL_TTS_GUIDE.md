# 可选大模型 TTS 接入指南

本仓库默认只要求 Edge-TTS、Windows SAPI、OCR 和剧情下载依赖。大模型 TTS 是可选后端，模型权重、推理框架、GPU 运行时和缓存都必须由使用者自行准备，不能提交到 Git 仓库。

## 接入目标

一个新的 TTS 后端至少应提供以下能力：

```text
输入：文本、音色、语速、可选情绪/提示词、可选参考音频
输出：音频字节、实际文件格式
生命周期：初始化、合成、释放模型/显存
状态：启动、生成中、失败、释放完成
```

当前项目的主要接入位置是 `voice_generation.py`：

- `ENGINE_*`：声明引擎标识；
- `ENGINE_LABELS`：GUI 显示名称；
- `voice_options()`：提供音色下拉选项；
- `TTSBackendManager.synthesize()`：统一调用入口；
- `generate_voice_pack()`：逐句生成音频、写入 manifest 和断点状态；
- `TTSBackendManager.release_qwen()`：释放独立本地模型服务的示例。

## 推荐架构：GUI 与大模型进程分离

不要在 PySide6 GUI 进程里直接导入 PyTorch、Transformers 或大型 TTS 模型。推荐使用独立服务进程：

```text
GUI
 └─ HTTP/JSON 或 stdin/stdout
     └─ TTS Server
         └─ 模型、GPU 后端和显存
```

这样做可以：

1. 基础 Edge-TTS/SAPI 功能不受大型依赖影响；
2. 模型加载失败不会直接拖垮 GUI；
3. 生成完成后可以终止服务进程并回收显存；
4. 更容易为 AMD、NVIDIA 和 Apple Silicon 分别准备环境。

`runtime/tts_server.py` 是当前 Qwen3-TTS 服务的参考实现。新模型可以复用接口设计，不必复用 Qwen 的代码。

## 建议的最小服务接口

可以保持现有接口形式：

```json
POST /synthesize
{
  "text": "要朗读的文本",
  "speaker": "voice-name",
  "speed": 1.0,
  "mode": "custom_voice",
  "reference_audio": "",
  "reference_text": "",
  "instruct": "情绪平静，语速适中"
}
```

响应直接返回 WAV/MP3 音频字节。建议再提供：

```text
GET  /health
POST /shutdown
```

服务端需要对模型加载失败、参考音频不存在、文本过长、显存不足和音频为空给出明确错误。客户端必须设置超时并把失败交给逐句 manifest，而不是让整个队列丢失进度。

## 模型准备清单

接入任意本地 TTS 模型前，使用者通常需要自行准备：

- 模型权重和 tokenizer/声学组件；
- 与模型匹配的 Python 版本；
- PyTorch 或其他推理框架；
- 对应 GPU 后端运行时；
- 模型所需的音频编解码库；
- 参考音频格式、采样率和时长限制；
- 模型许可证、声音克隆授权和数据使用许可。

不要把模型权重复制到源码仓库。建议通过配置项指定模型目录，例如：

```text
runtime/models/<model-name>/
```

并在启动时检查目录是否完整；缺失时给出下载或配置提示，而不是静默失败。

## AMD、NVIDIA、macOS 适配

硬件适配应集中在 TTS 服务端，不要改动剧情解析和 OCR 匹配逻辑。

### AMD / ROCm

- 安装与显卡和 Python 版本匹配的 ROCm/PyTorch 组合；
- 识别 `torch.cuda.is_available()` 是否能正确暴露 HIP 设备；
- 根据模型实际支持情况选择 `float16`、`bfloat16` 或 `float32`；
- 为 ROCm/MIOpen 设置独立缓存目录；
- 测试模型加载、单句生成、长句生成和释放显存。

### NVIDIA / CUDA

- 换用 CUDA 对应的 PyTorch 和依赖版本；
- 应用目录使用独立的 `runtime_cuda`，避免和 ROCm 的 PyTorch 包互相覆盖；
- 检查 `CUDA_VISIBLE_DEVICES` 和 device map；
- 用 `torch.cuda.empty_cache()`、进程退出和必要的 IPC 清理释放显存；
- 不要假设 ROCm 的缓存目录或内核缓存能直接复用。

当前语音生成界面提供 `Qwen3-TTS 0.6B` 和 `Qwen3-TTS 1.7B` 两个模型选项，以及
`自动检测`、`NVIDIA CUDA`、`AMD ROCm` 三个后端选项。加入队列时会把自动检测结果
写入任务配置，恢复任务时不会因为机器环境变化而偷偷切换后端。6GB 显存（例如
RTX 3060 Laptop）应优先使用 0.6B；1.7B 需要根据显存和精度实际测试。

### macOS / Apple Silicon

- 使用 MPS 或模型支持的 CPU 推理路径；
- 屏幕截图应改用 ScreenCaptureKit 或其他 macOS 原生方案；
- Windows SAPI 应替换为 `say`、AVFoundation 或其他系统 TTS；
- 不要在 macOS 上假设 `shutdown.exe`、Windows 路径和 `ImageGrab` 的行为一致。

## 逐句生成和质量优化

本项目是预生成架构。接入大模型时应保持以下原则：

1. 一句一文件，文件名和 `manifest.json` 中的 `line_id` 保持稳定；
2. 每生成一句就更新 manifest；
3. 失败句记录错误但允许后续句继续；
4. 重试时复用已经存在且有效的音频；
5. DeepSeek 等文本分析服务只负责提供提示词，不应修改原台词；
6. API Key 只从用户设置或环境变量读取，绝不写入源码、日志和提交记录。

## 接入完成后的测试

至少完成以下测试再合并：

- 基础 Edge-TTS 和 Windows SAPI 不受影响；
- 单句、长句、空文本和特殊标点；
- 预设音色和声音克隆（如果支持）；
- 语速和情绪提示实际生效；
- 模型加载失败、显存不足、服务异常和超时；
- 暂停后继续、停止后中断、失败片段重试；
- 释放模型后 GPU 显存和服务进程确实退出；
- 部分完成的语音包仍能被 OCR 监听安全读取。

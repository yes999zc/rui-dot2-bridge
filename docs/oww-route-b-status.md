# 设备端唤醒（路线 B）进展与现状（2026-10-25 16:40，测试已暂停）

> 结论：**设备侧 OWW 打分已在本机 Dot 2 上真实跑起来**（stock 英文 `hey_jarvis_v0.1`），
> adapter 侧 v1 `device_wake` 链路已按"证据式 probe"实现并通过单测；**真人触发验证按 9 哥安排暂停**，
> 等新会话统一调度测试时再一起做。

## 一、设备侧：资产与打分器

| 项 | 事实 |
|---|---|
| 设备目录 | `/data/local/share/echomuse/oww`（**不是** `/app/models/oww_runtime`，那是 EchoMuse 控制器镜像里的路径） |
| 已有资产（10-23 就装好了，本次 md5 全部复核一致） | `libonnxruntime.so` 12,290,332 B（来自 `onnxruntime-android-1.19.2.aar` 的 `jni/armeabi-v7a`）、`melspectrogram.onnx`、`embedding_model.onnx`、`hey_jarvis_v0.1.onnx`、`alexa_v0.1.onnx`、`hey_mycroft_v0.1.onnx`、`hey_rhasspy_v0.1.onnx`、`silero_vad.onnx`（控制器改写版） |
| 启用方式 | 控制器 `/control` 的 `config` 消息：`{"owwOnDevice":"on","owwModel":"hey_jarvis_v0.1","owwThreshold":0.5}`，**字符串不是 bool**，`owwThreshold` 只能 >0 |
| 设备日志证据 | `[shadow] on-device wake word scoring (onnxruntime 1.19.2, model hey_jarvis_v0.1, xnnpack=true, streaming embedding: no streaming model given, threshold 0.50, barge-in off) — triggering turns` |
| 关闭方式 | 同一消息推 `"owwOnDevice":"off"` → 设备日志 `[shadow] on-device wake word disabled`（本次已关回去） |
| 关键限制 | 我们的固件在控制器**未宣告 `listen_session`** 时保持 `stream` 模式（mic 继续推流），不会进入 `local`；唤醒只在 `oww_wake` 消息里单独上报 |

参考来源：EchoMuse `device/internal/wakeword/shadow/open.go`（目录与文件命名、`ModelStem` 规则）、
`device/internal/listen/state.go`（`Resolve()`：`on` + 控制器有 sessions + scorer 就绪 = local）、
`device/cmd/server.go`（`applyShadowConfig`、`onWakeCrossing`）、`controller/em_oww_assets.py`
（资产清单/来源）、`controller/em_oww_sync.py`（字段面 = shell 平面 + md5）。我们设备有 USB adb，直接 `adb push` 更省事。

## 二、adapter 侧改动（本仓库已提交）

1. **config 推送补字段**：原来只推 `owwOnDevice`，设备不知道用哪个模型；现在推 `owwModel` + 可选 `owwThreshold`
   （`DOT2_OWW_MODEL` / `DOT2_OWW_THRESHOLD`）。
2. **证据式 probe**：`DEVICE_WAKE` 的第二个诚实证明——设备真的打过一次分（有效 `oww_wake` 或 `oww_shadow_cross`）
   就宣告，不再只认 `listen_state="local"`。理由：`stream` 模式下设备仍持续推流、唤醒走 `oww_wake`（无 session 字段），
   这正是 rui v1 想要的"设备唤醒 + 连续音频"形态。`oww_shadow_cross` 只作证据、绝不进 DeviceWake 队列。
3. **barge 补位**：stream 模式固件不报 `barge` 位，adapter 用自己的播放状态补：播放中收到 wake → `barge=True`。
4. **DOT2_BARGE_IN_ENABLED** 可控：设备播放中会把门槛降到 `bargeInThreshold`（默认 0.05），实验时建议关掉，
   避免把回声/噪声当成 barge。
5. 单测：新增 3 个 link 测试（stream 证据升级、shadow 证据、config 字段、播放中 barge），全套 **117 tests + ruff clean**。

## 三、为什么暂停在这里

9 哥决定：**测试统一由新会话调度**（后面还有 1 台 LX06 + 4–5 台 Dot2 到货），本会话不再动真人段。
所以 `.34` 已恢复到文档中的稳定形态：`rui-dot2:0.4.2`、`healthy`、设备在线、`DOT2_OWW_*` 已移除、设备端 scorer 已关。

## 四、下次测试要做的事（预计随 0.5.0 干净安装一起）

1. 干净装好 0.5.0 + adapter 层后，把 `.env` 加回 `DOT2_OWW_MODEL=hey_jarvis_v0.1`、`DOT2_OWW_ON_DEVICE=on`、
   `DOT2_BARGE_IN_ENABLED=0`；
2. 设备侧确认 `[shadow] ... — triggering turns`；
3. 真人说两次 "hey jarvis"：第一次让 rui 从 KWS 切到设备唤醒（`device_wake on after re-probe`），
   10 秒后第二次带问题，验证完整 turn；注意 rui 重探测预算只有 12×5 s，说完没触发就重启音频面重来；
4. 观测：`/api/v1/audio` 的 `device_wakes` / `device_wake_turns` / `device_wake_barges` 计数、turn trace；
5. 结果回写 rui#3（device_wake）与 #7。

## 五、遗留风险

- 设备进程出现过一次 `T`（stopped）状态、`kill -CONT` 后由 supervisor 重启恢复；原因未定位（可能与短时间内多次容器重建有关），复现时记录。
- `owwThreshold=0.5` 是设备默认；英文模型对中文口音/音量敏感，真人测试时若分数不够可下调到 0.4 或改用 `alexa_v0.1` 对比。
- `embedding_stream.onnx` 未装（可选，装了省约 2/3 CPU），本次不需要。

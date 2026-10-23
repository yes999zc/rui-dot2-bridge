# P0 / P1 实测报告（2026-10-24 凌晨）

不依赖人配合的部分已经跑通。所有数字来自真机 + 本机 rui，不是纸面推演。

## P0 — 设备协议探针

| 检查项 | 实测 |
|---|---|
| 设备发现 | 静态 `controller.json` 生效：`Static endpoint 1/2 → wss://<lan-ip>:8870`（旧 controller 仍在 endpoints[1] 作为回滚） |
| TLS | 用旧 controller 的 `server.pem/server.key`，设备以固定 ServerName `echomuse-controller` 校验通过 |
| 握手 | `register` → `ack` 一次通过；设备上报 **v2.18.1 / fireos / ip <lan-ip>**，27 项能力（`mic,speaker,leds,led_anim,buttons,...,aec_hw_ref,...,ble_connect`） |
| 监听状态 | 握手后 `listen_state=stream`；`mic_start(lock_mic=false)` 后**连续流**，无需设备侧唤醒 |
| 上行 | `0x01` 帧，**12.5 帧/s、80 ms/块、≈31 KB/s**，RTT 5.4–7.8 ms |
| 音量 | 设备原音量 85/127 → 被 adapter 压到 **38/127（30%）**；设备日志 `Volume set to 38/127`；物理按键超限会被拉回 |
| 回滚 | 停掉 adapter 后设备依次尝试 endpoints[1] 旧 controller；恢复旧 controller 容器即回退 |

## P1 — rui 端到端

### 配置形态（零改 rui）

- 本机 checkout `01_Code/projects/house-ear` 只读；adapter 通过 `PYTHONPATH=…/rui-dot2-bridge/src` 注入，
  `src/sitecustomize.py` 在**父进程和音频面子进程**里都执行 `registry.register("dot2", ...)`；
- rui 配置只改 `dev/rui/config.yaml`：设备 1 台（`adapter: dot2`，TLS 8870）、`capture 16k/1ch/0ref`、
  `playback 48k/1ch`；
- 音频面子进程仍受 rui 自己的 watchdog 管理；`/health` → `audio_plane=true`、`speaker_connected=true`。

### 文本回合（rui 自带"试一试" = 完整回合 + TTS + 播放）

请求：`POST /api/v1/debug/say {"text":"现在几点？"}`

| 指标 | 实测 |
|---|---|
| 回复 | 「现在是凌晨2点38分，周六。」（DeepSeek） |
| 回复音频 | 111,358 B @16 kHz = 3.48 s |
| rui 阶段耗时 | `llm_first=437 ms`、`llm_done=2094 ms`、`tts_first=940 ms`、`play_start=940 ms` |
| 设备播放 | `periods=85 underruns=0 primeWait=13ms recvSpan=3341ms maxGap=123ms` |
| 结论 | **TTS→adapter→设备出声全链路通**，无欠载；`primeWait` 仅 13 ms（rui 的 lead 已把设备的 1 s prime 门槛填满） |

### 语音回合（声学回路自测）

因为没有人在房间，用注入钩子把「你好小瑞 + 现在几点」以 48 kHz 送进设备扬声器（音量仍 30%），
设备麦克风听到后由 adapter 录成 WAV：

- 录音 39.6 s，注入段 RMS ≈ -45 dBFS；
- 把录音喂给 rui 的 `/api/v1/asr`：
  - 16–19 s → **「你好，小瑞。」**
  - 18–21 s → **「小瑞，现在几点？」**
- 结论：**扬声器 → 空气 → 7 麦 → 设备 AEC/波束 → S16 mono → adapter → rui ASR** 这条声学路径成立。

### 鲁棒性（2026-10-24 02:46 实测）

- 直接 `kill -9` 音频面子进程（监听 8870 的那个）：
  - 6 s 内 rui 的 watchdog 判定 `audio plane reader stopped: IncompleteReadError`；
  - **3 s 后平面重建**（`audio plane restarted (#1, pid=94653)`）；
  - 又 3 s 设备重新 `register`、`speaker_connected` 回到 `true`，全程**不需要重启 rui、不需要动设备**；
  - 说明"平面崩溃 → 重新拉起 → 设备自己拨回来"这条链在真机上成立。
- 同时启动了 8 小时 soak 记录（`scripts/soak.py` → `dev/rui/soak.jsonl`），早上可直接看
  重连次数/平面重启次数/唤醒计数；这是 P4 的 24 h 证据的前半段。

### 回滚与音量上限（2026-10-24 02:54–02:58 实测）

- 杀 adapter → 设备 40 s 内落到 `controller.json` endpoints[1] 的旧 EchoMuse controller
  （old-controller DB `last_seen` 从 02:25 跳到 02:54:30），**自动回滚成立**；
- 恢复 adapter、停旧 controller → 设备 45 s 内回到本项目，设备日志 `Volume set to 38/127`，
  `/tmp/em-config.json` 的 `received.startupVolume=38`（上限已持久化）；
- **发现**：旧 controller 接管时会按 HA 的 volume 状态把音量推成 `0.669`（= 85/127），
  不理会我们的 30% 上限。所以无人值守时它保持停止；启用它做回滚演练前要先把它的设备音量改成 0.3。
- rui 控制台现在能看到设备音量（0–255 刻度显示 **76**，即 30%），任何入口的请求都仍被夹到 30%。

### 唤醒（KWS）—— 已修正：30% 音量可行，是"音色"不是"电平"

第一版结论（用 macOS `say` 的 Tingting 音色做自测）说"差约 20 dB"，**这个结论是错的**。
换成 rui 自己的讯飞音色 `x4_lingxiaowan_boy` 后：

| 输入 | 0.15（线上阈值） | 0.25 | 0.40 |
|---|---|---|---|
| Tingting 合成词，满幅离线评分 | 0 命中 | 0 | 0 |
| **讯飞音色**合成词，满幅离线评分 | **1 命中** | **1 命中** | **1 命中** |
| 讯飞音色经 30% 音量扬声器回采，离线评分 | **1 命中（0 dB 增益）** | 1 命中 | — |
| 同一路径，真实 rui 平面 | **`wake: 你好小瑞`（2026-10-24 03:01:01 实测）** | | |

结论：
- **30% 音量下唤醒是可行的**——KWS 模型对讯飞音色敏感度正常；之前"差 20 dB"是
  Tingting 音色与模型不匹配造成的假象。
- 自注入唤醒的**时序**比电平更麻烦（唤醒窗口 5 s、会话 15 s），所以自测只能证明
  "链路能唤醒"，不能替代真人 20 次的唤醒率/误唤醒统计。
- 真人测试（`scripts/p2_trials.py`）仍是 Gate G1 的唯一口径；工具和 runbook 就绪。

**自动语音自测工具（`tools/self_test_voice.py`）的边界**：它会注入唤醒词、等平面进入唤醒态再注入命令。
实测：唤醒腿通过（`wake_count +1`，`cue: 490 ms`），但**命令腿被平面自己的"回声保持/门控"吞掉**
（`gate_discarded=1`，随后 `wake with no speech after 5005 ms`）。结论：合成自注入可以验证
"麦克风→KWS"这条腿，但**不能替代真人完成一个对话回合**；P2 必须真人测。

## 另外两个发现（都写进 ROADMAP）

1. **低频漂移**：设备采集存在 ~0.05 Hz、峰值可达满幅的缓慢摆动（AEC 开/关都有）。
   ASR 不受影响（已实测转写正确），但会污染 `voice_rms_db/peak` 指标；
   rui 的高通只存在于 AecChain 里，单声道无参考时整条链被跳过。
   候选修法：adapter 侧加 90 Hz 二阶高通（与 rui AecChain 的 `HighPass` 一致），加之前先量前后 KWS/ASR。
2. **设备 AEC 测试模式**：本轮为验证声学回路，通过 config push 把设备 AEC 关掉（设备日志 `[aec] disabled`）；
   生产默认仍是 AEC 开（`DOT2_AEC_ENABLED` 不设即为开）。

## 复现命令

```bash
# 1) 证书与 token（0600，gitignore）
cp ~/AI_Workspace/01_Code/services/echomuse-controller/data/tls/{ca.pem,server.pem,server.key} secrets/

# 2) 设备指向本项目（endpoints[1] 是旧 controller 回滚位）
adb shell "su -c 'cat > /data/local/etc/echomuse/controller.json'" < controller.json

# 3) 本机 rui（只读 checkout）+ adapter
./scripts/dev_rui.sh
curl -s http://127.0.0.1:8091/health

# 4) 单独探针（不经过 rui，用于排查设备侧）
PYTHONPATH=src .venv/bin/python -m rui_dot2_bridge.standalone \
  --certs-dir secrets --tone --record /tmp/dot2-mic.wav --duration 30
```

## 未完成 / 需要人

- [ ] P2 真人唤醒率、误唤醒、打断延迟（工具与配置已就绪）
- [ ] NAS 部署（需要 NAS 上 rui 容器的卷路径，且等验收日镜像更新）
- [ ] P3 固件 fork：本机没有 Go 工具链（EchoMuse 需要 Go 1.24 + Android/arm 交叉编译）

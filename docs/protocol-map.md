# 协议映射：EchoMuse 设备 ↔ rui AudioFrame

## 一、帧格式对照

| 方向 | EchoMuse 设备线 | 本项目适配 | rui 侧 |
|---|---|---|---|
| 上行麦克风 | `/data` `[0x01][S16LE mono 16 kHz]`，连续流 80 ms/块 | 重新切块为 20–40 ms，包成 `AudioFrame(channels=1, sample_rate=16000, reference_channels=0)` | 平面：16 kHz → VAD/KWS → ASR |
| 下行语音 | `/data` `[0x02][S16LE mono 48 kHz]`，`[0x03]` 结束 | `play()` 的 PCM 直接切片加 `0x02` 头；`stop_playback()` 发 `speaker_flush`（或等 EOS） | 平面按 `playback.rate=48000` 交付 |
| 开始采集 | `/control` `{"type":"mic_start","lock_mic":false}` | `open()` 收到设备 `register` 并回 `ack` 之后下发 | 无感知 |
| 停止采集 | `/control` `{"type":"mic_stop"}` | `close()` | 平面退出 |
| 握手 | 设备→`register`，controller→`ack` | adapter 维持状态机 | `probe()` 返回 `DeviceInfo` |
| 心跳 | controller 每 20 s ping，30 s 无 pong 断开 | `websockets` 库自动回 pong；adapter 主动 ping 检测设备掉线 | 断线后 `frames()` 继续等，设备自己重拨 |

**不需要实现：** `0x04/0x05` 音乐、`0x06` BLE 广告、`0x07` 私密聆听会话、`0x08` GATT、`config` 推送、`wifi_*`、`shell_open`（除非要读设备环境）。

## 二、一次对话的时序

```
设备(1.0/1.1)              adapter（rui 音频面进程内）
  │── wss /control ──────────▶│  X-EM-Token 校验（TLS 用移植的 server.pem）
  │── {"type":"register"} ────▶│
  │◀── {"type":"ack"} ────────│
  │── wss /data ─────────────▶│
  │◀── {"type":"mic_start","lock_mic":false}
  │── [0x01][80ms mono] ─────▶│── AudioFrame ──▶ 平面 decimate/VAD/KWS
  │                            │        （KWS 触发 → turn → ASR → LLM）
  │◀── [0x02][48k mono]… ─────│◀── play() ──────── TTS 流式分句
  │◀── [0x03] ────────────────│◀── 句子结束
  │── …                            （barge-in：KWS 在播放中触发 → stop_playback）
```

## 三、需要 adapter 自己决定的三个点

1. **连续流 vs 受限轮次。** rui 自己的 KWS 需要连续音频（`mic_start` 不带 `lock_mic`）。
   用设备的 VAD 门控（`lock_mic:true`）会让 rui 的 KWS 只看到句子里的一截，且 KWS 模型要求连续输入。
   **默认选连续流**；设备自带唤醒设为 `off`，避免两套唤醒打架。
2. **帧大小。** 设备给 80 ms，rui 的平面按 `voice.frame_ms=20` 设计（但接受任意块）。
   adapter 内部按 20 ms 再切，代价是一点拷贝，收益是可预测的 KWS 延迟。
3. **播放速率。** rui 默认 16 kHz，设备下行要 48 kHz。**配置 `audio.playback.rate: 48000`**，
   让 rui 在调用 `play()` 之前完成重采样，adapter 里不做 DSP。

## 四、错误与降级

| 情况 | 行为 |
|---|---|
| token 不匹配 | adapter 记录并拒绝握手（不静默接受）；开发期可配置 `allow_any_token` 但启动时告警 |
| 设备断线 | `frames()` 不抛异常，继续等下一次 `register`（rui 的平面按此设计） |
| 设备版本缺 `register` 字段 | 忽略未知字段，按默认能力处理 |
| TLS 证书缺失 | adapter 启动失败并说明路径，不静默降级为明文（设备只认 wss） |
| 播放中设备卡顿 | `stop_playback()` 必须真正静音：发 `speaker_flush`，否则旧缓冲会和新回答叠在一起 |

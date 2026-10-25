# 真机验收 · 第 1 轮（rui 0.5.0 + 原生播放后静音，2026-10-25 18:21–18:30）

> 环境：`.34` = `rui-dot2:0.5.0`（base digest `sha256:01a3afe1…`）+ Dot 2（EchoMuse `20261024-1911-dev`，8870）；
> 唤醒走 rui KWS（中文「你好小瑞」），设备音量 unity、`EQ_100`；播放后静音用上游原生
> `devices[].audio.voice.post_playback_mute_ms=2500`（adapter 门控已撤）。

## 结果汇总

| # | 项目 | 结果 | 关键数据 |
|---|---|---|---|
| 1 | 唤醒→回合 | ✅ | KWS 命中→段 1.86 s→ASR 241 ms `现在几点？`→LLM 817 ms→TTS 首块 1590 ms；回复正确；`gap=0` |
| 2 | 播放中打断 | ✅ | 长回复（17.9 s 音频）中途唤醒：**服务端 16 ms** 清队 + `stop_play`，丢 17.9 s 排队；随后 ASR `停一下。` 命中 stop intent、会话关闭 |
| 3 | 音量 / LED | ✅（音量待 9 哥确认） | LED 序列 `wake→listening→thinking→speaking→listening`；ack「在呢」后有一个**小杂音**（见下） |
| 4 | 安静房间基线 | ✅ | 安静 2 分钟：`wake_count` 4→4（0 次误唤醒）、0 个伪造 turn、`capture_errors=0` |

## 发现 1：ack「在呢」后的小杂音（已修，属于我们 adapter）

- ack 490 ms = 11 个 42.7 ms 设备周期 + **20.7 ms 尾巴**；
- 旧逻辑：EOS 在最后整周期后 1.2 s 才触发，把 20.7 ms 尾巴补成整周期**在 1.2 s 后才发**；
  设备因此开了一个 `periods=1、primeWait≈1242 ms` 的幽灵流（16:23 的设备日志），起始突入 = 听到的"小杂音"；
- 修复（`adapter._eos_after_idle`）：**EOS 时丢弃 <30 ms 的尾巴**（人耳不可闻）；更长的尾巴加 10 ms 淡入 + 淡出、
  补齐到整周期再发（避免突入）。新增 `dsp.fade_in_pcm` 与两个单测。
- 验证：重建镜像后连放两次 ack.wav，设备日志只有 `periods=11` 正常收尾，**不再出现 `periods=1` 幽灵流**。

## 发现 2：KWS 唤醒被误标「during playback」（待 rui 侧确认）

三次日志：

| 时间 | 日志 | 当时实际 |
|---|---|---|
| 18:21:46 | `barge: 1 wake during playback, dropped 0.0 s queued` | 上一段播放已在 3 分钟前结束 |
| 18:22:28 | （干净，无 barge） | 正常 |
| 18:22:45 | `barge: 2 ... dropped 17.9 s` | 真实播放中打断（正确） |
| 18:23:07 | `barge: 3 ... dropped 0.0 s queued` | 上一轮被「停一下」关闭会话后 18 s |

现象：`barged = _speak_queued_s>0 or _playback_task 未完成`，丢 0.0 s 说明队列为空但 `_playback_task` 残留。
影响：唤醒被当成 barge，多走一次 stop/flush（无害），但会把 `_playback_task` 状态和 trace 弄脏。
代码位置：`src/rui/audio/process.py` `_device_wake_playing()` / `_on_kws_wake`。已作为观察反馈 rui#7，等他们确认。

## 发现 3：设备固件一次 SIGSEGV（低频）

`supervisor.log`：`2026-10-25 08:25:29 exit pid=1239 code=139 ran=64677s`（设备时钟 UTC，=本地 16:25），
发生在 OWW 打分器开启的实验期间；之后至今未再出现。当前 OWW 关闭。记录为低频风险，若复发再收集。

## 待补

- 9 哥确认「音量是否合适」「长回复结束时是否也有杂音」（短提示已确认修复）；
- 真人"唤醒后不说话/只说唤醒词"、连续多轮、距离变化等场景（可按需补）；
- 2 分钟安静基线偏短，后续在多设备测试会话里做更长的误唤醒统计。

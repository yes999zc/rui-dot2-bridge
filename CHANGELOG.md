# Changelog

## Unreleased — 2026-10-24（白天）

### Fixed

- 音量：上限从 30 %（−44.5 dB，几乎听不见）改为 **100 % = level 127 = unity**；
  修掉"上限被默认 30 二次夹"与"目标/回读混用"两个 bug，控制台现在显示设备实际值。
- 响度：stock EQ 阶梯 + 低架/高架 + 限幅 + trim −2 dB（A/B 通过）；修掉限幅器
  `abs()` 漏检导致的破音、换流状态残留导致的杂音、回复末尾的"嗒"声。
- 播放：`EOS_IDLE_S` 0.4 → 1.2 s，避免 TTS 断供时把短回复切成两段流重新 prime。
- 麦克风：设备预增益 24 → 18 dB，消除削波（语音段峰值 0.70–0.88）。
- 部署：override 的 `DOT2_VOLUME_CAP` 改为 100，EQ 曲线打进 bundle，`doctor` 默认新上限。

### Added

- `DOT2_MUTE=1`：静音扬声器但保留唤醒/ASR/打断。
- 响度运行时调参 `GET/PUT /loudness`（`DOT2_CONTROL_PORT`，dev 8872）。
- `tools/stock_eq.py`（离线 EQ 评估）与 `tools/eq_variants.py`（变体试听）。
- P2 记录器 v2：按 VAD 收尾计时、识别重复唤醒、`--replay` 复盘。
- 出站 PCM dump（`DOT2_PLAYBACK_DUMP`，调试用）。

### Fixed (playback holes)

- 每轮回复开头的 ~400 ms 空档：空闲时 `stop_playback()` 不再发 flush（flush 只在真正有播放时发），
  消除"首块已发、flush 才到"的跨 socket 竞态。
- 剩余 TTS 分片空档：adapter 加 **0.45 s 播放预缓冲**（`DOT2_PREBUFFER_MS`，短回复 0.35 s 后强制开始）
  吸收；代价是回复延迟 +~0.45 s。EOS 空闲 1.2 → 0.7 s。

### Filed upstream

- [yes999zc/rui#1](https://github.com/yes999zc/rui/issues/1)：播放回复出现 ~400 ms 中段空档
  （流式 TTS 到达与 Pacer 预存不匹配），附时间戳、设备日志与三条建议。

## v0.1.0 — 2026-10-24

First runnable version: the Dot 2 is a rui voice satellite, with **no changes to
rui's code** and **no reflash of the device**.

### Added

- `rui_dot2_bridge.device_link` — an EchoMuse controller the device dials:
  TLS wss on 8870 with the transplanted controller certificate, `X-EM-Token`,
  `register`/`ack`, `mic_start(lock_mic=False)`, `0x01` capture frames,
  `0x02`/`0x03` playback, `speaker_flush`, volume/LED/button messages.
- `rui_dot2_bridge.adapter` — the rui `DeviceAdapter`: 80 ms mono blocks cut to
  20 ms `AudioFrame`s, playback in whole 4096-byte periods with an idle EOS,
  barge-in flush, the 12-LED ring, and a hard 30 % volume ceiling enforced at
  connect, on rui requests, and after the physical buttons.
- `src/sitecustomize.py` + `scripts/dev_rui.sh` — registers `dot2` inside rui's
  parent **and** audio-plane child via `PYTHONPATH`, so the rui checkout and
  image stay untouched.
- `standalone.py` (probe without rui), `inject.py`/`inject_cli.py` (test-only
  audio injection), `tools/fake_device.py` (drives the adapter without hardware).
- Two-channel capture support behind `DOT2_REF_CHANNELS=1`, ready for the P3
  firmware that hands over the de-aliased ch8 reference.
- P3 firmware patch (`patches/0001-ref-uplink-v2.19.0-ea.1.patch`), cross-built
  with the upstream NDK image (ARM, sha256 `876f63b4…`), not installed.
- Tooling: `scripts/check.sh`, `doctor.py`, `device_health.py`, `p2_trials.py`,
  `soak.py`, `soak_report.py`, `make_deploy_bundle.sh`, `build_p3_firmware.sh`.
- Tests: 34 unit/integration tests (frame format, handshake, volume cap, LED,
  layout, EOS/flush), ruff clean.

### Measured on the hardware

- register/ack, `listen_state=stream`, 12.5 mic frames/s (80 ms), RTT 5–8 ms;
- TTS reply played with `periods=131 underruns=0`; text turn ~2 s to first audio;
- acoustic loop at 30 % volume: recorded mic → rui ASR returns the wake word and
  command correctly; the live plane logged `wake: 你好小瑞` on the matched voice;
- adapter stop → device on the old controller in 40 s; restore → back in 45 s;
- plane `kill -9` → watchdog rebuild in 3 s → device re-registered in 3 s.

### Known limits

- P2 wake/ barge-in rates need a human (the tooling is ready);
- rui has no AEC on a mono stream without a reference, so barge-in is off in
  v0.1.0 by design; P3 lifts that if the patch is accepted;
- the old EchoMuse controller pushes volume 0.669 (85/127) when it takes over —
  it stays stopped for unattended runs.

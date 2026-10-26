# Changelog

## [1.3.1] - 2026-10-26

### Changed

- Plugin版本 1.2.0 → **1.3.1**；`rui_compat` 维持 `">=0.6.0 <0.8.0"`（0.7.x 发布线内不需要再抬）。
- 与 rui 0.7.2 对齐：核心侧的 ack cue 回声抑制、按房间隔离的播放/回合状态、以及
  「唤醒时丢弃已入队我方语音」都不需要桥侧改动；桥的行为与 1.3.0 一致。

### Verified with rui 0.7.2

- 唤醒 30/30 admitted（6 句 × 5 轮，校准后 TTS 源）
- 语音指令 26/30（含音乐占据扬声器的场景）
- 播放中打断 8/8（真机人声）
- 音乐中短指令（下一首 / 停止播放）10/10（人声，1 m）

### Known limitations

- 设备端 OWW 只有英文模型；中文唤醒依赖 rui 服务端 KWS。
- EQ 只有测量结论（+24.65 → +2.11 dB、−12.85 → −2.19 dB、0 削波），**耳测未做**。
- 多台 Dot2 并发、音量与指示灯未做系统性验证。

## Unreleased — 插件 1.2.0：rui 0.6 manifest（audio_defaults）

### Changed

- `plugin.yaml`：插件版本 1.1.3 → **1.2.0**；`rui_compat` 从 `">=0.3.0 <0.6.0"` 改为
  **`">=0.6.0 <0.7.0"`**。上界跟随 rui 0.6 发布线；下界一并抬到 0.6.0，因为 1.2.0 的
  `devices/rs03qr.yaml` 声明了 v1-only 的 `audio_defaults`，而 rui 0.5.x 的
  `DeviceManifest` 是 `extra="forbid"`——版本门放行 0.5.x 只会在 manifest 校验处报
  `audio_defaults: Extra inputs are not permitted`。下界写清楚，旧核心得到的是
  "requires RUI >=0.6.0" 而不是一条误导性的字段错误。
- 仍跑 rui 0.5.x 的部署继续用插件 **1.1.3** + 部署侧 `devices[].audio.voice.ack_echo_ignore_ms`
  配置；本版本不做向后移植。

### Added

- `devices/rs03qr.yaml` 在 `rui-device/v1` 下声明 **`audio_defaults`**（rui 0.6 三层合并的
  最底层：manifest < 全局 `audio`/`ack` < `devices[].audio`）：

  ```yaml
  audio_defaults:
    capture: {rate: 16000, channels: 1, mic_channels: 1, ref_channels: 0, bits: 16}
    playback: {rate: 48000, channels: 1}
    ack_echo_ignore_ms: 2500
  ```

  新部署不再需要手写 `audio.capture` / `audio.playback` /
  `devices[].audio.voice.ack_echo_ignore_ms` 就能拿到正确值（显式配置仍然优先）；
  `ack_echo_ignore_ms: 2500` 与 1.1.3 的 adapter 侧播放门控配合，覆盖 rui 的 ack/echo 窗口。
  `compatibility.tested_with` 补 `0.6.0`。
- adapter 代码无改动（1.1.3 的播放期静音逻辑不变）。

### Verified

- 官方 `rui plug-test` 在 **rui 0.6.0 候选镜像** `ghcr.io/yes999zc/rui:sha-7d1c1f8`
  （repo digest `sha256:fc47849732834a42b9393257efb9c65999daaec7300e2bde14ad89b7dbe99963`，
  CI `docker-publish`，main @ `7d1c1f8`）上：**15 PASS / 0 FAIL / 2 WARN / 5 SKIP**，
  verdict `pass`；报告 `docs/contract-report-0.6.0.json`（含镜像 tag/digest 与 rui 版本）。
- 本地单测（rui 0.6.0）：**133 passed**，`ruff check src tests` 通过；
  `tests/test_manifest.py` 覆盖 1.2.0 的版本/兼容区间与 `audio_defaults` 字段、
  并在真实 loader 下断言三层合并的 manifest 默认值。

## Unreleased — 插件 1.1.3：播放期静音上行麦克风（关闭自听）

### Fixed

- **自听根因在 adapter 侧关闭**：rui 0.5.3 的 `devices[].audio.voice.ack_echo_ignore_ms`
  只控制 `vad.hold()` 的窗口长度，窗口内的音频仍会 **buffer-and-replay**。0.5.3 的
  canary 实测（`.34`）把 cue 回声在窗口内回灌（+2064 ms，窗口 2990 ms），adapter 的
  麦克风在播放期间是开着的，回声先进入 hold 缓冲，窗口一抬就被重放成“请求”，得到
  伪造 turn `"在呢。"`。插件 1.1.3 在声源处解决：**播放期间静音上行麦克风**
  （`PLAYBACK_LIVE_S = 0`，即 `DOT2_PLAYBACK_LIVE_MS=0` 语义），播放结束后继续静音
  **2500 ms**（`DEFAULT_PLAYBACK_GATE_MS = 2500`，与 rui 侧
  `post_playback_mute_ms=2500` 对齐，覆盖 rui 的 follow-up 窗口）。回声不再进入 VAD
  的 hold 缓冲，buffer-and-replay 无从重放。
- **打断（barge）不受影响**：barge 走设备唤醒事件（`barge_via_device`），不依赖麦克风
  音频；`stop_playback()`（barge/stop 时 rui 会调用）会立即解除门控，让用户当下这句
  话被听见。（rui 在每次回复第一块前也会调用 `stop_playback()`，随后的 `play()` 会
  重新设置门控。）
- 参考通道保留：`_gate_mic()` 只把麦克风通道置零，`ref_channels` 不受影响。

### Changed

- `plugin.yaml` 插件版本 1.1.2 → **1.1.3**（`rui_compat ">=0.3.0 <0.6.0"` 不变；
  rui 0.5.3 无设备/插件契约变更）。
- `health()` 增加 `playback_gate_ms` / `playback_live_ms`，排障时可直接看到门控窗口。
- 单测：播放静音窗口测试重写为 13 项（默认 0/2500、播放期静音、窗口边界、关闭/覆盖
  开关、`stop_playback` 解门控、frames 置零/放行、静音期设备唤醒仍可 barge）。

### Env

- `DOT2_PLAYBACK_GATE_MS`（默认 **2500**；`0` 关闭播放后静音）。
- `DOT2_PLAYBACK_LIVE_MS`（默认 **0**；>0 恢复旧行为“播放中/排空期保持麦克风”，会把
  回声重新放进 rui 的 hold 缓冲，仅作诊断，不建议生产）。
- `DOT2_PLAYBACK_DUMP`（调试：把下发设备前的 PCM 落盘）。

## Unreleased — rui 0.5.2 验证

### Added

- `docs/contract-report-0.5.2.json`：官方 `rui plug-test` 在 **0.5.2** 上 **15 PASS / 0 FAIL / 2 WARN / 5 SKIP**，22 项逐项与 0.5.1 一致（仅版本字符串与时间戳不同）。
- 版本钉从 0.5.1 更新到 **0.5.2**（README 徽章/构建示例、`docs/agent-install.md`、`deploy/downstream/README.md`、`deploy/downstream/Dockerfile` 的 `ARG RUI_IMAGE` 默认值、`docker-compose.yml` 默认 `image`）；`devices/rs03qr.yaml` 的 `tested_with` 追加 `0.5.2`。

### Verified（真机，`.34` + Echo Dot 2 / EchoMuse `20261024-1911-dev`）

- `POST /api/v1/devices/dot2/test` → `ok:true`、`transport:inbound`、host 检查 `not_applicable`（Dot 2 无 SSH，不再误判）、token 检查 `managed_by_adapter:true`、`listen_port:8870`；
- `GET /api/v1/devices/dot2/status` → `ready:true`、`errors:[]`（不再 `token_missing`）；
- 真实回合：`audio_bytes=810130`、`playback_gap_count=0`；容器重建后设备 **10.7 s** 重连（≤60 s 判据）。
- 无 adapter 代码改动，`rui_compat ">=0.3.0 <0.6.0"` 不变。

## Unreleased — 跟随 rui 0.5.1

### Changed

- 文档与默认值全部从 rui **0.5.0** 更新到 **0.5.1**（README 徽章/构建示例、`docs/agent-install.md` 的步骤与
  `.env` 示例、`deploy/downstream/README.md`）。`deploy/downstream/Dockerfile` 的 `ARG RUI_IMAGE` 默认值也从
  `0.3.4` 改为 `0.5.1`：不带 `--build-arg` 直接构建时不再悄悄落到 0.3.4。
- `deploy/downstream/docker-compose.yml`：`image` 与 `RUI_BASE_IMAGE` 的默认值改为 `0.5.1`。
- `deploy/rui-dot2-override.yml`：service 名 `house-ear` → **`rui`**（0.2+ 上游 compose 的容器名），挂载路径改为
  `${DOT2_REPO_DIR:-/opt/rui-dot2-bridge}` 由部署方提供（仓库不再带具体宿主路径），并修正注释里音频面子进程的模块名
  （`rui.audio.process`）。

无 adapter 代码改动；`rui_compat "≥0.3.0 <0.6.0"` 不变。

## 2026-10-25 — 开源（MIT）

- 项目以 **MIT** 许可开源；新增 `LICENSE`、面向新用户的 `README.md` 落地页（含"让 agent 帮你安装"入口）与
  `docs/agent-install.md`（给 AI agent 的执行手册）。
- README 补齐前提条件（Echo Dot 2 / firebreak 解锁 / EchoMuse 固件 / rui 版本）、刷机流程摘要、手动安装与验收清单、
  已知限制、上游致谢（EchoMuse、amonet/firebreak、rui、openWakeWord）。


## Unreleased — rui 0.5.0 兼容

### Fixed

- **ack/短回复后的"小杂音"（两段修）**：设备周期 42.7 ms，490 ms 的「在呢」剩 20.7 ms 尾巴。
  ① 旧逻辑在 EOS（最后整周期后 1.2 s）才把尾巴补成整周期发出，设备因此开一个 `periods=1` 的幽灵流，
  起始突入就是杂音；② 直接丢弃尾巴又会在最后一个音节处硬切（"和'呢'一起出现"的杂音）。
  最终策略（9 哥 18:44 确认"没有了"）：**短流（≤1.5 s，cue/filler）用 0.15 s 短 EOS 空闲**，尾巴**续在同一流里**
  （不加淡入——淡入会把最后一个音节做出 tick）、只在真正结尾做 10 ms 淡出并补静音到整周期，随后 EOS；
  **长流**保持 1.2 s EOS 空闲，<30 ms 尾巴直接丢弃。设备日志 `periods=12`、同一秒收尾，无幽灵流；
  `DOT2_PLAYBACK_DUMP` 抓到的下发 PCM 衔接跳变 105（可忽略）、末样本 0。新增 3 个播放收尾单测。

### Changed

- `plugin.yaml`：`rui_compat` 从 `>=0.3.0 <0.5.0` 抬到 **`>=0.3.0 <0.6.0`**，插件版本 1.1.1 → **1.1.2**。
  rui 0.5.0（对外发布版）的版本门会拒绝 `<0.5.0`；维护者在 rui#7 确认 0.5.0 **无设备/插件契约变更**
  （Phase 2 多平面/仲裁全部在 rui 内部，`drain_wakes()` 时序与 `DeviceWake` 六字段不变），只需要改上界。
  配置侧新增两条（不启用多设备时行为不变）：`devices[].room` 在 `multi_device.enabled=true` 时必填、
  `devices[].audio.voice.post_playback_mute_ms`（默认 0，设备级播放后静音窗口）。
- adapter 侧无代码改动；`devices/rs03qr.yaml` 的 `tested_with` 更新到 0.5.1（契约自测逐版本 15/0/2/5）。

## 2026-10-25 — rui 0.4.x 兼容

### Changed

- `plugin.yaml`：`rui_compat` 从 `>=0.3.0 <0.4.0` 放宽到 **`>=0.3.0 <0.5.0`**，插件版本 1.1.0 → **1.1.1**。
  rui 0.4.0 的 `device/`、`plugins/`、`audio/` 契约模块与 0.3.4 相同（0.4.0 只改了内置插头的版本区间和
  控制台 API），原区间会被 `load_plugin()` 的版本门直接拒绝；无 adapter 代码改动。

## 2026-10-25 — P4 接通：rui 0.3.4 + Socket/Plug v1 + 下游镜像

### Added

- **Socket/Plug v1 插头**：`plugin.yaml` + `devices/rs03qr.yaml`（`rui-device/v1`，class B，trust trusted，
  `runtime.listen_port=8870`、`token_env=DOT2_EM_TOKEN`）随 adapter 包发布；官方入口改为
  `RUI_ADAPTER_PLUGINS=dot2=rui_dot2_bridge.adapter:build`，删除 `sitecustomize` 注入。
- `wake.py`：EchoMuse `oww_wake` → v1 `DeviceWake` 六字段（单调 `ts_ms`、0–1 归一化、严格 bool `barge`、
  dBFS `level`、无效事件带原因拒绝）；`drain_wakes()` + `BoundedWakeQueue(32)`。
- `deploy/downstream/`：官方镜像 + adapter 层的下游镜像、compose、manifest 安装脚本与 README。
- 契约自测：官方 `rui plug-test` 在 0.3.4 上 15 PASS / 0 FAIL / 2 WARN / 5 SKIP（`docs/contract-report-0.3.4.json`）。
- 文档：`docs/handoff-2026-10-25-p4.md`、`docs/p4-device-wake-feasibility.md`、`docs/p4-e2e-runbook-0.3.4.md`、
  `docs/deploy-0.3.4-user-report.md`。

### Changed

- adapter 迁移到 `rui.device.contract`：`probe()` 只在设备真的能打分时宣告 `device_wake`/`barge_via_device`；
- 音量两层单位：`set_volume()` 0–100 ↔ `services` 0–255 ↔ 设备 0–127（越界拒绝，不静默 clamp）；
- `build(dev, cfg)` 优先级 DeviceConfig > params > `DOT2_*` env > 插件缺省；X-EM-Token 支持 `<secrets_dir>/device_token` 文件回退（env 优先，日志只记来源）；
- 测试主机（<lan-ip>）从 rui 0.2.2 升级到 **0.3.4**，再切到下游镜像；设备以 `devices[].adapter=dot2` 接入 8870。

### Fixed

- **自听**：播放门控拆成两段——播放中保持麦克风（打断可用），设备排空尾巴 0.35 s 后静音到 `DOT2_PLAYBACK_GATE_MS`
  （生产 2500 ms）。修复 rui follow-up 窗口把残响转录成回合（实测 18.6 s 长回复后不再产生第二段 turn）。
- `audio.playback.rate` 16 kHz → **48 kHz**（设备线格式），否则每条回复都被 adapter 拒绝播放；
  capture 改为 16 kHz / 1ch / 0ref，rui 侧 AEC 关闭（AEC 归设备）。

## 2026-10-25 — 0.2.2 迁移暂停 / AEC 结论 / 补丁落地

### Added

- P3 固件补丁独立成仓库 **echomuse-dot2-ref**（MIT，v0.1.0 release + GitHub Actions 构建 + A/B 安装/回滚脚本）；
  已安装到设备 server_b（`20261024-1911-dev`），server_a 保留旧固件。
- rui v0.2.2 测试实例（官方 Docker 镜像，控制台 8080，设备入口 4412）以"纯用户视角"部署成功，报告
  `docs/deploy-0.2.2-user-report.md`。
- AEC 三份调研落库：`aec-rui-analysis.md`（线性上限 14–18 dB）、`aec-device-tuning.md`（设备 AEC 稳态 14–18.2 dB，
  tail/AGC 在当前拓扑无效）、`aec-parameter-model.md`（who/reference/constraints 参数模型）。
- 误唤醒基线：20 次文件播放（三档音量 + wakeMic 0/6）0 误唤醒、0 空唤醒；确认"自听"是 `debug/say` 的 turn 记录假象。
- 迁移计划与契约问题：`docs/next-deployment-plan.md`、`docs/rui-0.2.2-dot2-compat.md`、`docs/rui-0.3-requirements.md`；
  向 rui 提 [#3](https://github.com/yes999zc/rui/issues/3)（device_wake）与 [#7](https://github.com/yes999zc/rui/issues/7)（0.2.2 接入契约）。

### Changed

- 生产配置定为 `ref_channels=0` + 设备 AEC；`DOT2_PLAYBACK_GATE_MS` 默认 0（可选半双工兜底）。
- 设备 `controller.json` 只保留测试实例一个 endpoint（移除 Mac/旧 IP 兜底）。

### Removed

- 测试主机上的旧 systemd 部署、代码仓、venv、模型、状态目录全部清理。

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

- 插头 manifest 盲开 v0：`plug.json` + `src/rui_dot2_bridge/plug.py`，`build()` 按
  「manifest < `DOT2_*` 环境变量 < rui DeviceConfig」取值；`doctor.py` 增加 manifest 自检；
  部署 bundle 自带 `plug.json`。
- `DOT2_MUTE=1`：静音扬声器但保留唤醒/ASR/打断。
- 响度运行时调参 `GET/PUT /loudness`（`DOT2_CONTROL_PORT`，dev 8872）。
- `tools/stock_eq.py`（离线 EQ 评估）与 `tools/eq_variants.py`（变体试听）。
- P2 记录器 v2：按 VAD 收尾计时、识别重复唤醒、`--replay` 复盘。
- 出站 PCM dump（`DOT2_PLAYBACK_DUMP`，调试用）。

### Fixed (playback holes)

- 每轮回复开头的 ~400 ms 空档：保留 `speaker_flush`（设备 prime 旁路需要它），但在同一条
  control 连接上加 **ping/pong 屏障**，保证 flush 先于首帧被处理，消除跨 socket 竞态。
- 剩余 TTS 分片空档：adapter 加 **0.45 s 播放预缓冲**（`DOT2_PREBUFFER_MS`，短回复 0.35 s 后强制开始）
  吸收；代价是回复延迟 +~0.45 s。实测设备 `primeWait` 17–22 ms、`underruns=0`、流内 `maxGap` 123–156 ms。

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
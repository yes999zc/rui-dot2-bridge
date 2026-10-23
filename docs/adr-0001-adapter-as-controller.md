# ADR-0001：用 rui adapter 顶替 EchoMuse controller

- 状态：已接受（2026-10-24）
- 决策者：9 哥 / Lead
- 影响范围：项目形态、部署方式、设备是否需要改固件

## 背景

Dot 2 已经跑通 EchoMuse 全链路：设备端固件 v2.18.1 + controller v2.26.1（OrbStack Docker）+ HA ESPHome 卫星。
但这条链路的"大脑"是 HA Assist，而 rui 自己就是一个完整助手（KWS → VAD → ASR → LLM → TTS → 播放仲裁）。
要把它接进 rui，必须先回答：**EchoMuse controller 是保留、改造，还是替换？**

## 选项

### A. 保留 EchoMuse controller，把 rui 接到 HA 后面
**否决。** 拓扑反了：rui 会变成 HA 的一个后端，而 rui 的 session/turn/tool/播放仲裁全部用不上；
两套编排（controller 的 pipeline + rui 的 coordinator）互相打架，延迟和故障面都翻倍。
而且 EchoMuse controller 的 pipeline 是硬编码 HA Assist 的，没有"换后端"的接口。

### B. Fork EchoMuse controller，把它的助手后端换成 rui
**否决。** 它是 Go（设备侧）+ Python（controller）的一整套系统，替换后端等于重写它的编排层；
还要长期跟上游合并。我们要复用的只是**设备协议**，不是它的编排。

### C. （采纳）写一个 rui 侧 adapter，由它直接扮演 controller
adapter 在 rui 的音频面进程里起 `wss://` 服务端，讲 EchoMuse 设备协议，把设备帧翻译成 rui 的 `AudioFrame`；
播放方向把 `adapter.play()` 的 PCM 打成 `0x02` 帧。设备无感知，rui 拥有全部对话逻辑。

**为什么不需要改 rui：**
- `DeviceConfig.adapter: str`（不是 enum），配置里写 `dot2` 就能过校验；
- `house_ear/device/registry.py` 提供 `register(name, target)`，注释写明就是给"downstream build"加设备用的；
- rui 的音频面是子进程 `sys.executable -m house_ear.audio.process`，继承环境变量 → `PYTHONPATH` + `sitecustomize.py` 能在父进程和平面子进程里都完成注册。

**关键复用：**
- 设备端 TLS 证书（`data/tls/`）原样搬过来，设备不用重刷、不用重新配网；
- 设备静态地址文件 `controller.json` 让发现过程不再依赖 mDNS。

## 后果

**正面**
- 一套对话逻辑（rui），没有第二套编排；
- 部署面只有 rui 容器 + 一个挂载进去的 Python 包，回滚到 EchoMuse controller 只需删一个文件、重启旧容器；
- 设备侧零改动（Phase 1/2）。

**代价**
- 我们要实现设备协议的**子集**（`register/ack`、`mic_start/mic_stop`、`0x01/0x02/0x03`、keepalive、TLS+token）。协议有 500 行文档 + 上游源码可对照，子集不大。
- 设备上行只有 **单声道 16 kHz（已过设备内 AEC/波束/AGC）**，没有参考通道。rui 的 AEC/barge-in 在 `ref_channels < 1` 时会被显式关掉（`audio/process.py:374`）。barge-in 能否靠"设备侧 AEC 已经把回声压掉"来保住，需要 Phase 2 实测。
- 若实测不达标，Phase 3 走"改设备固件送 9 声道原始流"的升级（见 ROADMAP）。

## 被记录但未采纳的变体

| 变体 | 为什么不选 |
|---|---|
| 让 adapter 伪装成 LX06 客户端，配置写 `adapter: xiaoai` | 零成本冒烟可以（Phase 0 备选），但会向 rui 谎报 `REFERENCE`/`MIC_ARRAY`，还继承 `mysoftvol`、厂商播放暂停等 LX06 专属路径。只用于一次性验证链路。 |
| adapter 做成独立 bridge 服务，rui 通过本地 ws 连它 | 多一个进程、多一层协议、多一处故障点。rui 的平面本来就会拉起 adapter，进程内更简单。 |
| 换掉设备端固件，直接对 rui 送原始 9 声道 | 硬件接管、AEC、灯环、按键全都要重写，且 483 MB 内存上风险高。留作 Phase 3 的条件项，不作为起点。 |

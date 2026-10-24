# 对 rui 侧接口的需求（等待官方定稿）

> 状态：**暂停开发**。9 哥已把请求转给 rui 侧，等其回复并完善 plug/接口文档后，我们再照定稿实现。
> 本文只列"我们依赖什么"，不代表 rui 的最终方案。

## 1. 官方的 out-of-tree adapter / plug 入口

现状（v0.2.2）：声明式 plug（`plugin.yaml` + `devices/*.yaml`）由 loader 读取，但 **loader 不执行
`entrypoint`**；可执行 adapter 只能用 `PYTHONPATH + sitecustomize + registry.register` 注入，属非官方，
镜像升级会断。

需要：一个**有文档、受支持**的入口，让第三方 adapter 在官方镜像里注册并随版本升级存活。
形态可以是 downstream build 钩子、plugin entrypoint、或 adapter 侧车进程——由 rui 定。

## 2. 多音箱共存 + 每设备协议

卫星音箱天然是多台（LX06 + Dot2 + 未来设备），且各自的内层协议不同：

- LX06：一条 ws，明文 + query token；
- EchoMuse/Dot2：TLS WebSocket + `X-EM-Token` 头，`/control` + `/data` 两条逻辑通道。

v0.2.2 是单入口（4412）+ 一个音频面驱动一台设备，没有协议 demux。需要：

- 每设备独立监听端口（Dot2 用 8870 wss，设备侧配置不变），或
- 单入口内按 adapter 做协议分发，或
- 每设备一个音频面/adapter 实例。

**唤醒仲裁由 rui 定**：多个音箱同时听到唤醒词时，谁接管是 rui 的策略，不是音箱自己决定。
这要求 rui 能同时看到多台设备的 wake/音频状态。

## 3. 设备能力/参数模型（开放 vs 封闭）

按 9 哥定的规则，音箱分两类，参数要能表达：

- `openness`：closed（厂商 DSP 不可控）/ semi / open（固件可改）；
- `aec.who`：device / controller / both / none（开放设备倾向 device 自己做）；
- `reference`：source（none / device_loopback / vendor_tap）、channels、same_frame、dealiased、已知延迟；
- `wake.source`：controller / device（开放设备本地唤醒）；
- 每设备覆盖（`devices[].aec` 等），不是全局一个 `audio.aec`。

## 4. 设备侧唤醒（device_wake）契约

开放设备可本地检测唤醒并上报（EchoMuse 已有 `oww_wake`：score/threshold/barge/level）。
需要 rui 提供消费端：设备唤醒 → rui 按多设备策略决定是否起回合/打断；封闭设备继续走 rui KWS。
现有 `VendorSignalMsg.kind="wake"` 是"厂商助手让路"，不能复用。

## 5. 可观测性

- adapter 扩展健康/指标（ref ucap、device AEC att、playback_stats）希望能在控制台/API 看到；
- 没有 per-block 播放头的设备（Dot2）需要 play-head 的替代契约，pacer 才不用靠 adapter 预缓冲硬撑。

## 我们的暂停状态

- `.34`：rui v0.2.2 Docker 栈运行中（8080 控制台，healthy），`devices: []`；
- Dot：`controller.json` 只指向 `.34:8867/8870`，正在重试（预期离线）；
- rui-dot2-bridge：**不改**，等接口定稿后再迁移到 0.2.x/0.3 契约；
- AEC 归属、2ch 取舍：等 rui 侧回复。

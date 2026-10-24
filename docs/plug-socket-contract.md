# 插头 / 插座 契约（方向记录，2026-10-24）

> 状态：**方向已定，rui 侧插座尚未开发，先按本文件"盲开"**。
> 本文件记录 9 哥 2026-10-24 上午口述的工作界面，以及本方（音箱侧）在拿到
> 正式规范前采用的临时假设。拿到 rui 的插座规范后，本文件应以那份规范为准修订。

## 目标

两条维护线互不依赖：

```
音箱侧仓库（本仓库）                         rui 侧仓库
硬件 / 固件 / DSP / 音量刻度 / LED / 重连      turn / ASR / LLM / TTS / AEC 策略 / 播放仲裁
        │                                        ▲
        └────────── 插头（声明 + 实现） ──────────┘ 插座（规范 + 校验 + 接入）
```

- rui 定义"插座规范"：插头字段、校验、接入/拔出方式、版本语义；
- 音箱侧按规范交付"插头"：一台设备 = 一份声明式 manifest + 一个适配实现；
- rui 不出现任何品牌名；音箱侧不依赖 rui 内部实现；
- 加新音箱 = 新做一根插头，rui 不改代码。

## 边界归属（已达成）

| 归音箱侧（插头） | 归 rui（插座） |
|---|---|
| 硬件能力、采样率/通道/参考通道 | 唤醒词、VAD |
| **音量刻度与 dB 曲线**（如 0.5 dB/步、127=unity） | ASR / LLM / TTS |
| LED 语义、按键、静音 | AEC 谁来做（device / rui / none） |
| 连接方式（inbound/outbound、TLS、token） | 会话、工具、播放仲裁、打断策略 |
| 重连、健康、固件参数 | 多设备编排 |
| 设备默认参数 | 部署覆盖与运行时命令（受插头约束） |

原则：**物理约束归插头，对话策略归插座**；部署覆盖和运行时命令不得违反插头声明的物理约束
（例如 `scale_max=127`，任何 >127 的请求都由插头侧夹住并上报）。

## 插头草案（盲开版，字段名待插座规范确认）

```json
{
  "plug": "dot2-echomuse",
  "version": "1.0.0",
  "requires_socket": ">=1",
  "identity": { "model": "Echo Dot 2 (RS03QR)", "vendor": "Amazon", "firmware": "EchoMuse v2.18.1" },
  "transport": { "direction": "inbound", "scheme": "wss", "port": 8870, "auth": "X-EM-Token" },
  "audio": {
    "capture":  { "rate": 16000, "channels": 1, "mic": 1, "ref": 0, "frame_ms": 20, "format": "s16le" },
    "playback": { "rate": 48000, "channels": 1, "format": "s16le", "max_frame_bytes": 4096 }
  },
  "volume": { "scale_max": 127, "law": "db_linear", "db_per_step": 0.5, "unity_at": 127, "ceiling_percent": 100 },
  "capabilities": { "led": { "count": 12 }, "keys": true, "barge_in": false, "aec": "device" },
  "health": { "fields": ["connected", "firmware", "rtt_ms", "dropped_audio"] },
  "limits": { "uplink_kbps": 32, "notes": ["mono/no-ref => rui disables AEC+barge-in"] },
  "entrypoint": "rui_dot2_bridge.adapter:build"
}
```

P3 之后（固件送出 2ch 参考）只需改插头声明：`capture.channels=2, ref=1, capabilities.aec="rui"`，
实现由同一 entrypoint 提供，插座不需要知道设备型号。

## 盲开假设（插座规范出来前，本方按此实现）

1. 交付形态：**Python 包（提供 `build` entrypoint）+ `plug.json`（声明）**；
2. 插座读取 manifest → 校验 `requires_socket` → 调用 entrypoint 构建 adapter；
3. 部署覆盖（端口/token/音量上限）通过环境变量注入，且由 manifest 声明可覆盖项；
4. 无法识别的字段：插座应忽略而不是拒绝（前向兼容），插头侧同理；
5. 版本语义：manifest 的 `version` 是插头版本，`requires_socket` 是插座最低版本。

以上任一条与最终规范冲突，以规范为准；冲突点记在本文件"待确认"。

## 待确认（等 rui 侧插座规范）

- [ ] 插头是"包 + manifest"，还是"纯 JSON + 固定启动约定"？
- [ ] 参数注入入口（文件 / API / 控制台），哪一个是正式插座口？
- [ ] `requires_socket` 的比较规则（semver？整数？）
- [ ] 健康/遥测字段的命名与单位
- [ ] 插头的安装/卸载/回滚由谁执行

## 本方下一步（盲开阶段，均不改 rui）

1. 把现散落在 `config.yaml`、`dev/rui/env`、构造函数里的参数收拢成 `plug.json`；
2. `adapter.build()` 改为只从 manifest + 显式覆盖读参数；
3. `scripts/doctor.py` 增加"插头自检"：manifest 合法性、与当前运行参数的差异；
4. 等插座规范落地后做一次对插，不再重写实现。

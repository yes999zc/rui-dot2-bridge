# 响度：原厂 EQ 复刻 + 限幅（可运行时调参）

> 2026-10-24 9 哥 A/B 试听结论：**响度提升明显、无失真、人声清晰度不受影响、
> 只加 EQ 会爆音 → 采用「EQ + 限幅」**。本文件是它的参数与接口说明。

## 为什么需要这一层

- Dot 2 的音量刻度是 dB 线性（0.5 dB/步，127 = 0 dB），**30% 只有 −44.5 dB**；
- 即使 100%，原厂 Alexa 在 unity 之上还叠了一级播放 EQ（`EQ_50…EQ_100`，按音量
  边界 50/60/70/80/90/100% 选择），EchoMuse 是平直输出，所以比原厂薄/轻；
- 原厂 100% 曲线 `EQ_100` 对 1 kHz 是 +13.7 dB、150 Hz 是 +39.9 dB，直接应用会
  削波（实测 7 秒音频 91k 个样本削波），所以**必须有限幅**。

## 实现

```
48 kHz mono s16 ──► [EQ] ──► [trim] ──► [look-ahead limiter] ──► 设备 0x02 帧
                    │ stock: 按设备音量选 EQ_50…EQ_100（1024-tap FIR，overlap-add）
                    └ custom: 任意条参数 EQ（RBJ biquad：lowshelf/peaking/highshelf）
```

- 曲线系数来自设备原文件 `/system/vendor/etc/audio-algorithms/EQ_*.cfg`，**只读拉取**，
  放在 `artifacts/eq-prototype/`（vendor 文件，已 gitignore；部署时放 `secrets/eq/`）；
- 限幅器：5 ms look-ahead（**带 5 ms 延迟线，跨块生效**）、−1 dBFS ceiling、80 ms release，硬限 −0.1 dBFS；
  早期版本按 100 ms 块各自前瞻，块尾会欠限幅，集成后听感比离线渲染差——已修；
- 附加延迟约 10.7 ms（1024-tap 线性相位 FIR 的群延迟）+ 0（限幅用前瞻而非延时）；
- CPU：7 秒音频离线处理 < 0.5 s；实时每 100 ms 块一次 FFT/卷积，约占单核 2–4%。

## 参数（`LoudnessConfig`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 总开关（A/B 用） |
| `mode` | `stock` | `stock`（FIR 阶梯）/ `custom`（参数 EQ）/ `off` |
| `curve_ladder` | true | true=跟随音量选曲线；false=固定 `EQ_100` |
| `trim_db` | **−2** | EQ 后、限幅前的整体增益（±dB）；2026-10-24 试听定：−2 dB 去掉句尾"刺耳" |
| `limiter_ceiling_db` | −1.0 | 限幅目标峰值 |
| `limiter_lookahead_ms` | 5.0 | 前瞻窗口 |
| `limiter_release_ms` | 80.0 | 释放时间 |
| `lowcut_hz` / `lowcut_db` | 250 / **6** | EQ 后的低架削减（原厂低频提升在小喇叭上会失真） |
| `highshelf_hz` / `highshelf_db` | 4000 / **2** | 高频轻微收敛（去掉"偏尖"的听感） |
| `bands` | [] | `custom` 模式：`{type, freq, gain_db, q}` 列表 |

当前默认值 = 2026-10-24 试听选定的 **v11**：`EQ_100 + 低架 −6 dB@250 Hz + 高架 −2 dB@4 kHz + 限幅 −1 dBFS`，
A 计权响度与 v0 相同（−14.8 dBFS），低频能量 89% → 82%，无失真、无削波。

## 运行时接口（给 rui 后台调）

adapter 在 `127.0.0.1:${DOT2_CONTROL_PORT}`（dev 默认 8872）提供最小的 JSON/HTTP：

```bash
# 查当前状态（含已加载曲线、当前曲线名）
curl -s 127.0.0.1:8872/loudness | jq .

# A/B：关掉/打开
curl -s -X PUT 127.0.0.1:8872/loudness -d '{"enabled":false}'
curl -s -X PUT 127.0.0.1:8872/loudness -d '{"enabled":true}'

# 改整体增益
curl -s -X PUT 127.0.0.1:8872/loudness -d '{"trim_db":2.0}'

# 切到参数 EQ（rui 后台做"精细 EQ"面板就用这个）
curl -s -X PUT 127.0.0.1:8872/loudness -d '{
  "mode":"custom",
  "bands":[
    {"type":"lowshelf","freq":150,"gain_db":6,"q":0.7},
    {"type":"peaking","freq":1000,"gain_db":3,"q":1.0},
    {"type":"peaking","freq":2800,"gain_db":-4,"q":1.2},
    {"type":"highshelf","freq":6000,"gain_db":2,"q":0.7}
  ],
  "limiter_ceiling_db":-1
}'
```

- `PUT` 是**局部更新**：只传要改的字段；
- 立即生效（音频面下一块开始），并落盘到 `DOT2_LOUDNESS_FILE`（默认 `dev/rui/loudness.json`），
  重启后保留；
- 这是**插头的一部分**：manifest 里声明 `control.loudness = {bind: 127.0.0.1:8872, routes: [...]}`，
  插座（rui）可以把它代理成设备设置页；`DOT2_CONTROL_PORT=0` 可关闭；
- 协议故意做成最小 HTTP（无框架），若插头规范要求其他形式（JSON-RPC/MCP），只换这一层。

## 相关文件

| 路径 | 作用 |
|---|---|
| `src/rui_dot2_bridge/dsp.py` | FIR/参数 EQ/限幅/配置 |
| `src/rui_dot2_bridge/control.py` | 127.0.0.1 HTTP 控制面 |
| `tools/stock_eq.py` | 离线评估/生成对比样音 |
| `artifacts/eq-prototype/` | 从设备拉下的 `EQ_*.cfg` + 试听样音（gitignore） |
| `docs/eq-listening-guide.md` | 试听流程与结论 |

## 发布注意

1. `EQ_*.cfg` 是设备 vendor 文件，**不进 git**；部署 bundle 默认不含它们。NAS 部署时用
   `adb` 从设备拉一次放到 `secrets/eq/`，或直接从 Mac 的 `artifacts/eq-prototype/` 拷贝；
2. 缺曲线时 `mode=stock` 会**降级为平直**并告警，不会静音或崩溃；
3. 与 P3（rui 接管 AEC）无冲突：这一层只处理下行播放，不碰采集/参考通道；
4. 如果 rui 未来自己做响度/EQ，把 `enabled=false` 即可让插头侧完全让位。

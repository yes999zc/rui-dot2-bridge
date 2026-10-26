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
- 附加延迟 **15.67 ms** = FIR 群延迟 10.67 ms（1024-tap 线性相位）+ 限幅器前瞻延迟线 5 ms
  （旧文档只写了 FIR 的 10.7 ms，漏算限幅器；`fade_out_pcm` 的淡出就是为这段延迟补的）；
- CPU：7 秒音频离线处理 < 0.5 s；实时每 100 ms 块一次 FFT/卷积，约占单核 2–4%。

## 2026-10-26 修正：EQ 阶梯退化与低频过冲

**实测**（`tools/eq_report.py`，生产同一条 `LoudnessProcessor` 路径）：

- `EQ_50/60/70.cfg` 三个文件 md5 相同（`5ae935…`）。而且**六条曲线的相对形状完全一样**
  （150-250 Hz +26 dB、2-3 kHz −8…−12 dB），只是整体增益不同 ⇒ 原厂"阶梯"其实只有
  "一个音色 × 几个音量"，50/60/70 连增益都一样。
- 旧默认链（EQ_100 + 低架 −6@250 + 高架 −2@4k + trim −2 + 限幅 −1）在 10 s
  粉噪+60 Hz 鼓点（RMS −15.4 dBFS）上：100–300 Hz 比 1–3 kHz **高 +24.7 dB**，
  3–6 kHz 低 **−3.1 dB**，限幅器平均 **−12.9 dB**（最深 −18.1 dB），波峰因数被压到 7–8 dB。
  这就是真机"沉闷、像在缸里"的可测来源。

**修正后的目标**（`ladder_correct: true`，默认）：

- 六个档位按 **tier 0…5** 各自设计一条曲线（不再依赖 vendor 文件是否重复）：
  100–300 Hz 的提升随档位**单调下降**（+5.9 → +2.35 dB），3–6 kHz 的 presence 随档位
  **单调上升**（0 → +1.3 dB）；100 Hz 点 +6.3 → +3.5 dB，4 kHz 点 −0.2 → +2.4 dB。
- 设计依据：小喇叭在 100–300 Hz 抬 20 dB 以上会变成"桶声"，而 2–4 kHz 的塌陷直接
  对应"闷"；等响曲线在低音量需要更多低频补偿，所以低档保留更多 bass、高档更平直。
- **20–47 Hz 二阶滚降**（driver 放不出来，只会喂限幅器）：目标曲线在 **8192 点网格**
  （5.9 Hz/bin）上设计，再截取中心 **1024 taps**，所以 20–47 Hz 真的被塑形，而不再
  受 1024 点网格首 bin = 46.9 Hz 的限制（旧实现的 `freqs[0]=freqs[1]` 让 EQ_100 在
  20 Hz 只测到 −2.6 dB，等于没有滚降）。整链实测 EQ_100：20 Hz **−15.9 dB**、
  30 Hz −9.4 dB、47 Hz **−3.0 dB**、60 Hz −0.1 dB（相对 1 kHz）；群延迟仍是
  512 samples = **10.67 ms**，与 vendor FIR 相同。
- 限幅器平均衰减从 **−12.9 dB → −1.27 dB**（同一段粉噪+鼓点；最深 −4.3 dB），
  峰值仍 −1.01 dBFS、0 削波；波峰因数 7.56 → 10.51 dB，鼓点不再把整句压下去。
  A 计权输出 −19.17 → **−14.19 dBFS（响 5.0 dB）**；中高频为主的 `00-source.wav`
  是 −15.06 → **−17.61 dBFS（轻 2.6 dB）**，需要更响可把 `ladder_gain_db_100` 提到 11。
- `lowcut_db` / `highshelf_db` 默认改为 0：旧 v11 的两个搁架是为压制 vendor 低频包的，
  修正阶梯已经接管音色，再叠会变薄。字段保留，可继续做 A/B 或用户偏好。

**回滚/对照**：`ladder_correct: false` 立即回到原始 vendor 曲线（旧的 v11 搁架需同时
把 `lowcut_db`/`highshelf_db` 设回 6/2）。运行时：

```bash
curl -s -X PUT 127.0.0.1:8872/loudness -d '{"ladder_correct":false}'
```

### 部署后核对清单（必须逐条确认）

1. `GET /loudness` 返回：
   - `ladder.corrected: true`
   - `tone.lowcut_db: 0`、`tone.highshelf_db: 0`
   - `mode: "stock"` 时 `curve` 是 `EQ_50…EQ_100` 之一（不是空，也不是 custom/off）；
     100% 音量下应为 `EQ_100`、`ladder.tier: 5`
2. **持久化文件优先于新默认**：adapter 启动时若 `DOT2_LOUDNESS_FILE` 存在，就整份覆盖
   manifest 的 `loudness_defaults`。生产/旧环境里的
   `/var/lib/rui/loudness.json` 或 `/opt/rui-secrets/loudness.json` 若存在且还是
   `lowcut_db=6` / `highshelf_db=2` / 无 `ladder_correct`，必须先同步为
   `lowcut_db=0`、`highshelf_db=0`、`ladder_correct=true`，或直接删除让默认生效；
   本仓库 tracked 的 `dev/rui/loudness.json` 已同步为这一组值。
3. A/B 回滚用 `PUT /loudness {"ladder_correct":false}`；回滚后 `GET /loudness` 的
   `ladder.corrected` 应为 `false`，且若要完全回到 v11 听感需再设
   `lowcut_db=6`、`highshelf_db=2`。

## 参数（`LoudnessConfig`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 总开关（A/B 用） |
| `mode` | `stock` | `stock`（FIR 阶梯）/ `custom`（参数 EQ）/ `off` |
| `curve_ladder` | true | true=跟随音量选曲线；false=固定 `EQ_100` |
| `ladder_correct` | true | 用修正后的六档曲线；false=原始 vendor 曲线（回滚/对照） |
| `ladder_bass_db_50` / `_100` | 9.0 / 6.0 | 100–300 Hz 目标提升（tier EQ_50 / EQ_100） |
| `ladder_presence_db_50` / `_100` | 0.8 / 3.5 | 3–6 kHz 目标提升（tier EQ_50 / EQ_100） |
| `ladder_gain_db_50` / `_100` | 2.0 / 10.0 | 1 kHz 绝对增益（tier EQ_50 / EQ_100） |
| `ladder_taps` | 1024 | 修正曲线 FIR 长度；目标先在 8192 点网格设计再截断，群延迟 10.67 ms |
| `trim_db` | **−2** | EQ 后、限幅前的整体增益（±dB）；2026-10-24 试听定：−2 dB 去掉句尾"刺耳" |
| `limiter_ceiling_db` | −1.0 | 限幅目标峰值 |
| `limiter_lookahead_ms` | 5.0 | 前瞻窗口 |
| `limiter_release_ms` | 80.0 | 释放时间 |
| `lowcut_hz` / `lowcut_db` | 250 / **0** | 可选低架削减（修正阶梯默认已处理低频） |
| `highshelf_hz` / `highshelf_db` | 4000 / **0** | 可选高频收敛（修正阶梯默认已给 presence） |
| `bands` | [] | `custom` 模式：`{type, freq, gain_db, q}` 列表 |

当前默认值 = **2026-10-26 修正阶梯**：六个 tier 各自一条 1024-tap FIR + 限幅 −1 dBFS，
`trim −2 dB`，`lowcut_db=0`、`highshelf_db=0`。改前/改后的完整频响表与复现命令见
`tools/eq_report.py`（`python tools/eq_report.py` / `--json` / `--wav <48k mono S16>`）。

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
| `tools/eq_report.py` | 改前/改后六档频响、100–300 vs 1–3k、限幅器平均衰减（复现本文件的数字） |
| `artifacts/eq-prototype/` | 从设备拉下的 `EQ_*.cfg` + 试听样音（gitignore） |
| `docs/eq-listening-guide.md` | 试听流程与结论 |

## 发布注意

1. `EQ_*.cfg` 是设备 vendor 文件，**不进 git**；部署 bundle 默认不含它们。NAS 部署时用
   `adb` 从设备拉一次放到 `secrets/eq/`，或直接从 Mac 的 `artifacts/eq-prototype/` 拷贝；
2. 缺曲线时 `mode=stock` 会**降级为平直**并告警，不会静音或崩溃；
3. 与 P3（rui 接管 AEC）无冲突：这一层只处理下行播放，不碰采集/参考通道；
4. 如果 rui 未来自己做响度/EQ，把 `enabled=false` 即可让插头侧完全让位。


## 2026-10-24 白天修掉的问题（同一层）

| 问题 | 根因 | 处理 |
|---|---|---|
| 长句/短回复"破音" | 限幅器计算前瞻峰值忘了取 `abs()`，负半周大峰漏检、撞 −0.1 dBFS 硬削波 | 峰值取 `np.abs()`；回归测试（全负峰信号必须被限住） |
| 换流后"呢"后杂音 | FIR overlap-add 尾巴、biquad 延迟线、限幅器前瞻线跨流残留 | 每段播放开始 `reset_stream()` 清零 |
| 回复末尾"嗒"一声 | 信号被处理链延迟 ~16 ms，最后非零样本直接跳静音 | 流结束时对部分帧做 10 ms 淡出再补零 |
| 句中"卡"（~400 ms） | rui 流式 TTS 到达有断档，Pacer 只预存 320 ms | **不在本层**：rui issue #1；本层只负责不额外制造断缝 |

调参接口新增：`GET/PUT /loudness` 的 `trim_db` 当前默认 **−2 dB**（句尾"套"字在 0 dB 时略刺耳）。

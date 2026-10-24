# 当前状态与交接（2026-10-24 10:52 更新）

> 这份文件取代凌晨 03:00 的夜班交接。当前工作：Echo Dot 2（EchoMuse v2.18.1）作为
> rui 的语音卫星，**Mac 本地 dev 实例**在跑；NAS 部署按 M1 清单攒着，一次性上。
> 硬约束：不改 rui 代码、不重刷设备；音量上限按 9 哥最终确认 = **100%**。
> 对外身份：**Dot2 桥开发智能体 / dot2-bridge-agent**（提 issue、写报告时署名）。

## 1. 现在正在跑什么

| 项 | 状态 |
|---|---|
| rui（本机 dev） | `:8091`，`audio_plane=true`、`speaker_connected=true`，配置 `dev/rui/config.yaml` |
| adapter | rui 音频面子进程内，监听 `wss://0.0.0.0:8870`；设备已连（`G090LF0964340RN6`，v2.18.1，<lan-ip>） |
| **音量** | **100% = level 127 = 0 dB（unity）**；控制台显 255/255（回读设备实际值） |
| **麦克风预增益** | **18 dB**（设备默认 24 dB 会削波；实测语音段峰值 0.70–0.88） |
| **设备静音** | **当前静音中**（`DOT2_MUTE=1`，设备 level 0/127）；要出声时去掉该变量重启（见 §5） |
| 响度处理 | 开：stock EQ 阶梯（EQ_50…EQ_100）+ 低架 −6 dB@250 + 高架 −2 dB@4k + 限幅 −1 dBFS + trim −2 dB |
| 设备 AEC | 开（`[aec] enabled: frame=512 tail=64ms`）；rui 侧 AEC 因无参考关闭 |
| 旧 EchoMuse controller | 容器保留、默认停止（接管时会推 85/127） |
| 8 小时 soak | 10:44 起跑，每分钟一行 `dev/rui/soak.jsonl`（M1 第 6 项） |
| 播放 | flush 后加 **pong 屏障**（保 prime 旁路、免跨 socket 竞态）+ **0.45 s 预缓冲**（`DOT2_PREBUFFER_MS`）；实测 `primeWait` 17–22 ms、`underruns=0`、流内 `maxGap` 123–156 ms；回复延迟 +~0.45 s |

## 2. 2026-10-24 白天修掉的真问题

| # | 问题 | 根因 | 提交 |
|---|---|---|---|
| 1 | 声音极小（"30%"） | 音量刻度是 **dB 线性**（0.5 dB/步，127=0 dB），30% = −44.5 dB | 上限改 100% |
| 2 | 改 70% 不生效 | 上限被 `clamp_percent()` 默认 30 二次夹；且目标/回读混用一个变量 | `0b1f711` |
| 3 | 破音（长句/短回复） | 限幅器算峰值忘了取绝对值，负峰撞硬削波 | `1f49920` 前后（见 `docs/findings.md`） |
| 4 | 句中"卡"（400 ms 空档） | rui 流式 TTS 到达有 400 ms 空档，Pacer 只预存 320 ms | rui issue #1（本仓库不改） |
| 5 | "呢"后小杂音 | 换流时 FIR/限幅状态残留；以及末尾非零样本直接跳静音 | 流边界 `reset_stream()` + 10 ms 淡出 `601a9c6` |
| 6 | 短回复被切成两段流 | `EOS_IDLE_S=0.4` 太短，TTS 断供 0.4 s 就换流重新 prime | 改 1.2 s `ddaa375` |
| 7 | 控制台音量显示错 | `volume_255()` 漏传 cap + 用目标值 | `a5b635b` |
| 8 | 麦克风削波（6/7 顶 1.0） | 设备 mic 预增益默认 24 dB | 推 `micGainDb=18` `352c500` |
| 9 | "AEC 关闭 = 打断不可用" | 告警文案误导；AEC 链关但唤醒打断仍触发 | 文档纠错 `f6e9bd4` |

## 3. 已验证的数字（有日志/表格）

- **P2（修复后干净窗口，7 次）**：**7/7 有回答**；说完→出声 **P50 998 ms / P90 1238 ms**；
  wake→出声 P50 5.5 s（含人说话时间，不代表延迟）；barge 4 次、server 侧 0 ms。
- **麦克风**：18 dB 后语音段峰值 0.70–0.88，**0/5 削波**（24 dB 时 6/7 顶 1.0000）。
- **响度**：A 计权 −14.8 dBFS（与试听通过的 `02` 同响度），低频能量 89%→82%，零削波，
  10.6 s 长句 0 削波。
- **回滚演练**：停 rui → 设备试 8867/8770（旧 controller 已停，拒绝）→ 重启后 **~35 s** 重连注册。
- **崩溃恢复**：`kill -9` 音频面 → watchdog 3 s 重建 → 设备 3 s 重连（首轮记录）。
- **部署体检**：`doctor.py` 22 项 0 fail 0 warn。
- 测试：**56 个单测全绿**，ruff 干净。

## 4. 已知但未闭环

1. ~~播放 400 ms 空档~~ → adapter 侧已修（flush + pong 屏障 + 0.45 s 预缓冲），
   见 [rui#1](https://github.com/yes999zc/rui/issues/1)。**rui 已接受 issue，计划 0.2 修**：
   - P2.9：有序 `start(flush, id) + ack` 屏障、`audio.playback.prebuffer_ms` 配置化、Pacer gap 指标；
   - P3：adapter 回报 `played_ms`（需固件支持，v2.18.1 无逐块播放头）。
   等 0.2 契约落地后，Dot2 adapter 去掉预缓冲、恢复低延迟；此前保留兜底。
2. **P3 固件**（2ch 交出 AEC 参考）：已编译未安装（sha256 `876f63b4…`），A/B 槽可回滚，等 9 哥决定。
3. **严格 G1**（20 次唤醒 ≥19）：修复后只有 7 次干净样本，可选补 13 次。
4. **NAS 一次性部署**：等 M1 清单全绿（soak、plug.json、窗口）。
5. **插头 `plug.json`**：契约已写（`docs/plug-socket-contract.md`），实现是盲开阶段下一步。

## 5. 常用操作

```bash
cd ~/AI_Workspace/01_Code/projects/rui-dot2-bridge

# 启动/重启 dev 实例
./scripts/dev_rui.sh

# 静音 / 取消静音（重启生效）
DOT2_MUTE=1 ./scripts/dev_rui.sh          # 静音：音量上限=0，唤醒/ASR/打断照常
./scripts/dev_rui.sh                      # 恢复

# 响度实时调参（rui 后台可代理）
curl -s 127.0.0.1:8872/loudness
curl -s -X PUT 127.0.0.1:8872/loudness -d '{"enabled":false}'   # A/B
curl -s -X PUT 127.0.0.1:8872/loudness -d '{"trim_db":-2}'

# 体检 / 一键检查
python scripts/doctor.py
./scripts/check.sh

# P2 真人测试
python scripts/p2_trials.py --trials 20

# 夜间长稳
python scripts/soak_report.py
```

## 6. 环境变量（dev/rui/env 里有默认值）

| 变量 | 当前值 | 作用 |
|---|---|---|
| `DOT2_VOLUME_CAP` | 100 | 音量上限（% of 设备 0–127 刻度） |
| `DOT2_MIC_GAIN_DB` | 18 | 设备麦克风预增益 |
| `DOT2_EQ_DIR` / `DOT2_LOUDNESS_*` | `artifacts/eq-prototype` / 开 | 响度处理与曲线目录 |
| `DOT2_CONTROL_PORT` | 8872 | 响度调参 HTTP |
| `DOT2_MUTE` | 未设 | 1 = 静音（上限 0） |
| `DOT2_PREBUFFER_MS` | 450 | 播放预缓冲毫秒数（吸收 TTS 分片空档） |
| `DOT2_AEC_ENABLED` | 1 | 0 仅用于声学回采测试 |

## 7. 安全/约束

- **不改 rui 仓库**（`git -C ~/AI_Workspace/01_Code/projects/house-ear status` 应保持干净）；
- **不重刷设备**；P3 固件只在 9 哥同意后装，A/B 槽自动回退；
- 设备音量任何路径都经过 adapter 的上限；
- `secrets/`、`dev/rui/data*`、`artifacts/` 不进 git。

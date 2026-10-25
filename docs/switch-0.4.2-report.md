# rui 0.4.2 受控切换报告（.34，2026-10-25）

> 目的：按维护者在 [rui#7](https://github.com/yes999zc/rui/issues/7) 公布的 0.4.2 与 4 项清单，
> 在测试机 `.34` 做一次「停 0.3.4 → vanilla 0.4.2 冷启动 → 加 adapter 层 → 真机链路 → 回滚演练」的受控切换。
> 9 哥决定：**真人段暂缓**（先保证上游 rui 的质量，效果调试属于我们后续工作），
> 因此本次"真机回合"用上游官方 `POST /api/v1/debug/say`（同一 runner/TTS/播放路径）替代；真人段另行补测。

## 一、时间线（+08）

| 时间 | 事件 |
|---|---|
| 15:44–15:45 | 备份状态（`~/rollback-switch-0.4.2-20261025-154420/`）+ `docker save` 0.3.4 下游镜像；停 0.3.4、清空全部 rui 镜像；构建目录 `~/rui-dot2` 临时移走 |
| 15:45–15:55 | **新用户视角 agent**（fresh context，只读上游 v0.4.2 材料）完成 vanilla 0.4.2 原地升级；卡在"设备不可驱动导致 unhealthy"，如实记录 |
| 15:53–15:56 | Lead 加 adapter 层：装 manifest 1.1.1、构建 `rui-dot2:0.4.2`、恢复下游 compose、切 `.env`、写入 followup 参数 → **healthy** |
| 15:56–15:58 | 非人工回合验证（`/debug/say`）→ 发现撤门控后自听复现 → 恢复 `DOT2_PLAYBACK_GATE_MS=2500` 并复测 |
| 16:02–16:04 | 回滚演练：0.4.2 → 0.3.4（21 s 容器 + 设备重连）→ 切回 0.4.2（11 s 容器 + 设备重连） |

停机窗口合计约 9 分钟（15:44 停 → 15:53 恢复），Dot 2 全程离线重试。

## 二、备份与回滚物料

| 物料 | 位置 |
|---|---|
| 状态备份（.env / compose / config / data / dot2-secrets） | `~/rollback-switch-0.4.2-20261025-154420/rui-state.tar.gz`（186 MB，0600，含 sha256） |
| 回滚镜像（0.3.4 下游） | 同目录 `rui-dot2-0.3.4.tar.gz`（153 MB）+ 本地 `rui-dot2:0.3.4` tag |
| 切换前证据 | 同目录 `before-containers.txt` / `before-health.json` / `before-images.txt` / `before-ports.txt` |
| 0.4.2 现场快照 | 同目录 `drill-042-saved/`（.env、config、compose） |
| 新用户 agent 阶段备份 | `.34:/tmp/rui-coldstart-backup/rui-20261025-154903/`（270 MB） |

旧镜像清理：0.2.2 / 0.3.4(upstream) / 0.4.0 / 0.4.1 / rui-dot2:0.3.4 全部删除；**tag ≠ 镜像** 的坑不再有干扰项。
注：`docker rmi` 对共享层只做 untag，宿主仍留 dangling image（新用户 agent 也观察到，记为 cosmetic K2）。

## 三、vanilla 0.4.2 冷启动（新用户视角）

完整报告见 [deploy-0.4.2-user-report.md](deploy-0.4.2-user-report.md)（由独立 agent 产出，Lead 仅归档）。

- 按 `docs/deploy.md` 升级路径执行；`/health.version=0.4.2`、`config_version=2` 不变、无 crash-loop、8080 正常；
- `dot2-secrets` 20/20、`data/models` 27/27 **逐字节未变**；
- 仍然 **unhealthy**：唯一 enabled 设备的 adapter 不在 vanilla 镜像里，healthcheck 因此失败；
  同时宿主 `data/plugs/dot2-echomuse/plugin.yaml` 还是旧版（`1.1.0 / rui_compat <0.4.0`），被 0.4.2 明确拒绝加载。
- 结论：**vanilla 0.4.2 安装本身成功；"已有 out-of-tree adapter 安装原地升级"缺迁移文档/兼容矩阵**（见 rui 新 issue）。

## 四、加 adapter 层后的最终状态

| 项 | 结果 |
|---|---|
| 镜像 | `rui-dot2:0.4.2`（base `ghcr.io/yes999zc/rui:0.4.2`，digest `sha256:3791ccfd…`） |
| 容器 | `healthy`，RestartCount=0 |
| /health | version 0.4.2、config_version 2、devices=1、enabled=1、device_errors=0、audio_plane=true、speaker_connected=true、`adapter_plugins.registered=["dot2"]` warnings=0 |
| 端口 | 8080 / 8870 / 127.0.0.1:8872 |
| manifest | 重装为 **1.1.1 / `rui_compat ">=0.3.0 <0.5.0"`** |
| `/devices/dot2/status` | **listen_port=8870**、**token_env=DOT2_EM_TOKEN**（0.4.1 的修复生效，4412 冲突消失） |
| 契约自测 | `plug-test` on 0.4.2：**15 PASS / 0 FAIL / 2 WARN / 5 SKIP**，与 0.3.4/0.4.0/0.4.1 基线逐项一致（[报告](contract-report-0.4.2.json)） |
| 非人工回合 | 官方 `POST /api/v1/debug/say`：LLM→TTS→设备喇叭，`playback_gap_count=0`、`playback_max_gap_ms=0` |

## 五、关键发现：撤掉门控后自听复现（上游 2 个 knob 不够）

维护者在 #7 建议：「`followup_delay_ms=2500` + `followup_replay=false`，并撤掉 adapter 的 `DOT2_PLAYBACK_GATE_MS`」。
我们照做后：

1. 长回复（110 字，播放约 8 s）播完，follow-up 窗口按 2.5 s 打开；
2. **9 s 后生成一条垃圾 turn**：ASR 输出「我测试下什的。」→ 回复「现在七点五十六分。」（`device_id=dot2`，`end_of_speech=0.0`）；
3. 恢复 `DOT2_PLAYBACK_GATE_MS=2500`（播放中保持麦克风、播放后 0.35–2.5 s 静音）后，同一长回复 **45 s 内 0 条伪造 turn**。

结论：对 `ref_channels=0`、设备自带 AEC 的 inbound 设备，**上游 followup 两个 knob 不能替代适配器门控**，门控仍需保留。
（自听本质：设备 AEC 后仍有残余，rui 的 follow-up 窗口把残余当用户语音。）

## 六、其他两个反馈点

1. `/devices/dot2/status` 显示 `ready=false`、`errors=["token_missing"]`，但设备实际已连上（speaker_connected=true）。
   原因：status 的 `token_configured` 只查 env 或 rui secrets store 里的 `DOT2_EM_TOKEN`（0.4.1 起用 manifest 的 `effective_token_env`），
   而我们的 X-EM-Token 按部署约定放在 0600 的 `dot2-secrets/device_token` 文件里，adapter 走「env → 文件」回退。
   属诊断口径不一致，不影响功能。候选修法：① 容器启动时从文件注入 `DOT2_EM_TOKEN`；② manifest 放弃 `runtime.token_env: DOT2_EM_TOKEN`、改回上游默认 `RUI_DEVICE_TOKEN`；③ 上游允许 adapter 声明"文件来源"。
2. vanilla 冷启动时，healthcheck 的提示是「`docker compose restart rui` brings it back」，
   但本例根因是"设备 adapter 不在本 build"，restart 不可能解决——建议 healthcheck/doctor 在 `device_errors` 非空时给出更贴切的提示。

## 七、回滚演练（双向）

| 方向 | 操作 | 结果 |
|---|---|---|
| 0.4.2 → 0.3.4 | 停容器 → `docker load` 0.3.4 镜像 → 恢复状态备份的 `.env`/`compose`/`config` → up | **21 s 起来（healthy）**，`/health.version=0.3.4`、设备 ≤30 s 重连、plug 注册 dot2 |
| 0.3.4 → 0.4.2 | 停容器 → 恢复 `drill-042-saved/` 的 `.env`/`compose`/`config` → up | **11 s 起来**，`/health.version=0.4.2`、speaker_connected=true、device_errors=0 |

注意：0.3.4 的 schema 不认 `audio.voice.followup_delay_ms/replay`，回滚必须用切换前的 config 快照，不能直接用 0.4.2 的 config。

## 八、结论

- **0.4.2 + 我们的 adapter 层在 .34 上 healthy、设备在线、契约零回归**；`#7` 清单第 1、2 项通过。
- 第 3 项（撤门控）**实测不成立**，已回退到门控方案并记录在案。
- 第 4 项（真实回合 + 回滚）：回滚演练通过；真人回合按 9 哥要求暂缓，之后补。
- vanilla 冷启动的 5 个卡点单独开 issue 反馈给上游。

## 九、音量偏小的根因与修复（9 哥实测反馈，2026-10-25 16:09）

9 哥反馈"播放声音特别小"。根因不在 0.4.2（该版本无音频运行时改动），而在**音量两层单位 + EQ 阶梯的耦合**：

| 层 | 值 | 换算 |
|---|---|---|
| rui `devices[].volume`（百分数） | **60** | rui 侧 `mysoftvol` 0–255 → 153 |
| adapter `set_volume_255(153)` | 60 % | `percent_to_level(60)` → 设备码率 level **76** |
| 设备 codec 音量律（0.5 dB/step，127=unity） | level 76 | 相对 unity **−25.5 dB** |
| 响度阶梯 `curve_for_level(76)` | `EQ_60` | 语音带增益仅 **+0.3 dB**（`EQ_100` 是 +14.9 dB） |

即：配置里的 60 % 让它同时拿到"设备 −25.5 dB"和"EQ 阶梯不补偿"两重衰减，合计比 `volume: 100` 低约 30–40 dB。

**修复**：把 `devices[].volume` 从 60 改成 **100**。rui 的 `0 = 不管理` 会跳过 reconcile，而 adapter 的连接时 push 是
`DOT2_VOLUME_CAP`（100 % → level 127 = unity）；改成 100 后两边一致、不再有 reconcile-down，播放时选到 `EQ_100`。
实测：`/loudness` 的 `curve` 在播放后为 `EQ_100`，日志不再出现 `volume: reconciled to ...`。

后续若仍偏小/偏大：优先调 `trim_db`（现 −2 dB）或 `PUT http://127.0.0.1:8872/loudness` 的 parametric bands（热生效，不用重启）。
另：`devices[].volume=60 → −25.5 dB` 这个换算坑值得写进我们的部署说明，也可能值得回报上游（百分数映射到指数型 codec 律时不是线性响度）。

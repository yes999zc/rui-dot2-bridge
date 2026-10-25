# rui 0.5.0 干净安装验收记录（.34，2026-10-25 18:06–18:20）

> 状态：**通过**。安装由"只负责安装、零项目背景"的 agent 按上游 v0.5.0 材料完成；operator 配置与 adapter 层由 Lead 接手。
> 同时把 **B 路线（`devices[].audio.voice.post_playback_mute_ms`）** 在 0.5.0 上验证通过，`.34` 已切到原生字段、撤掉 adapter 门控。

## 一、时间线

| 时间 | 事件 |
|---|---|
| 18:03 | 扫描到 v0.5.0：tag `v0.5.0`→`406785e`，GHCR digest `sha256:01a3afe1…`；`docker pull` 后 `RepoDigests` 与公告 digest 一致 |
| 18:04 | `plug-test` on 0.5.0：**15 PASS / 0 FAIL / 2 WARN / 5 SKIP**（`docs/contract-report-0.5.0.json`） |
| 18:05 | 备份并清空 .34：`~/rui-pre-0.5.0-20261025-180528/`（旧 `~/rui` 全量、`rui-dot2:0.4.2` 镜像 tar、before 证据、home 遗留物）；删旧镜像/构建目录 |
| 18:06–18:11 | install-only agent 从零装 vanilla 0.5.0（约 5 分钟，0 blocking） |
| 18:12–18:16 | Lead 接手：恢复密钥/设备证书 → 恢复 operator config → 构建 `rui-dot2:0.5.0` → 装 manifest 两份 → 切下游 compose → 起来 |
| 18:16 | 修复 `dot2-secrets/eq` 目录缺 x 位导致的 `PermissionError`（备份恢复时丢了目录执行位） |
| 18:16–18:20 | 验收 + B 路线 A/B（两次长回复，无伪造 turn） |

## 二、vanilla 阶段（install-only agent）

- 镜像 `ghcr.io/yes999zc/rui:0.5.0`（digest `sha256:01a3afe18faa116f318223b8ce6596825fb61e2cd04789bc7c44d37de6184b15`），官方 compose，容器 `rui`，host 网络、1000:1000、只读根；
- `/health`：`status=ok`、`version=0.5.0`、`config_version=2`；容器 healthy；8080 可访问；
- 0 设备语义：`audio_plane=false`、`audio_devices` 空行、healthcheck 不要求音频面（符合上游设计）；
- `fetch-models` 首次 429，agent 重跑成功（239 MB），`asr runtime chain=['local_asr']`；
- 报告与证据：`/tmp/rui-0.5.0-clean-install/report.md` + `evidence/`。

### 安装侧问题（已单独开 issue）

| issue | 内容 | 严重度 |
|---|---|---|
| [#13](https://github.com/yes999zc/rui/issues/13) | `install.sh` 的 `.env` heredoc 未加引号 → 注释里的反引号被当命令执行（本次 CWD 无 compose 只报噪声；在已有 compose 项目目录执行会误启动）；同时把控制台 token 打到 stdout | 中（非阻断） |
| [#14](https://github.com/yes999zc/rui/issues/14) | `fetch-models` 对 hf-mirror 429 无重试、只 WARNING、安装器 exit 0 → ASR 静默缺失，0 设备时 `/health` 仍绿 | 中（非阻断） |

## 三、Lead 接手做了什么

1. 从备份恢复 `dot2-secrets/`（含 `eq/`，修好目录权限：`700` / `750` / `640`）；
2. `.env`：保留新安装器生成的键，注入旧部署的密钥值 + `DOT2_*`；`RUI_IMAGE=rui-dot2:0.5.0`；
3. `config/config.yaml`：恢复 operator 的 v2 配置（audo 48k、capture 16k/1ch/0ref、AEC 关、followup 2500/false、`devices[].volume=100`、`room=客厅`）；
4. 构建 `rui-dot2:0.5.0`（base ghcr 0.5.0 + plugin 1.1.2 / `rui_compat ">=0.3.0 <0.6.0"`）；
5. `install-plug-manifest.sh ~/rui/data`（data/plugs 一份）；
6. compose：上游 0.5.0 原文件 + 下游改动（`image`、`PYTHONPATH`、`RUI_ADAPTER_PLUGINS`、`DOT2_SECRETS_DIR`、`DOT2_LOUDNESS_FILE`、`./dot2-secrets:/opt/rui-secrets:ro`），去掉 `build:` 段；
7. `docker compose up -d --force-recreate` → 设备 43 s 重连；`/health` devices=1、audio_plane=true、speaker_connected=true。

## 四、验收结果（0.5.0 下游部署）

| 项 | 结果 |
|---|---|
| 版本/健康 | `version=0.5.0`、`config_version=2`、容器 healthy |
| 设备 | `devices=1`、`enabled_devices=1`、`device_errors=0`、`audio_plane=true`、`speaker_connected=true`、`speaker_device=dot2` |
| 端口 | 8080 / **8870** / 127.0.0.1:8872 |
| 插件 | `adapter_plugins.registered=["dot2"]`、warnings=0 |
| 设备状态 | `listen_port=8870`、`token_env=DOT2_EM_TOKEN`（`ready=false/token_missing` 显示 bug 仍在，已在 #7 提） |
| 契约 | `plug-test` 15/0/2/5（`docs/contract-report-0.5.0.json`） |
| 播放 | `/debug/say` 长回复 `playback_gap_count=0`、`max_gap=0` |

## 五、B 路线验证（重点）

配置：`devices[].audio.voice = {followup_delay_ms: 2500, followup_replay: false, post_playback_mute_ms: 2500}`，
**移除 adapter 的 `DOT2_PLAYBACK_GATE_MS`**，`multi_device.enabled=false`（单平面）。

| 观察 | 结果 |
|---|---|
| follow-up gate 打开时机 | **5.0 s**（2500 ms 静音窗口 + 2500 ms delay），与上游"`post_playback_mute_ms ≥ followup_delay_ms` 时跳过 gate"一致 |
| 长回复（108 / 116 字）后伪造 turn | **0 条**（45 s 观察窗口，两次） |
| 对照 | 0.4.2 上同样撤门控会复现一条垃圾 turn（ASR 噪声） |

→ 结论：**`post_playback_mute_ms` 在 0.5.0 单平面路径生效，B 路线成立**；`.34` 采用原生字段、不再需要 adapter 门控。
（adapter 门控代码保留，作为回退方案；`DOT2_PLAYBACK_GATE_MS` 不设置即关闭。）

## 六、回滚

- 回 0.4.2：`~/rui-pre-0.5.0-20261025-180528/rui` 恢复为 `~/rui` + `docker load` `rui-dot2-0.4.2.tar.gz`；
- 0.5.0 配置含 v3 per-device 字段（文件仍写 `version: 2`），回 0.4.x 必须用切换前 config 快照；
- 门控回退：`.env` 加 `DOT2_PLAYBACK_GATE_MS=2500`、去掉 `post_playback_mute_ms`，restart。

## 七、待办

- 真人唤醒/打断/误唤醒基线：按 9 哥安排由新会话统一调度，结果回 #3/#7；
- `ready=false/token_missing` 显示 bug（0.5.0 仍在）已提 #7，等上游修；
- docs PR（C1/C5 下游升级一节 + 音量百分数换算）待提交。

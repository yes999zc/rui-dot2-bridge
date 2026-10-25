# rui 0.5.1 干净安装复测记录（.34，2026-10-25 19:13–19:30）

> 目的：v0.5.1 是 rui 针对我们 #13/#14 的 **安装路径补丁**，维护者要求"发布后按原 clean-install 步骤复测一次"。
> 流程与 0.5.0 相同：清空 `.34` → 零背景 install-only agent 装 vanilla → Lead 填 operator 配置 + adapter 层 → 验收。

## 一、发布信息（已核对）

| 项 | 值 |
|---|---|
| tag | `v0.5.1` → `c0d7048`（release 18:57，标题 "rui 0.5.1 - install-path patch"） |
| 镜像 | `ghcr.io/yes999zc/rui:0.5.1`，digest `sha256:25d67cf88c62935c359571ce1a91ac3d6dbc9cae85baeeb873bc4f109d8930b5`（本地 `RepoDigests` 一致） |
| 契约 | 无运行时/配置/plug 契约变化；`plug-test` on 0.5.1：**15 PASS / 0 FAIL / 2 WARN / 5 SKIP**（`docs/contract-report-0.5.1.json`） |

## 二、安装路径复测（install-only agent，零背景）

### ✅ #13（install.sh 注释里的反引号被当命令执行）

- **已修**：安装日志里没有 `no configuration file provided`、没有 `not found`，没有误跑别的 compose；
  `.env` 注释按字面写入（反引号不再被展开）。
- **console token 不再打印到 stdout/日志**（3 次安装输出 + compose logs + service.log + doctor 全查，absent；
  device token 也没有打印）。`console-token.txt` 生成后 0600 / 33 B，与 config 一致。

### ✅ #14（fetch-models 429 静默 / 安装器 exit 0）

- 本次没有遇到 429，**重试分支未被触发**（无法实测）；
- 模型四件齐全，`rui doctor` 新增的 `models` 行 **PASS**：`ASR model + ASR tokens + VAD + KWS present`；
- 失败时非零退出（`exit 1` remediation）分支这次也没走到——因为下面这个新问题会先让脚本死在更早的位置，已在 #15 里注明。

### ❌ 新 blocking：v0.5.1 文档的 `curl … | sh` 一条命令安装会 exit 2

- 现象：`[5/5] healthy` 之后 `sh: 375: Syntax error: Unterminated quoted string`，退出码 **2**（2/2 复现），
  `console-token.txt` 不生成、收尾提示不打印；服务与模型本身正常；
- 根因：安装器收尾的容器健康探针（`docker compose exec -T …`）**从 stdin 读走了管道里尚未解析的剩余脚本字节**，
  shell 继续解析被截断的内容而死；`sh -n` 静态检查通过，所以现有片段式测试覆盖不到；
- 对照：同一份脚本字节「先落盘再 `sh file </dev/null>`」→ 退出码 0、token 正常落盘（这是运维绕过，不是改代码）；
- 已按 9 哥要求**单独开 issue**：[rui#15](https://github.com/yes999zc/rui/issues/15)，附建议修法（探针加 `</dev/null`、
  文档 one-liner 改落盘、补端到端 piped 测试）。

## 三、Lead 接手（operator 配置 + adapter 层）

1. 从备份恢复 `dot2-secrets/`（含 `eq/`，权限 700/750/640）、operator `config.yaml`、`.env` 密钥；
2. `.env`：`RUI_IMAGE=rui-dot2:0.5.1` + `DOT2_*`（**没有** `DOT2_PLAYBACK_GATE_MS`——播放后静音用 rui 原生字段）；
3. 构建下游镜像 `rui-dot2:0.5.1`（base `ghcr 0.5.1` + plugin 1.1.2 / `rui_compat ">=0.3.0 <0.6.0"`）；
4. 装 manifest 两份；compose = 上游 0.5.1 + 下游层（镜像/`PYTHONPATH`/`RUI_ADAPTER_PLUGINS`/`dot2-secrets` 挂载）；
5. `docker compose up -d --force-recreate` → 设备重连。

## 四、验收（下游 0.5.1）

| 项 | 结果 |
|---|---|
| 版本/健康 | `version=0.5.1`、容器 healthy |
| 设备 | `devices=1`、`device_errors=0`、`audio_plane=true`、`speaker_connected=true`、`adapter_plugins.registered=["dot2"]` |
| 端口 | 8080 / 8870 / 127.0.0.1:8872 |
| 设备状态 | `listen_port=8870`、`token_env=DOT2_EM_TOKEN`（`ready=false/token_missing` 显示 bug 仍在，属 rui#7 已知项） |
| 契约 | `plug-test` 15/0/2/5（`docs/contract-report-0.5.1.json`） |
| 播放 | 长回复 109 字：`playback_gap_count=0`、`max_gap=0`；follow-up gate 5.0 s（=2500 静音 + 2500 delay）；**45 s 内 0 条伪造 turn** |

## 五、回滚

- 状态备份：`~/rui-pre-0.5.1-20261025-191314/`（旧 `~/rui` 全量 + `rui-dot2:0.5.0` 镜像 tar + before 证据）；
- 回 0.5.0：恢复 `~/rui` + `docker load` 该 tar + `docker compose up -d`；
- rui 侧 0.5.0 → 0.5.1 对已有部署**无运行时变化**，不升级也不影响使用。

## 六、结论

- **#13 修复确认、#14 的可见性修复确认**；#14 的两个失败分支（429 重试、失败非零退出）本次无法实测；
- **发现新的安装路径 blocking（#15）**，对"按文档一条命令装"的新用户影响很大，已开 issue 并给修法；
- 我们的下游部署在 0.5.1 上**功能零回归**（契约/播放/设备均与 0.5.0 一致）。

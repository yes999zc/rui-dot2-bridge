# C-001 — dot2 每设备参数契约（v1）

- 日期：2026-10-28
- provider：`rui-dot2-bridge` 1.3.2（plug 源码挂载；**B3a dual-window 1.3.2，tag/release pending 9哥 approval**）
- consumer：`rui` 0.7.9（`rui_compat: ">=0.7.0 <0.9.0"` — B3a 双窗口，同时覆盖 0.7.x / 0.8.x）
- 背景：`docs/backlog-0.8.md` §c C-0。第 2 台起同宿主多 plane，5 项原 env-only 配置必须能按设备取。
- 落点：provider `src/rui_dot2_bridge/adapter.py` + `devices/rs03qr.yaml`；consumer 只把 `devices[].params` 原样传入（`DeviceConfig.params` 已是 `dict[str, Any]`，无需改 rui core）。

## 1. 参数表（在 `devices/rs03qr.yaml` 声明）

| key | 类型 | manifest 默认 | 解析后默认（无 env、无 params） | 语义 |
|---|---|---|---|---|
| `control_port` | int | `0` | `0`（关闭） | 响度控制面 `127.0.0.1:<port>`；每台必须不同或都 0 |
| `loudness_file` | string | `""` | `dev/rui/<device_id>/loudness.json` | 响度学习状态文件（**可写，按设备隔离**） |
| `eq_dir` | string | `""` | `artifacts/eq-prototype` → `secrets/eq`（存在时） | 只读 EQ 曲线目录；**默认共享**，可按台覆盖 |
| `secrets_dir` | string | `""` | `secrets/<device_id>`（目录存在时）否则 `secrets` | TLS 材料 + `device_token` 目录 |
| `oww_threshold` | string | `""` | 空 = 固件默认 0.5 | 设备端唤醒阈值；按台覆盖 |

`eq_dir` 是只读厂商输入，不参与"每设备写状态"，所以默认不按 device_id 拆目录；`loudness_file`
是唯一可写状态，默认必须按设备隔离。这是本契约对"文件类默认值按 device_id 区分"的唯一解释例外，
理由：拆分只读曲线目录会让新设备找不到 `EQ_*.cfg`。

## 2. 解析顺序（高 → 低）

1. `DeviceConfig` 字段（`id` / `host` / `room` / `listen_port` / `token_env` / `secrets_dir` 若未来新增）
2. `dev.params.<key>`
3. 环境变量：`DOT2_CONTROL_PORT` / `DOT2_LOUDNESS_FILE` / `DOT2_EQ_DIR` / `DOT2_SECRETS_DIR` / `DOT2_OWW_THRESHOLD`
4. 第 1 节的解析后默认值

env 只作 fallback：现网单台 override 继续生效，不强制迁移。

## 3. 凭据（secrets / token）

- `secrets_dir` 解析同上；`device_token` 从**解析后目录**读取。
- token 变量名：`dev.token_env` > `params.token_env` > `$DOT2_TOKEN_ENV` > `DOT2_EM_TOKEN`；
  先读该 env，再读 `<secrets_dir>/device_token`。
- 共享 secrets 目录 + 共享 token（受信 LAN）**可接受**，但本契约推荐逐台
  `params.secrets_dir` + 各自 `device_token`；不做强制迁移，不改变单台现网路径。

## 4. 错误与降级

- `control_port` bind 失败（EADDRINUSE 等）：**warning + 关闭该台控制面，plane 继续启动**；
  不允许再因端口抢占 abort `open()`。
- 文件路径父目录不可写：保持现有行为（启动时报错），由配置负责；默认路径在容器内需由
  部署方映射到可写卷（生产仍用 env/params 指到 `/var/lib/rui`）。
- 未声明 key 被 v2 写入时仍是 `unknown_params_ignored`；本契约把 5 个 key 加入 manifest 后消除。

## 5. 兼容与回滚

- 无破坏性接口：`build(dev, cfg)` 签名不变；新增 4 个**可选** ctor kwarg。
- 现网单台：env 优先，路径/端口/阈值与 0.7.9 完全一致。
- 回滚：revert provider 一个 commit 即回到 env-only；consumer 无需回滚（无 core 行为变更）。
- 版本：B3a dual-window 1.3.2，tag/release pending 9哥 approval（窗口 `">=0.7.0 <0.9.0"` 覆盖
  0.7.x / 0.8.x，0.6.x 部署停在 1.3.1）；生产部署需 9 哥单独批准。

## 6. 验收（provider 侧自动测）

1. 两个不同 `device_id`、无 env：loudness 路径不同，control_port 均为 0，`eq_dir` 共享。
2. 5 个 key 的 `params` 均压过 env；env 压过默认值。
3. 同端口第二个 adapter `open()` 不抛异常，控制面降级。
4. `params.secrets_dir` 生效；`token_env` 逐台生效；`device_token` 从各自目录读取。
5. `rs03qr.yaml` 声明 5 个 key；`scripts/check.sh` 全绿。

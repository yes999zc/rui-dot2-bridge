# M1 里程碑：本地磨平 → NAS 一次性部署

> 2026-10-24 9 哥 定的工作方式：**不轻易上 NAS**。NAS 上的 rui 是生产实例（还带小爱
> LX06 那条链路），在上面反复调试既复杂又有风险。所以流程是：
>
> **Mac 本地调通一切 → 攒成 M1 → 一次性上 NAS 验证。**
>
> 本文件定义"M1 完成"的验收清单，以及 NAS 那一次的固定动作。NAS 之前的任何问题都在
> 本地解决，不拿 NAS 当试验场。

## 为什么这样

- NAS 部署 = 重启生产 rui 容器，会短暂影响正在用的小爱音箱 Pro 链路；
- NAS 上调试成本高（日志、挂载、镜像版本、权限），一个参数错误就要再来一轮；
- 本地 Mac 有 adb、有完整日志、可以随时重启，是唯一适合"试错"的地方。

## M1 验收清单（全部在本地 Mac 完成）

| # | 项 | 验收标准 | 证据 |
|---|---|---|---|
| 1 | 唤醒 | 真人 20 次 ≥ 19 次；8h 误唤醒 ≤ 2 | `docs/measurements.md`（`p2_trials.py`） |
| 2 | 打断 | 播放中唤醒可用，静音 P50 ≤ 600 ms；50 轮无自激 | 同上 + 平面日志 `barge:` |
| 3 | 音量/响度 | 最终方案定稿（EQ 复刻 / makeup+limiter / 外接音箱三选一），无削波、无失真 | 离线 EQ 安全评估 + 实听 |
| 4 | 音量回读 | 控制台显示与设备实际一致（修掉 cap 漏传、目标/回读混用） | 单测 + 现场对照 |
| 5 | 插头 manifest | `plug.json` 与运行参数一致；`doctor.py` 插头自检全过 | `doctor` 输出 |
| 6 | 稳定性 | 本地连续 8h：无平面重建、无断连、无内存单调增长 | `soak_report.py` |
| 7 | 部署件 | bundle 里的 `DOT2_VOLUME_CAP`、override、config 模板都等于最终值（**不得残留 30**） | bundle 内容 + `doctor` |
| 8 | 回滚演练 | 杀 adapter → 设备落回旧 controller；恢复 → 回来；< 2 min | 设备日志 + old-controller `last_seen` |
| 9 | 文档 | `handoff-morning.md`、`deploy-nas.md` 与实际一致 | 人工过一遍 |

任何一项不过，**不上 NAS**。

## NAS 那一次的固定动作（一次性、可回滚）

前置：rui 侧镜像已是目标版本（验收日之后统一更新）；选一个 5 分钟无人使用小爱的窗口。

1. 备份：`config/`、`.env`、`data/` 快照（rui 自带 backup 脚本优先）；
2. 上传 bundle（`scripts/make_deploy_bundle.sh` 产物）+ 放 `secrets/`（0600）；
3. 在 rui 的 compose 上叠加 `deploy/rui-dot2-override.yml`（挂 `src/`、`PYTHONPATH`、
   `DOT2_VOLUME_CAP`=最终值、`DOT2_SECRETS_DIR`）；
4. rui 配置加 `adapter: dot2` 设备条目（capture/playback 按 manifest）；
5. `docker compose up -d`，跑 `python scripts/doctor.py --config <nas config> --secrets <nas secrets>`；
6. 设备 `controller.json` 指向 NAS（endpoints[0]）+ 旧 controller（endpoints[1]）；
7. 验收：`/health`、设备日志 `Registered/Volume set/streaming`、一次 TTS 回合、一次真人唤醒；
8. 失败回滚：恢复 compose/config 备份 → 设备 endpoints 指回旧 controller → 容器重启；
   全程目标 < 5 分钟。

## 与插头/插座的关系

- 设备侧开通（证书、`controller.json`、token）仍由插头方负责，NAS 那一次只做一次；
- 容器侧接入（compose/config）属于插座方动作，长期应变成 rui 的"接入插头"能力；
  在插座规范落地前，按上面第 3/4 步手工执行；
- M1 之后如果插座规范就绪，Nas 部署应改为"rsync bundle + 调接入 API"，而不是改 compose。

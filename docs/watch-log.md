# 上游守望日志

> 每次扫描一行；有变化时在下方写详情。状态文件：`.watch/rui-state.json`。

| 时间（+08） | 结论 |
|---|---|
| 2026-10-25 10:56 | 建立。基线：rui 最新 tag **v0.4.0**（02:01Z），ghcr 最新镜像仍 **0.3.4**——0.4.0 的 docker-publish 失败，镜像未发布；#3/#7 各 3 条评论（最后一条是我方 10:31 的回报）；两个仓库均无他人 PR/issue |
| 2026-10-25 11:03 | **v0.4.0 镜像已发布**：ghcr 新增 `0.4`/`0.4.0`/`sha-9f13a0f`（digest `sha256:c4cd1a61…`，03:01:39Z），匿名可拉；#3/#7 仍 3 条评论、无新回复；我们仓库出现 #1 issue + #1 PR（compat 门禁，本队另一 session 创建，标注勿自行合并）；**0.4.2 未 tag，冷启动触发条件未满足** |
| 2026-10-25 11:07 | **Lead 决策**：不用 0.4.0 起冷启动（9 哥要求等 0.4.2）；0.4.0 已发布（digest `sha256:c4cd1a61…`），契约复核：main 区间被拒 / PR#1 `<0.5.0` 通过；.34 保持 0.3.4。**rui 维护者 03:04Z 在 #3/#7 回复**（详见下） |
| 2026-10-25 11:10 | **PR#1 已合入** `dd314cd`（rui_compat `>=0.3.0 <0.5.0`，无 adapter 改动）；合入后 plug-test：rui **0.3.4 与 0.4.0 均 15 PASS / 0 FAIL / 2 WARN / 5 SKIP，逐项一致**（报告 docs/contract-report-0.4.0.json）；已在 rui #3/#7 回报；**0.4.1** 将提供 `audio.voice.followup_delay_ms/replay` 与 `status.listen_port` 修复，届时用上游配置替换 adapter 的 `DOT2_PLAYBACK_GATE_MS`；**0.4.2 tag+digest 仍未出现，冷启动待触发** |
| 2026-10-25 12:00 | **v0.4.1 已发布且可拉**：tag `v0.4.1`（`de7b17e`），ghcr `0.4.1`=`0.4` digest `sha256:380327e5…`（amd64+arm64，匿名 GET 200）；`latest` 随 main（`21761804…`）；Release 03:31:25Z；#7 新评论（03:32Z，第 6 条）要求合入 PR#1 后用 0.4.1 跑 plug-test + 真机回合/回滚，并列出 3 项相关修复（followup 可配置、生效端口/token_env、register 后 device_wake 重探测）；**0.4.2 未出现**；按 Lead 指令不部署 .34，仅信息与验证 |
| 2026-10-25 12:02 | Lead 已回 **#7**（0.4.1 ack：plug-test 报告、真机回合/回滚并入 0.4.2 冷启动、followup 配置与撤门控计划、token rotation 边界）；**真机验证只能受控切换**——Dot `controller.json` 只拨 8870、同刻仅能一个进程监听 8870，"独立临时实例"不可行，须 停 0.3.4 → 起 0.4.1 → 验 → 回滚（需 9 哥同意）；继续盯 0.4.2 tag+可拉 digest；`latest` 随 main、0.4.x 固定映射 `0.4` |

### 2026-10-25 11:03 — v0.4.0 docker-publish 成功，镜像可用（0.4.2 仍未出现）

- rui main HEAD `9f13a0f`（"docs: add changelog, 0.4.0 release notes and release tooling"，02:57:43Z）；tag **v0.4.0 已指向 9f13a0f**；02:58 对 main + tag 重新触发 workflows；tag 的 docker-publish **success**（02:59Z）。
- ghcr `yes999zc/rui` 新增 tags `0.4`, `0.4.0`, `sha-9f13a0f`（并更新 `latest`）；manifest index digest `sha256:c4cd1a61bb476764a73629ecd64ca315eae67e5012cbdd3464304d98023a4974`；匿名 `GET /v2/yes999zc/rui/manifests/0.4.0` 返回 200，含 linux/amd64 + linux/arm64。
- GitHub Release **v0.4.0 已发布**（03:03:31Z，tag `v0.4.0` → commit `9f13a0f`，无 release 二进制资产，镜像仅在 ghcr）；release notes 声明 audio plane 与 `/api/v1/*` 未变、config schema 仍 v2、0.3.2+ 可平滑升级——此前 compat PR 里"等官方 tag/digest + Release 确认"的前提现已满足。
- 仍无 `v0.4.2` tag/镜像 → `docs/cold-start-0.4.2.md` 的触发条件（0.4.2 tag 存在且 digest 可拉）**未满足**；0.4.0 是否部署由 Lead 决定。
- issue #3 / #7：comments 仍为 3 条，最后一条仍是 02:31Z 我方回报，rui 无新回复；#1 最后评论 2026-10-24T06:37Z（我方）。
- 我们仓库：新增 #1 issue + #1 PR `compat: accept rui 0.4.x (rui_compat <0.5.0)`（作者 yes999zc，03:01Z，属本队另一 session 的 compat 工作，PR 明示"请勿自行合并"）；仍无他人 PR/issue。
- rui PR：#8/#9/#10 均已 merged（旧 CI 修复），无新 PR。
- 处置：无 issue 回复（无需回），未改任何代码，仅本文件变更。

### 2026-10-25 11:07 — rui 维护者回复 #3/#7（03:04Z）+ 0.4.0 契约复核

- **#3 回复要点**：① 设备端 OWW 资源表达不新增字段，`device_side.install_steps` + `compatibility.notes` 即推荐做法（未来机器可读字段走 minor 并提前一个 minor 通知）；② `wake.barge_via_device: true` 但 probe 无 OWW → probe 不宣告、RUI 降级 KWS 正是期望行为，保持 manifest intent（不要改成 false，否则 OWW 就位后要再改一次）；③ 0.4.x 唯一阻塞是 `plugin.yaml` 的 `rui_compat` 上界，PR 已开在我们仓库，合入后请用 0.4.0 重跑 `plug-test`；④ 冷启动 probe 早于 register 已记录，随后续设备唤醒工作修（KWS 不受影响）。
- **#7 回复要点**：① 0.4.0 官方产物就绪：Release v0.4.0、ghcr `0.4.0`=`0.4`=`latest`=`sha256:c4cd1a61…`（amd64+arm64）、上游 CI 全绿（pytest 1104 passed / 17 skipped）；② 0.4.0 未改插头/设备契约，合入 PR 后请在 0.4.0 上跑 plug-test + 真机回合 + 回滚演练；③ **接受我方两条建议进 0.4.1**：`audio.voice.followup_delay_ms`（默认 1000，0–10000）与 `audio.voice.followup_replay`（默认 true）、`status.listen_port` 生效值 bug；发布前继续用 `DOT2_PLAYBACK_GATE_MS` 绕过；④ 多设备/仲裁未交付，交付前仍单音频面单设备；⑤ 0.4.1 发布会再 ping。
- **更正**：我们仓库只有 **PR #1**（GitHub 编号共享；扫描脚本 own-issue 查询走 `/issues` 端点会把 PR 也列出，故 11:03 行"#1 issue + #1 PR"实为同一 PR）。该 PR 由 **rui 维护者** 开在我们仓库（PR 明示"请勿自行合并，由 repo owner 合并"）——11:03 详情里"属本队另一 session"作废。
- 另：rui main 于 03:04:33Z 新增 docs-only 提交 `4fc4e96`（"docs: record the 0.4.0 re-cut, official image/release and green baseline"），再次触发 ci/docker-publish（03:06:38Z 仍 in_progress）。
- 处置：**未在 rui 回复**（PR 合入决定属 Lead/9 哥，plug-test 回报须在合入后进行）；未改代码，仅本文件变更。

### 2026-10-25 12:00 — v0.4.1 发布（0.4.1 = 0.4，latest 随 main）；#7 第 6 条评论

- **产物**：tag `v0.4.1` → `de7b17e`；GitHub Release v0.4.1（03:31:25Z，非 draft）；ghcr `ghcr.io/yes999zc/rui:0.4.1` = `0.4`，OCI index digest `sha256:380327e5ca0796ac8656b6c195ce5de2258785a4eaf7cf7cee45e56191312668`，manifest 含 linux/amd64 + linux/arm64（另 2 个 unknown/unknown 为 attestation），匿名 GET manifest 返回 200 → **可拉**。`latest` 于 03:36:44Z 指向 main `sha-8f0317d`（digest `21761804…`），按上游说明 latest 随 main、digest 会变，**不要拿 latest 当 0.4.1 固定版本**。
- **#7 评论要点（03:32:01Z，第 6 条）**：① 0.4.1 已发布，要求先合入 PR#1（已合 `dd314cd`），然后用 0.4.1 跑 `plug-test` 与一次真机回合 + 回滚；② **follow-up 可配置**：`audio.voice.followup_delay_ms`（默认 1000，0–10000）、`audio.voice.followup_replay`（默认 true），设备侧 AEC 插头建议 2500 / false，可撤掉 adapter 里的 `DOT2_PLAYBACK_GATE_MS` 门控，默认值不变、LX06 不回归；③ **生效端口/token_env**：`/devices`、`/{id}`、`/{id}/status`、`/{id}/connect` 与 setup 的 connect 证据显示 8870 / `DOT2_EM_TOKEN`，token rotation 也写 `DOT2_EM_TOKEN`，dot2 与 LX06 不再误判 4412 冲突；④ **register 后 device_wake 重探测**：probe 之后后台重试（12 次 × 5 s），设备拨入证明能力后自动切 v1 路径，预算用尽只记一次日志、KWS 不受影响；⑤ 已知边界：token rotation 只轮换 rui transport secret，不轮换设备内层 `X-EM-Token`，需要的话另立 adapter 契约。
- **0.4.2**：tag/镜像均未出现 → 冷启动触发条件未满足。
- **处置**：未部署、未重启 .34（保持 0.3.4），0.4.1 只做信息与非部署验证；是否在 0.4.1 上跑 plug-test / 真机回合由 Lead 定（建议非部署 plug-test 先跑，真机回合+回滚并入 0.4.2 冷启动窗口）。

### 2026-10-25 12:02 — Lead 已回 #7（0.4.1 ack）；真机验证只能受控切换

- **#7 处置**：由 Lead 直接回帖（2026-10-25T04:02:12Z，第 7 条）：0.4.1 确认 + plug-test 报告 + 真机回合/回滚并入 0.4.2 冷启动窗口 + followup 配置与撤 adapter 门控计划 + token rotation 边界；rui-watch 不再回帖，只做记录与守望。
- **约束（记入以免重复讨论）**：不在 .34 另起"独立临时实例"做 0.4.1 验证——Dot 的 `controller.json` 只拨 8870，同一时刻只能有一个进程监听 8870，因此真机验证本质是"停 0.3.4 → 起 0.4.1 → 验 → 回滚"的一次受控切换，**须 9 哥同意后才执行**；未经同意不动 .34。
- **监控口径**：`latest` 随 main 走、digest 会变；0.4.x 固定映射 `0.4` 与 release 版本；冷启动仍以 **tag `0.4.2` + 可拉 digest** 为唯一触发条件。
| 2026-10-25 15:00 | **巡检停止**（9 哥指示"停止扫描"）：已删除 cron `schedule-f95e2991`、rui-watch 收工、task-5 完成。最后有效扫描为 12:00（v0.4.1 可用、0.4.2 未出现）。之后如需上游状态，手动跑 `python3 scripts/watch_upstream.py` 即可（脚本与 runbook 保留） |
| 2026-10-25 15:20 | 新建 [rui#11](https://github.com/yes999zc/rui/issues/11)：**多设备/多卫星仲裁契约提案**（进程模型、来源分层不跨来源比 score、同房间交叉回声抑制、barge 归属正在播放设备、仲裁 trace/metrics 字段、Q1–Q7 待 rui 决定）。短期我们接受"一设备一实例"，不催进度 |
| 2026-10-25 15:27 | 扫描确认 **v0.4.2 已发布且可拉**：tag `v0.4.2`（`3c4af3e`），ghcr `0.4.2`=`0.4`，digest `sha256:3791ccfd…`（amd64+arm64）；#7 两条新评论（07:27Z 公布 digest + 4 项检查清单；07:42Z 多设备仲裁冻结补充），#11 已答复 Q1–Q7（0.5 单实例多 plane，不需要 adapter 改字段） |
| 2026-10-25 16:05 | **0.4.2 受控切换完成**（.34）：清空旧镜像 → 新用户视角 agent 装 vanilla 0.4.2 → Lead 加 adapter 层 → `rui-dot2:0.4.2` healthy；`plug-test` 15/0/2/5 与基线一致；`/devices/dot2/status` 显示 8870 / `DOT2_EM_TOKEN`；**撤门控后自听复现**，已恢复 `DOT2_PLAYBACK_GATE_MS=2500`；回滚演练 0.4.2↔0.3.4 双向通过。真人段按 9 哥要求暂缓 |
| 2026-10-25 16:16 | rui 回复 #7 / #12：① 门控走 **B 路线**（上游新增 `devices[].audio.voice.post_playback_mute_ms`，默认 0，落地前继续保留 adapter 门控）；② token 显示 bug 进打磨批（短期按"status 只认 env/secrets store"写文档）；③ 音量百分数换算欢迎 docs PR；④ 冷启动反馈 C2/C3/C4/K1 与 C1/C5 文档项接受（欢迎 docs PR），干净主机冷装他们暂不排 |
| 2026-10-25 16:21 | rui #11 Phase 2 草案 v0.1：**对 adapter/manifest 无接口改动**（六字段不变、多平面在 rui 内部、`rui_compat` 待 0.5 发布时更新兼容矩阵）；部署侧仅 `devices[].room`（多设备启用时必填）与 `post_playback_mute_ms` 两个字段 |
| 2026-10-25 16:40 | 9 哥定：测试暂停（新会话统一调度，另有 1×LX06 + 4–5×Dot2 待部署）；**盯 rui v0.5.0 对外发布版**，每 30 min 扫描；发布后在 .34 做完全干净安装（安装专用 agent → Lead 填配置），安装问题单独开 issue。设备端唤醒路线 B 的资产/打分器已真机验证、adapter 改动已提交，`.34` 已恢复到稳定 `rui-dot2:0.4.2` |

### 2026-10-25 15:27 — v0.4.2 发布（tag + digest + 4 项清单），多设备仲裁冻结

- **产物**：tag `v0.4.2` → `3c4af3e`；ghcr `0.4.2`=`0.4`，OCI index digest
  `sha256:3791ccfdbd4429bba76eb57776d2b9047538ff3a8766f725e4c2ee19f1d4096b`（amd64+arm64，匿名可拉）。
- **#7 评论（07:27:59Z）**：9 哥已同意 .34 受控切换；0.4.2 仅控制台/文档收尾（Phase 4：单一 nav manifest、
  11 面板、旧 panel/路由退役、README 瘦身、多设备设计文档），**无 API/config schema/音频/设备运行时改动**；
  要求记录 4 项：① /health 字段；② `/devices/dot2/status` 8870 + DOT2_EM_TOKEN；③ followup 2500/false 并撤门控；
  ④ 一次真实回合 + 回滚。
- **#11 评论（07:42:09Z）**：Q1–Q7 全部答复；0.5 直接做**单实例多 plane**（多实例只是过渡部署手法）；
  来源分层可配置、400 ms 候选合并窗口、同房间交叉回声由 RUI 抑制（room 必填）、barge 只归正在播放设备、
  trace 字段采纳我方命名、`max_planes: 4`；**不要求 adapter 为仲裁加字段**。
- **#4 已关闭**：控制台 token 不可见的问题 0.4.1 修复（我们冷启动报告里的 K4 与它相关）。
- **处置**：0.4.2 可用 → 触发冷启动；9 哥同意原地切换。

### 2026-10-25 16:05 — 0.4.2 受控切换 + 回滚演练完成（详见 switch-0.4.2-report.md）

- 备份/清镜像 → 新用户视角 agent 装 vanilla 0.4.2（版本对、数据逐字节未变，但设备 adapter 缺失 → 必然 unhealthy）→
  Lead 加 adapter 层（manifest 1.1.1、`rui-dot2:0.4.2`）→ healthy，`plug-test` 15/0/2/5，status 8870/DOT2_EM_TOKEN。
- **反直觉发现**：按维护者建议撤 `DOT2_PLAYBACK_GATE_MS` 只留 `followup_delay_ms=2500` + `followup_replay=false`，
  自听复现（一条垃圾 turn）；恢复门控后 45 s 内 0 条伪造 turn → 门控保持，向 #7 反馈。
- 回滚演练：0.4.2→0.3.4 21 s、0.3.4→0.4.2 11 s，设备均自动重连。
- 真人段按 9 哥要求暂缓；非人工回合用官方 `/api/v1/debug/say` 验证播放链路。
- 待办：向 rui 开 cold-start 反馈 issue + #7 回帖；补真人段；OVW 决策；token 显示口径。

### 2026-10-25 16:16–16:21 — rui 对 0.4.2 反馈的结论 + Phase 2 草案 v0.1

- **#7**：接受"撤门控不成立"的结论，选 **B 路线**：新增 `devices[].audio.voice.post_playback_mute_ms`（每设备覆盖，默认 0；
  输出 owner 停播后 N ms 内丢弃麦克风、播放中 barge 不受影响；`≥ followup_delay_ms` 时跳过 follow-up gate；
  落地在 Phase 2 多平面，0.5 之前不要求我们改）→ **在字段可用前继续保留 adapter 门控**（我们现状）。
  token 口径短期选③（文档写清 status 只认 env / rui secrets store），并修复"已连接却 ready=false"的显示 bug（进打磨批）；
  音量百分数→指数型 codec 律的换算欢迎 docs PR。
- **#12**：接受 C2（install.sh 打印已有 RUI_IMAGE）、C3/C4（healthcheck 根因/doctor 一致性）、K1（release notes 占位注释）；
  接受 C1/C5 文档项（`docs/deploy.md` 新增 out-of-tree adapter 升级一节 + 兼容矩阵）；**docs PR 欢迎**（只动 docs）。
  他们暂不排干净主机冷装（做完仲裁后自己做发布前验收）。
- **#11 Phase 2 草案 v0.1**：`drain_wakes()`/`DeviceWake` 六字段不变、多平面在 rui 内部、**不需改 adapter**；
  部署侧两条：`multi_device.enabled=true` 时 `devices[].room` 必填；新增 `post_playback_mute_ms`。
  trace/metrics 会加仲裁字段（可选用于验收）。
- **我们的动作**：a) `.34` 恢复稳定 0.4.2、设备端 OWW 关闭；b) 未来 0.5.0 出现后做干净安装（docs/clean-install-0.5.0.md）；
  c) 0.5.0 前对齐 `rui_compat` 上界（当前 `<0.5.0` 会拒 0.5.0）；d) docs PR（C1/C5 + 音量换算）待 0.5 前提交。

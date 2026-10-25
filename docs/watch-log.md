# 上游守望日志

> 每次扫描一行；有变化时在下方写详情。状态文件：`.watch/rui-state.json`。

| 时间（+08） | 结论 |
|---|---|
| 2026-10-25 10:56 | 建立。基线：rui 最新 tag **v0.4.0**（02:01Z），ghcr 最新镜像仍 **0.3.4**——0.4.0 的 docker-publish 失败，镜像未发布；#3/#7 各 3 条评论（最后一条是我方 10:31 的回报）；两个仓库均无他人 PR/issue |
| 2026-10-25 11:03 | **v0.4.0 镜像已发布**：ghcr 新增 `0.4`/`0.4.0`/`sha-9f13a0f`（digest `sha256:c4cd1a61…`，03:01:39Z），匿名可拉；#3/#7 仍 3 条评论、无新回复；我们仓库出现 #1 issue + #1 PR（compat 门禁，本队另一 session 创建，标注勿自行合并）；**0.4.2 未 tag，冷启动触发条件未满足** |
| 2026-10-25 11:07 | **Lead 决策**：不用 0.4.0 起冷启动（9 哥要求等 0.4.2）；0.4.0 已发布（digest `sha256:c4cd1a61…`），契约复核：main 区间被拒 / PR#1 `<0.5.0` 通过；.34 保持 0.3.4。**rui 维护者 03:04Z 在 #3/#7 回复**（详见下） |
| 2026-10-25 11:10 | **PR#1 已合入** `dd314cd`（rui_compat `>=0.3.0 <0.5.0`，无 adapter 改动）；合入后 plug-test：rui **0.3.4 与 0.4.0 均 15 PASS / 0 FAIL / 2 WARN / 5 SKIP，逐项一致**（报告 docs/contract-report-0.4.0.json）；已在 rui #3/#7 回报；**0.4.1** 将提供 `audio.voice.followup_delay_ms/replay` 与 `status.listen_port` 修复，届时用上游配置替换 adapter 的 `DOT2_PLAYBACK_GATE_MS`；**0.4.2 tag+digest 仍未出现，冷启动待触发** |

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

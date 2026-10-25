# 上游守望日志

> 每次扫描一行；有变化时在下方写详情。状态文件：`.watch/rui-state.json`。

| 时间（+08） | 结论 |
|---|---|
| 2026-10-25 10:56 | 建立。基线：rui 最新 tag **v0.4.0**（02:01Z），ghcr 最新镜像仍 **0.3.4**——0.4.0 的 docker-publish 失败，镜像未发布；#3/#7 各 3 条评论（最后一条是我方 10:31 的回报）；两个仓库均无他人 PR/issue |
| 2026-10-25 11:03 | **v0.4.0 镜像已发布**：ghcr 新增 `0.4`/`0.4.0`/`sha-9f13a0f`（digest `sha256:c4cd1a61…`，03:01:39Z），匿名可拉；#3/#7 仍 3 条评论、无新回复；我们仓库出现 #1 issue + #1 PR（compat 门禁，本队另一 session 创建，标注勿自行合并）；**0.4.2 未 tag，冷启动触发条件未满足** |

### 2026-10-25 11:03 — v0.4.0 docker-publish 成功，镜像可用（0.4.2 仍未出现）

- rui main HEAD `9f13a0f`（"docs: add changelog, 0.4.0 release notes and release tooling"，02:57:43Z）；tag **v0.4.0 已指向 9f13a0f**；02:58 对 main + tag 重新触发 workflows：main 的 ci 一度失败、随后成功；tag 的 docker-publish **success**（02:59Z）。
- ghcr `yes999zc/rui` 新增 tags `0.4`, `0.4.0`, `sha-9f13a0f`（并更新 `latest`）；manifest index digest `sha256:c4cd1a61bb476764a73629ecd64ca315eae67e5012cbdd3464304d98023a4974`；匿名 `GET /v2/yes999zc/rui/manifests/0.4.0` 返回 200，含 linux/amd64 + linux/arm64。
- GitHub Release **v0.4.0 已发布**（03:03:31Z，tag `v0.4.0` → commit `9f13a0f`，无 release 二进制资产，镜像仅在 ghcr）；release notes 声明 audio plane 与 `/api/v1/*` 未变、config schema 仍 v2、0.3.2+ 可平滑升级——此前 compat PR 里"等官方 tag/digest + Release 确认"的前提现已满足。
- 仍无 `v0.4.2` tag/镜像 → `docs/cold-start-0.4.2.md` 的触发条件（0.4.2 tag 存在且 digest 可拉）**未满足**；0.4.0 是否部署由 Lead 决定。
- issue #3 / #7：comments 仍为 3 条，最后一条仍是 02:31Z 我方回报，rui 无新回复；#1 最后评论 2026-10-24T06:37Z（我方）。
- 我们仓库：新增 #1 issue + #1 PR `compat: accept rui 0.4.x (rui_compat <0.5.0)`（作者 yes999zc，03:01Z，属本队另一 session 的 compat 工作，PR 明示"请勿自行合并"）；仍无他人 PR/issue。
- rui PR：#8/#9/#10 均已 merged（旧 CI 修复），无新 PR。
- 处置：无 issue 回复（无需回），未改任何代码，仅本文件变更。

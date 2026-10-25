# 上游守望（rui / 我们自己的仓库）运行手册

> 2026-10-25 建立。目的：rui 开发者继续迭代（目标 0.4.2），我们不再靠人工轮询；
> 有变化由智能体 + 定时任务发现、判断、必要时在 issue/PR 上直接沟通。

## 组成

| 部件 | 作用 |
|---|---|
| `scripts/watch_upstream.py` | 确定性扫描：tags、ghcr 镜像、CI/publish 状态、issue 评论、双方 PR/issue；与 `.watch/rui-state.json` 比对，只输出变化 |
| 定时任务 | 每 3 小时（Asia/Shanghai）触发一次巡检，把结果交给 `rui-watch` 智能体 |
| 智能体 `rui-watch` | 读 digest → 决定回答/记录/升级；负责在 rui issue/PR 上与我方沟通 |
| `docs/watch-log.md` | 每次扫描一行；有变化时写详情 |
| `.watch/rui-state.json` | 上次快照（gitignored，本机持久） |

手动执行：

    cd 01_Code/projects/rui-dot2-bridge
    python3 scripts/watch_upstream.py              # 只看变化
    python3 scripts/watch_upstream.py --update-state
    python3 scripts/watch_upstream.py --json

## 扫描范围

| 类别 | 来源 | 关心什么 |
|---|---|---|
| 版本 | `repos/yes999zc/rui/tags` | 新 tag（尤其 **v0.4.2**） |
| 镜像 | ghcr `yes999zc/rui` 的 container tags | 新镜像（tag 有但镜像缺失 = publish 失败，要报） |
| CI/发布 | `repos/yes999zc/rui/actions/runs` | docker-publish / ci 的 conclusion |
| 契约讨论 | rui issue **#3**（device_wake）、**#7**（接入契约）、**#1**（播放空档） | 新评论、状态变化 |
| 提及 | rui 仓库中提及 dot2 的 issue/PR | 新的相关讨论 |
| 我们的仓库 | `yes999zc/rui-dot2-bridge` 的 issues / PRs | **有人给我们提 PR/issue**（优先级最高） |
| rui 的 PR | `repos/yes999zc/rui/pulls` | 与我们相关的 PR（例如插头文档/兼容修正） |

## 判定与动作

| 扫描结果 | 动作 |
|---|---|
| 无变化 | `docs/watch-log.md` 记一行（时间 + 无变化），结束。不要为"没变化"写长文 |
| 新 tag / 新镜像 | 核对 digest（`docker manifest inspect` 或 ghcr API），报告 Lead；**只有可用的 0.4.2 镜像才触发冷启动测试**；tag 有但镜像缺 → 报 Lead 并指出 publish 失败 |
| issue 新评论 | 读完整串（含之前的契约更正），判断是提问/结论/变更；能直接答的在 issue 回复（规则见下），否则升级 Lead |
| 我们仓库出现 PR/issue | 立即报告 Lead（这是"有人在跟我们交互"的最高信号），做初步分类：文档修正/代码/提问 |
| rui 出现相关 PR | 读 diff，判断是否影响我们的插头（manifest/contract/entrypoint），报告 Lead |
| publish/CI 连续失败 | 在相关 issue 里礼貌指出失败的工作流与 run 链接（不催进度） |

## 在 rui 上沟通的规则（必须遵守）

1. 用中文、技术化、可执行；每个结论给出**证据**（文件/章节/版本/日志），不空泛。
2. 引用冻结契约（`docs/socket-plug-v1.md` 的章节号）而不是"我们觉得"。
3. **不承诺日期**、不催进度、不要求特殊分支；我们按契约实现。
4. 不泄露任何 secret、token、设备地址、内网 IP、证书；日志摘录先脱敏。
5. 涉及我方方案变更/新增承诺（例如改端口、改 manifest 语义）→ 先报 Lead，不自行拍板。
6. 一次评论解决一件事；回复前先读完整串，避免重复已答内容。
7. 不在 rui 仓库提"给 Dot2 开特例"的请求——只按 v1 契约讨论。

## 边界

- 不改 rui 源码、不 fork 核心；
- 不碰 `<lan-ip>`（生产 NAS）；
- **不自行升级/重启测试部署**（`<lan-ip>` 的 rui 容器）——升级由 Lead 决定并安排冷启动流程；
- 不在我们仓库里提交未经测试的代码。

## 冷启动触发（0.4.2）

当 `python3 scripts/watch_upstream.py` 报告 **0.4.2 镜像可用**（ghcr tag 存在且 digest 可拉）：

1. rui-watch 立即报告 Lead，附 tag/digest/发布日期；
2. Lead 决定冷启动窗口，按 `docs/next-deployment-plan.md` 的方式让"纯用户视角"智能体照上游文档部署；
3. 部署后跑 `docs/p4-e2e-runbook-0.3.4.md` 的基线 + 真人段；
4. 回归通过才更新测试实例；生产（.168）仍单独决策。

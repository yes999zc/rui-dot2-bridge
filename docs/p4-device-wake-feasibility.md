# 设备侧唤醒（device_wake）可行性备忘

> 2026-10-25 ｜ 依据：rui v0.3.4 \`docs/socket-plug-v1.md\` §4.2/§5 + EchoMuse（wilbowes/EchoMuse）\`docs/listening.md\`、\`docs/device-controller-interface.md\`、\`controller/em_oww_sync.py\`、\`controller/em_oww_assets.py\`、\`oww_forge/README.md\`。

## 一、结论先说

**v1 契约侧已经就绪，设备侧唤醒还不能直接生效。**

- rui 0.3.4 接受 \`device_wake\` 为一等事件（\`drain_wakes()\` + \`DeviceWake\` 六字段 + RUI 策略层），我们已按此实现。
- 但 EchoMuse **固件里不含** 设备端唤醒（OWW）的运行时与模型：\`em_oww_assets.py\` 明确写着
  "The ONNX Runtime and the models are deliberately NOT in the firmware"。
- 设备能否本地打分取决于两件事：① 运行时 + 模型文件被推送进设备的
  \`/app/models/oww_runtime\`（\`libonnxruntime.so\` 12.3 MB + \`melspectrogram.onnx\` + \`embedding_model.onnx\` +
  \`<stem>.onnx\` 分类器 + \`silero_vad.onnx\`）；② 控制器用 \`config\` 消息下发 \`owwModel\` 并开启 \`owwOnDevice=on\`。
- 官方 stock 模型只有 **英文**：\`hey_jarvis_v0.1\`、\`alexa_v0.1\`。中文"你好小瑞"没有现成模型，
  必须用 EchoMuse 的 \`oww_forge\`（openWakeWord 官方训练流程，piper 合成正样本 + ~2000 h 负样本）自己训一个 ONNX。

## 二、设备侧状态机（决定我们上报什么）

| \`listen_state\` | 含义 | 是否连续推流 |
|---|---|---|
| \`local\` | 设备自己检测唤醒词 | 否（唤醒后才推） |
| \`stream\` | 控制器（rui）的 KWS 检测 | 是 |
| \`degraded\` | 被要求本地唤醒，但**跑不起来**（运行时/模型缺失或加载失败） | 否，只剩按键 |
| \`legacy\` | 固件不支持 \`oww_local_only\` | 是 |

\`oww_local_only\` 出现在 register capabilities 只代表"固件具备能力"，\`listen_state\` 才代表"现在真的能打分"。
\`degraded\` **不会**偷偷退回推流（否则等于偷偷破坏隐私承诺）。

\`oww_wake\` 事件字段：\`score\`、\`threshold\`、\`ageMs\`、\`capturedMono\`；跨帧时还有 \`level\`、\`peak\`；
私有聆听会话下额外带 \`session\`、\`floor\`、\`barge\`。映射到 rui v1 的六字段时，
\`session/floor/ageMs/capturedMono/peak\` 必须丢弃，\`ts_ms\` 用单调时钟（可用 \`ageMs\` 校正）。

## 三、三条可选路线

| 路线 | 做法 | 代价 | 唤醒词 |
|---|---|---|---|
| **A（当前默认）** | 能力诚实上报为否 → rui KWS 触发 turn；\`drain_wakes()\` 已实现但设备不会发事件 | 0（已含在本次迁移） | 中文"你好小瑞"，KWS 在 rui 侧 |
| **B（先证明链路）** | 推 OWW 运行时 + stock \`hey_jarvis_v0.1\` 模型，\`owwOnDevice=on\`，走完整 device_wake 路径 | 半天～1 天（需要实现/借道 shell 平面推文件 + 改 ack/config；真机验证） | 英文 "hey jarvis" |
| **C（目标态）** | 用 \`oww_forge\` 训"你好小瑞"自定义模型，再推设备 | 训练机 ~25 GB 资产、数小时/轮，中文效果需实测；另加 B 的推送与验证 | 中文"你好小瑞"，设备侧唤醒 |

**建议**：先 A 把 0.3.4 端到端跑通并留基线（本次）；再决定是否投入 B（快速验证契约链路，英文唤醒词）或直接 C。
B 的价值是只验机制、不赌中文模型质量；C 才是产品目标，但成败取决于中文 OWW 训练效果。

## 四、对桥/部署的影响（已按此实现）

1. adapter \`probe()\` 只在设备**真的能打分**时宣告 \`device_wake\`/\`barge_via_device\`；
   否则有效能力 = manifest ∩ probe 自动降级为 RUI KWS（契约 §2/§5）。
2. 默认不改变 \`owwOnDevice\`/\`wakeWordEnabled\`（保持 \`off\`），不引入"看起来支持但其实不能用"的状态。
3. manifest 仍声明 v1 \`wake: {source: device, barge_via_device: true}\`——这是插头的上限声明；
   运行期由 probe 决定是否生效。降级时 rui 只记 \`device_wake_fallbacks\`，KWS 不受影响。
4. OWW 运行时/模型安装器 **不在本次范围**，需要单独立项（涉及设备文件推送与模型训练）。

## 五、需要 9 哥 决策

- 唤醒词最终形态：继续中文"你好小瑞"（→ 路线 C，需训模型）/ 接受英文唤醒词先跑通（→ 路线 B）/ 长期双词。
- 若走 C：是否接受用 oww_forge 在本机或服务器上训练（约 25 GB 资产、数小时）并承担中文效果不确定的风险。

# rui v0.2.2 × Dot2/EchoMuse 兼容性分析

> **结论日期**：2026-10-24
> **分析对象**：`rui` tag `v0.2.2`（commit `7bc79c1`）只读 clone `R=/tmp/rui-0.2.2`；
> 本仓库 `rui-dot2-bridge`（只读）记为 `B=/Users/bakeyzhang/AI_Workspace/01_Code/projects/rui-dot2-bridge`。
> 本文只基于源码与文档，不改代码/配置，不重启 `.34` 或设备。
> 引用格式：`R:path:line` / `B:path:line`。

---

## 0. 结论（先看这里）

**能接，但不是“把 bridge 当插件装进去”这种接法。**

v0.2.2 有完整的 **声明式 plug**（`plugin.yaml` + `devices/*.yaml`）和安装向导，但
**没有加载/执行 out-of-tree adapter 代码的机制**：`runtime.entrypoint` 只做字符串校验，
loader 从不 import；`registry.register()` 仍是唯一的进程内注册钩子（`R:src/rui/plugins/loader.py:5-8,55-62`、
`R:docs/socket-plug.md:164-167`、`R:src/rui/device/registry.py:48-50`）。

因此 Dot2 接入 v0.2.2 的最小路径是：

1. **把 bridge 从 0.1.x 契约（`house_ear.*`）改到 v0.2.2 契约（`rui.*`）**；
2. 继续用 **`PYTHONPATH` + `sitecustomize.py` + `registry.register("dot2", ...)`** 注入
   （非官方、镜像升级可能失效）；
3. 让 Dot2 以 **wss** 作为 `devices[]` 里唯一的设备，adapter 自己监听设备端口；
4. 不要指望 v0.2.2 的“连接 `ws://<host>:4412/?token=...`”向导页能生成 EchoMuse 可用的地址。

**4412 不是协议限制，而是 stock LX06 adapter 的约定。** v0.2.2 的 4412 由
`XiaoaiAdapter.open()` 在音频面子进程里监听，协议由 adapter 决定（`R:src/rui/device/xiaoai/adapter.py:135-158`、
`R:docs/device-management-research.md:341`）。Dot2 adapter 完全可以在 4412 上说 TLS + `X-EM-Token`，
但**同一个 rui 实例只能驱动一台设备**（`R:src/rui/audio/client.py:387-416`），
所以它不能和 LX06 同时共用这个 4412。

| 问题 | 结论 |
|---|---|
| A. v0.2.2 还支持 out-of-tree adapter/plug？ | **声明式 plug 支持；可执行 adapter 不支持官方 out-of-tree。** `registry.register` 钩子还在，但只能由进程内代码调用。 |
| B. 4412 谁监听？能在 4412 内识别两种协议？ | **由 adapter 在音频面子进程里监听**；v0.2.2 一个音频面只起一个 adapter，没有协议 demux。stock 4412 是明文 ws + query token，不能直接接 EchoMuse 的 TLS + header token；但 Dot2 adapter 自己可以把 4412 做成 wss。 |
| C. 最小改动 | **改 bridge 契约 + 注入注册 + 配 `devices[]` + 证书/端口**；预计 1–2 天（含真机联调）。TLS 反代、改固件都不是最小路径。 |
| D. Dot2 feature 有位置吗？ | **2ch ref：有，且是完整一等公民；oww_wake：只有“让位”信号的位置，没有“设备唤醒直接起回合”的位置；playback_stats：有 play-head 契约，但固件没有 per-block 上报，且控制台不展示 adapter 扩展 health。** |
| E. 建议 | **主推 C1：bridge 改契约 + 作为 .34 唯一设备跑 wss（4412 或 8870）；备选：Dot2 继续留在 0.1.x/第二实例。** |

---

## A. v0.2.2 是否还支持 out-of-tree adapter / plug？

### A.1 支持的是“声明式 plug”，不是“可执行 adapter”

v0.2.2 的 plug 目录结构是：

```
rui-plugin-<id>/
  plugin.yaml
  devices/<model-id>.yaml
  adapter/            # 可选，代码包（但 0.2 不加载）
```

见 `R:docs/socket-plug.md:26-42`。加载器 `load_plugin()` 只读 YAML、做 Pydantic 校验、
检查 `rui_compat`、解析路径、限制 `third-party + in-process`；**从不 import `entrypoint`**
（`R:src/rui/plugins/loader.py:55-88,186-225`；`R:src/rui/plugins/models.py:12-14`）。
`runtime.entrypoint` 的唯一作用是“必须长得像 `package.module:function`”
（`R:src/rui/plugins/models.py:121-148,176-182`）。

v0.2.2 实际内置的 plug 只有 **LX06 一个**：

- `LX06_PLUG_ID = "xiaoai-lx06"`，`LX06_ADAPTER = "xiaoai"`
  （`R:src/rui/plugins/builtin.py:23-25`）；
- `runtime.entrypoint = "rui.device.xiaoai:build"`、`listen_port: 4412`、
  `token_env: RUI_DEVICE_TOKEN`（`R:src/rui/plugins/builtin.py:60-65`）；
- 仓库里没有 `dot2.plug.json` / `dot2` 的内置 manifest。调研文档里的 `dot2.plug.json`
  只是 **建议稿**（`R:docs/device-management-research.md:663-681`），不是 v0.2.2 的交付物。

### A.2 可执行 adapter 的注册钩子还在，但不在 plug 体系里

`registry.register(name, target)` 仍然存在，docstring 明确说是给 “downstream build”
用的（`R:src/rui/device/registry.py:48-50`）：

```python
_BUILDERS = {
    "xiaoai": "rui.device.xiaoai:build",
    "mock": "rui.device.mock:build",
}
```

`DeviceConfig.adapter` 也已经是普通字符串，不再被 `Literal["xiaoai","mock"]` 拒绝
（`R:src/rui/config/schema.py:120-125`；`docs/architecture.md` 里那条 debt 已被修掉，
见 `R:docs/architecture.md:3-8,154-164`）。

但 v0.2.2 **没有任何代码在启动时扫描外部包并调用 `registry.register`**：

- 没有 `entry_point` / `sitecustomize` / `.pth` 的加载逻辑
  （`sitecustomize`/`PYTHONPATH` 只出现在文档/部署示例里；`runtime.entrypoint` 在插件模型里只做字符串校验；没有启动时加载外部代码的路径）；
- 官方 Docker 镜像也没有 `PYTHONPATH` 注入（`R:Dockerfile:151-171`）；
- `plugins` loader 不碰 `device/registry.py`。

所以 **“out-of-tree adapter”在 v0.2.2 里仍然只能靠进程注入**，和 0.1.x 一样；
但它不再被文档承认为插件机制，属于“downstream build”的半公开钩子。

### A.3 我们现在的 `plug.json` 不能直接放进 `data/plugs/`

`B:plug.json:1-16` 是 0.1.x 的“盲开”格式：`plug`、`requires_socket`、`identity`、
`transport`、`audio`、`capabilities`（对象）、`control`、`entrypoint`。

v0.2.2 的 schema 要求（`R:src/rui/plugins/models.py:304-322,391-407`）：

- `plugin.yaml`：`schema: rui-plugin/v0`、`id`、`name`、`version`、`rui_compat`、
  `license`、`maintainer`、`trust`、`devices: [devices/*.yaml]`；
- `devices/<model>.yaml`：`schema: rui-device/v0`、`id`、`name`、`vendor`、`class`、
  `transport`、`protocol`、`capabilities`（**字符串数组**）、`params`，
  可选 `device_side`、`runtime`、`compatibility`、`acceptance`；
- 两个模型都是 `extra="forbid"`，多余字段直接报错。

`B:plug.json` 缺 `schema`、`id`、`name`、`class`、`devices`，且 `capabilities` 是对象不是数组，
所以 **不会被 v0.2.2 识别**；即使改名成 `plugin.yaml` 也会被拒。需要重写成 v0.2.2 的
`plugin.yaml` + `devices/dot2.yaml`，或者干脆先不用 manifest，直接写 `config.yaml`。

### A.4 如果接受“进程注入”，注册方式/配置字段是什么

**注册入口**（bridge 侧只需改 import）：

```python
# B:src/sitecustomize.py 当前是 house_ear
from rui.device import registry
registry.register("dot2", "rui_dot2_bridge.adapter:build")
```

注入方式：把 `B:src` 只读挂进容器（例如 `/opt/rui-py`），设 `PYTHONPATH=/opt/rui-py`。
`sitecustomize` 会在父进程和音频面子进程启动时自动执行；音频面是
`python -m rui.audio.process`（`R:src/rui/audio/client.py:406-416`），会继承环境变量。
这套做法和 0.1.x 完全同构（`B:docs/deploy.md:80-104`），只是模块名从 `house_ear` 换成 `rui`。

**配置字段**（`R:src/rui/config/schema.py:120-144`）：

```yaml
devices:
  - id: dot2
    adapter: dot2          # 注册后的名字
    host: 192.168.5.34    # inbound 下只用于显示/探测，不用于拨号
    room: 客厅
    wake_words: ["你好小瑞"]
    volume: 100
    listen_port: 4412      # 或 8870；这是 adapter 自己的监听端口
    token_env: RUI_DEVICE_TOKEN
    params: {}             # 可选：放 ref_channels、mic_gain_db 等非 secret 参数
```

`params` 是 v0.2.2 新增的、给 plug 声明非 secret 参数用的字典
（`R:src/rui/config/schema.py:141-144`）。bridge 目前不读 `dev.params`，
改造时可以把 `DOT2_REF_CHANNELS` / `DOT2_MIC_GAIN_DB` 等迁进去，减少环境变量。

**官方镜像的注入示例**（不改 rui 源码）：

> 注意：即使把 manifest 放进 `data/plugs/`，wizard 只会读它做展示/写 `devices[]`，
> **不会因此加载 adapter 代码**；adapter 仍然要靠上面的 `registry.register` 注入。

```yaml
# rui 部署目录旁的 docker-compose.override.yml
services:
  rui:
    volumes:
      - /path/to/rui-dot2-bridge/src:/opt/rui-py:ro
      - /path/to/echomuse-tls:/opt/dot2-tls:ro
    environment:
      PYTHONPATH: /opt/rui-py
      DOT2_SECRETS_DIR: /opt/dot2-tls
      DOT2_REF_CHANNELS: "1"      # 2ch ref 时
```

注意：`R:Dockerfile:151-171` 的官方镜像没有为这套注入留口子；这是**部署层 override**，
不是 v0.2.2 支持的插件安装方式。镜像升级、容器重建、`PYTHONPATH` 被覆盖都会让它失效。

### A.5 v0.2.2 契约 vs bridge 0.1.x 契约逐项对照

| 项目 | v0.2.2 | bridge 现状 | 差异/动作 |
|---|---|---|---|
| import 根包 | `rui.device.contract`（`R:src/rui/device/contract.py:1-20`） | `house_ear.device.contract`（`B:src/rui_dot2_bridge/adapter.py:24`） | **必须改**；也可做 `sys.modules` 别名，但会有重复模块/枚举身份风险，建议直接改 import |
| `DeviceInfo` | `id/adapter/model/host/room/capabilities/transport/channels/sample_rate/bit_depth/mic_channels/ref_channels`（`R:src/rui/device/contract.py:39-59`） | 同名字段全用（`B:src/rui_dot2_bridge/adapter.py:250-263`） | 兼容 |
| `AudioFrame` | `device_id/seq/ts_ms/pcm/channels/sample_rate/reference_channels/meta`（`R:src/rui/device/contract.py:62-95`） | 同名字段全用（`B:src/rui_dot2_bridge/adapter.py:409-417`） | 兼容 |
| 方法名 | `probe/open/close/frames/play/stop_playback/set_volume/set_led`（`R:src/rui/device/contract.py:140-169`） | 全部实现（`B:src/rui_dot2_bridge/adapter.py:330-589`） | 兼容；`set_led` 的 `repaint` 关键字也一致 |
| 新增可选方法 | `drain_signals()`、`reports_consumption/consumed/sink_generation/wait_consumed()`（`R:src/rui/device/contract.py:161-187`） | 未实现 | **可选**；不实现时 core 用默认（空信号、墙钟节拍） |
| `build` 签名 | `Builder = Callable[[DeviceConfig, Config], DeviceAdapter]`（`R:src/rui/device/registry.py:25,71-83`） | `build(dev, cfg)`（`B:src/rui_dot2_bridge/adapter.py:592`） | 兼容 |
| 监听职责 | inbound adapter 的 `open()` 负责监听（`R:docs/adapter-contract.md:25-29`） | `DeviceLink.start()` 监听 TLS/plain（`B:src/rui_dot2_bridge/device_link.py:161-185`） | 兼容；端口由 `dev.listen_port` 传入 |
| token | 值在环境变量，`devices[].token_env`（`R:docs/adapter-contract.md:29`） | `build()` 从 `dev.token_env` 读环境变量（`B:src/rui_dot2_bridge/adapter.py:596-599`） | 兼容；但握手位置不同（见 B.3） |
| feature 协商 | **core 没有 feature 握手**；`Capability` 是声明式（`R:src/rui/device/contract.py:23-31`） | EchoMuse `register → ack.features` 在 bridge 内部完成（`B:src/rui_dot2_bridge/device_link.py:254-324,455-465`） | 兼容；**EchoMuse 的 feature 对 core 透明**，由 adapter 自己解释 |
| 播放布局 | `play(pcm, sample_rate, channels)`；v0.2.2 实际传 `channels=1`、`sample_rate=audio.playback.rate`（`R:src/rui/audio/process.py:759-769,1251`） | 只接受 mono 48 kHz，否则报错（`B:src/rui_dot2_bridge/adapter.py:431-439`） | **必须把 `audio.playback.rate` 设为 48000** |
| 停播语义 | `stop_playback()` 淡出+flush（`R:docs/adapter-contract.md:49-50`） | `stop_playback()` 发 `speaker_flush` 并等 pong（`B:src/rui_dot2_bridge/adapter.py:541-569`） | 兼容；v0.2.2 仍按旧语义调用 |
| 能力枚举 | v0.2.2 增加 `NATIVE_KWS`、`STREAMING_ASR`（`R:src/rui/device/contract.py:23-31`） | 只设置 `PLAYBACK/VOLUME/LED/REFERENCE/MIC_ARRAY`（`B:src/rui_dot2_bridge/adapter.py:243-249`） | 兼容；要显式声明 `NATIVE_KWS` 需改 bridge |

**小结**：方法/数据结构层面几乎是 1:1，真正要改的是 **包名、注册行、配置映射、TLS/端口**，
不是重写 adapter。

---

## B. 4412 端口由谁监听？能否在 4412 内部识别两种协议？

### B.1 监听者是 adapter，不是 FastAPI/控制面

stock v0.2.2 的 4412 监听发生在 `XiaoaiAdapter.open()`：

```python
self._server = await serve(
    self._handle, "0.0.0.0", self.listen_port,
    max_size=None, ping_interval=20, ping_timeout=20,
)
```

见 `R:src/rui/device/xiaoai/adapter.py:135-158`。`self.listen_port = dev.listen_port or 4412`
（`R:src/rui/device/xiaoai/adapter.py:48,69-70`）。

调用链是：

```
rui serve (FastAPI, :8080)
  └─ AudioPlaneSupervisor.start()
       └─ python -m rui.audio.process --device <first>
            └─ _open_adapter() → XiaoaiAdapter.open() → :4412
```

见 `R:src/rui/audio/client.py:387-416`、`R:src/rui/audio/process.py:395-401,1646-1658`。
所以：

- `devices: []` 时音频面不启动，4412 **不会监听**（`.34` 当前就是这个状态，
  `B:docs/deploy-0.2.2-user-report.md:15,18,29`）；
- 4412 不是控制面硬编码的“固定协议端口”，只是 LX06 adapter 的默认值；
  控制台里的 `DEVICE_DEFAULT_LISTEN_PORT = 4412` 仅用于生成 URL/显示
  （`R:src/rui/web/api/app.py:1076-1078`）。

### B.2 v0.2.2 一个音频面只驱动一台设备

`AudioPlaneSupervisor._pick_device()` 从 `build_adapters(cfg)` 里取**第一个**可构建的 adapter，
只 spawn 一个 `rui.audio.process`（`R:src/rui/audio/client.py:387-416`）。
调研文档也明确写“一个音频面一台设备，多设备是 P3”
（`R:docs/device-management-research.md:345`）。

这意味着：

- 即使 `config.yaml` 里写了两台设备，音频面也只会服务第一台；
- 4412（或任何 inbound 端口）同一时刻只属于一个 adapter；
- 没有“按路径/SNI/首字节把连接分给不同 adapter”的核心机制。

### B.3 stock 4412 与 EchoMuse 的握手差异

| 维度 | stock LX06 4412 | EchoMuse Dot2 | 直接兼容？ |
|---|---|---|---|
| 传输 | 明文 `ws://`（`R:src/rui/device/xiaoai/adapter.py:150-157`） | 设备按 `controller.json` 走 `wss://`（也支持 plain endpoint，但生产用 wss；`B:plug.json:11-16`；`B:src/rui_dot2_bridge/device_link.py:216-231`） | ❌ TLS ClientHello 进不了明文 WS server |
| 鉴权 | URL query `?token=`，`hmac.compare_digest`（`R:src/rui/device/xiaoai/adapter.py:372-378`） | `X-EM-Token` header（`B:src/rui_dot2_bridge/device_link.py:233-236`） | ❌ stock 只读 query，header 被忽略；query 为空则 1008 |
| 路径 | stock 不检查路径，任何 path 都进 `_handle`（`R:src/rui/device/xiaoai/adapter.py:328-355`） | `/control` + `/data` 两条逻辑通道（`B:src/rui_dot2_bridge/device_link.py:46-47,238-248`） | ❌ stock 没有 `/control`/`/data` 分流 |
| 控制帧 | `{"Request":...}` / `{"Response":...}` / `{"Event":...}`（`R:src/rui/device/xiaoai/protocol.py:1-21`） | 首条 `/control` 必须是 `register`，adapter 回 `ack`（`B:src/rui_dot2_bridge/device_link.py:254-292`） | ❌ 完全不同的应用协议 |
| 音频帧 | JSON 包装的 `record`/`play`，int16（`R:src/rui/device/xiaoai/protocol.py:12-21`） | `[0x01]` 上行、`[0x02]/[0x03]` 下行（`B:docs/protocol-map.md:1-20`） | ❌ |
| 设备数 | 单连接，后到替换先到（`R:src/rui/device/xiaoai/adapter.py:335-338`） | 单设备；bridge 也是单 control + 单 data（`B:src/rui_dot2_bridge/device_link.py:78-150`） | 双方都单设备，但不能跨协议复用同一 listener |

**结论**：stock 4412 和 EchoMuse 在 **TLS、鉴权位置、路径、应用协议** 四个层面都不兼容。
把 EchoMuse 直接指向 `ws://<host>:4412/?token=...` 不会工作：TLS 先失败；即使改成明文，
query token 也不匹配，且后续帧会被 LX06 协议解析器忽略/丢弃。

### B.4 能否让一个 adapter 在 4412 内部识别两种协议？

**契约没有禁止，但 v0.2.2 没有现成机制，实际不可行/不建议。**

理论上可以写一个“超级 adapter”：

- 自己监听 4412，peek 首字节（`0x16` = TLS）或做 `ssl` 分流；
- TLS 分支按 EchoMuse `/control` + `/data` 处理，明文分支按 LX06 JSON 处理；
- `probe()` 根据当前连接返回不同 `DeviceInfo`。

但在 v0.2.2 里会撞上三个硬限制：

1. **一个音频面只构建一次 AEC/decimator**：`_capture()` 在启动时用第一次
   `probe()` 的 `mic_channels/ref_channels/channels/sample_rate` 初始化
   （`R:src/rui/audio/process.py:502-513`），运行中不会按新连接重建。
   Dot2（1 mic + 0/1 ref）和 LX06（6 mic + 2 ref）布局不同，切换设备会静默用错布局。
2. **一个 supervisor 只选一个 adapter**（`R:src/rui/audio/client.py:387-416`），
   没有“多 adapter 共享 4412”的注册/路由层。
3. **`play()` 的速率/声道假设是全局配置**（`audio.playback.rate`，v0.2.2 固定传
   `channels=1`，`R:src/rui/audio/process.py:759-769`），两种设备的播放格式若要不同，
   需要 per-device 配置，v0.2.2 不支持。

所以“4412 内识别两种协议”只能在**替换核心调度层**的前提下做，不是 v0.2.2 的配置项。

### B.5 那 Dot2 能不能直接让 adapter 在 4412 上说 wss？

**能。** 因为 4412 的协议由 adapter 决定（`R:docs/device-management-research.md:341`；
`R:docs/adapter-contract.md:25-29`），bridge 的 `DeviceLink` 本来就是 TLS server
（`B:src/rui_dot2_bridge/device_link.py:161-185,216-231`），只要把 `dev.listen_port` 设成 4412、
把 `DOT2_SECRETS_DIR` 指向 `server.pem/server.key`，它就能在 4412 上完成
EchoMuse 的 `wss + X-EM-Token + /control + /data` 握手。

代价是：**这个 4412 不再是 LX06 的 4412**。同一实例不能同时接 LX06。
如果 Lead 要求“4412 单入口、Dot2 优先”，这是可行的；如果要求“LX06 和 Dot2 同时在线”，
v0.2.2 做不到（见 C4/C5）。

另外，v0.2.2 控制台的 `device_connect` 会生成：

```python
ws_url = f"{scheme}://{host}:{port}/?token={quote(token)}"
```

见 `R:src/rui/web/api/app.py:3631-3636`。这对 EchoMuse **是错的**：设备要的是
`wss://host:4412/control` + `/data` + `X-EM-Token` header，不是 `ws://.../?token=`。
所以即使端口通了，向导的“连接”步骤也不能照抄；Dot2 必须手工配 `controller.json`，
或改 rui 的连接信息生成逻辑（额外工作）。

---

## C. 若不能直接接，最小改动路径是什么？

先明确：**“不能直接接”的根因不是端口，而是 v0.2.2 不加载 out-of-tree adapter 代码。**
只要还想让 rui 的音频面驱动 Dot2，就必须让 Dot2 adapter 在 rui 进程里可用；
区别只是“怎么让它可用”。

### C.1 方案对比

| 方案 | 做什么 | 工作量（估） | 风险 | 评价 |
|---|---|---|---|---|
| **C1. bridge 改 v0.2.2 契约 + 进程注入** | 改 import/注册；配置 `devices[].adapter=dot2`；adapter 监听 wss 4412 或 8870；证书/端口/2ch 配置 | **1–2 天**（代码 0.5–1 天，真机联调 0.5–1 天） | 中：非官方注入、镜像升级易断、单设备、向导 URL 不匹配 | **最小可行，主推** |
| C1b. 把 dot2 做进 rui 核心 | 在 `src/rui/device/dot2/` 放 adapter，`_BUILDERS` 加一行 | 1–2 天 + 上游 review | 低-中：改核心，需维护 fork/PR | 最稳，但违背“不改 rui” |
| C2. 4412 前加 TLS 反代 | nginx/stunnel 在 4412 终止 TLS，转发到 adapter 的明文端口 | C1 + 0.5–1 天 | 中-高：仍要 C1；反代不翻译 EchoMuse↔LX06；不能把 4412 分给两个 adapter | 只在“必须保留 4412 作为唯一 TLS 入口”时有意义，**不单独成立** |
| C3. 让 EchoMuse 改连明文 4412 + query token | 改固件/设备端 client：去 TLS、改鉴权、改 URL 形态 | 2–5 天 + 重刷/回滚风险 | 高：安全降级、固件改动、仍要 C1 | **不建议** |
| C4. Dot2 留在 0.1.x / 第二实例 | 不动 v0.2.2；Dot2 继续连 0.1.x；或再跑一个 rui 实例 | 0 代码，0.5 天运维 | 低技术风险，高产品复杂度（两套助手/两套配置） | **可作过渡备选** |
| C5. 等上游 sidecar/多设备 | 用 P3 的 sidecar 或多设备能力 | 周级 | 低，但不在 v0.2.2 | 长期正解，不是现在 |

### C.2 C1 的具体改动清单

**代码（bridge 侧，预计 3 个文件）**

1. `B:src/rui_dot2_bridge/adapter.py:24`：
   `from house_ear.device.contract import ...` → `from rui.device.contract import ...`。
   `B:tests/test_adapter.py:111` 的 `house_ear.device.contract` 同步改。
2. `B:src/sitecustomize.py:13-15`：
   `from house_ear.device import registry` → `from rui.device import registry`；
   保留 `registry.register("dot2", "rui_dot2_bridge.adapter:build")`。
3. `B:src/rui_dot2_bridge/adapter.py:592-645`（`build()`）：
   - `token_env`/`listen_port` 已兼容（`B:src/rui_dot2_bridge/adapter.py:596-599,630-633`）；
   - `secrets_dir` 目前是 `getattr(dev,"secrets_dir",None) or DOT2_SECRETS_DIR or "secrets"`
     （`B:src/rui_dot2_bridge/adapter.py:625-629`），v0.2.2 的 `DeviceConfig` 没有
     `secrets_dir`，必须用环境变量或 `dev.params["secrets_dir"]`；
   - `mic_channels/ref_channels` 建议优先读 `dev.params`，其次环境变量，最后 `plug.json`，
     避免 v0.2.2 配置里出现第二份事实；
   - `tls_port` 已优先用 `dev.listen_port`，兼容。

**配置（rui 侧）**

```yaml
devices:
  - id: dot2
    adapter: dot2
    host: 192.168.5.34
    room: 客厅
    wake_words: ["你好小瑞"]
    volume: 100
    listen_port: 4412        # 或 8870；见 E 的决策点
    token_env: RUI_DEVICE_TOKEN
    params: {}               # 可选：ref_channels / mic_gain_db / secrets_dir

audio:
  playback:
    rate: 48000              # 必须；bridge 只接受 48 kHz
    channels: 1
    # prebuffer_ms: 450      # 可选；bridge 自己也有 0.45 s 预缓冲，二选一，别叠加
  capture:
    rate: 16000
    channels: 2              # 2ch ref 时；单声道时 1
    mic_channels: 1
    ref_channels: 1          # 单声道时 0
    bits: 16
  aec:
    enabled: true            # 2ch ref 时；单声道 + 设备 AEC 时按 bridge 现状
    cancel: mix
```

`audio.playback.rate` 是 v0.2.2 的全局配置，默认 16000（`R:src/rui/config/schema.py:227-237`）；
bridge 的 `play()` 会拒绝非 48000（`B:src/rui_dot2_bridge/adapter.py:435-439`），
不设就会在第一次回复时报错。

**部署（官方镜像 + override）**

- 挂载 `B:src` 到 `/opt/rui-py:ro`，设 `PYTHONPATH`；
- 挂载 EchoMuse TLS 三件套（`server.pem`、`server.key`）到 `DOT2_SECRETS_DIR`；
- `DOT2_REF_CHANNELS=1`（2ch）或 `0`（单声道）；
- 设备侧 `controller.json` 的 endpoint 指向 `.34` 的 `tls_port`；
- 不改固件、不重刷（`B:README.md:23`）。

**风险**

- **非官方注入**：v0.2.2 镜像升级、`PYTHONPATH` 被覆盖、`sitecustomize` 冲突都会让
  adapter 消失；控制台只显示 `unknown adapter: dot2`，不会崩，但设备离线。
- **单设备**：Dot2 和 LX06 不能同时在线；`devices[]` 里谁排第一谁被
  `_pick_device()` 选中（`R:src/rui/audio/client.py:387-393`）。
- **向导不匹配**：`device_connect` 生成的 `ws://.../?token=` 不能给 EchoMuse 用，
  只能手工配设备；要让向导显示正确信息需要改 rui 的 API/UI（额外 1–2 天）。
- **token 模型**：v0.2.2 是共享 `RUI_DEVICE_TOKEN`，没有每设备 token
  （`R:docs/device-management-research.md:141-143`）；EchoMuse 设备端已有自己的 token，
  必须让两边一致，且不要把它写进仓库/日志。
- **bridge 的 fail-open**：`DeviceLink._authorized()` 在 `token is None` 时直接放行
  （`B:src/rui_dot2_bridge/device_link.py:233-236`）。部署时必须确保 `token_env`
  有值，否则任何能连到端口的人都能开麦。

### C.3 为什么 TLS 反代不是“独立方案”

一个 TLS 反代能做的只有：

```
设备 --wss:4412--> nginx/stunnel --ws:8870--> adapter
```

它不能把 EchoMuse 的 `/control` + `/data` 翻译成 LX06 的 `Request/Response`；
也不能让 v0.2.2 同时跑两个 adapter。因此：

- 如果后端是 **Dot2 adapter**：反代只是多做一次 TLS 终止，bridge 自己已经能终止 TLS，
  收益是“4412 统一入口/证书集中”，代价是多一个进程和证书配置。
- 如果后端是 **stock LX06 adapter**：EchoMuse 帧进去仍然没人解析，等于没接。
- 如果想让 4412 同时接 LX06 和 Dot2：反代需要按 SNI/ALPN/首字节分流到两个后端，
  但 v0.2.2 只有一个音频面/adapter，第二个后端没有消费者。

所以 C2 只能作为 C1 的部署变体，不是替代路径。另注意 bridge 当前只开 TLS
（`_link_kwargs` 里 `plain_port=None`，`B:src/rui_dot2_bridge/adapter.py:166-180`），
要用“TLS 反代 → 明文后端”还得先给 bridge 开 plain 端口，反而多一处配置。

### C.4 改 EchoMuse 固件为什么不是好路径

要让设备改连明文 4412 + query token，需要改 EchoMuse 设备端的 Go 代码：

- 去掉/降级 TLS；
- 把 `X-EM-Token` header 改成 query token；
- 可能还要改 `controller.json` 的 endpoint 语义。

代价：

- 设备端固件改动 + 重刷/回滚风险（`B:docs/p3-firmware-patch.md:3-5` 已有 A/B 槽经验，
  但那是 2ch ref 补丁，不是去 TLS）；
- 安全降级：麦克风/扬声器链路变明文；
- **仍然需要 C1**，因为 rui 侧还是得有一个会说 EchoMuse 协议的 adapter。

因此只在“绝对不能跑 wss”这种硬约束下才考虑，不作为建议。

---

## D. Dot2 的 feature 在 v0.2.2 契约里有没有对应位置？

| Dot2 feature | v0.2.2 的位置 | bridge 现状 | 判定 |
|---|---|---|---|
| **2ch ref**（固件补丁） | `DeviceInfo.ref_channels` + `AudioFrame.reference_channels` + `Capability.REFERENCE`；AEC 按 `info.mic_channels/ref_channels` 构建（`R:src/rui/device/contract.py:23-31,39-59,62-95`；`R:src/rui/audio/process.py:489-507`） | 已支持：`ref_channels>0` 时加 `REFERENCE`，每帧 `reference_channels=self.ref_channels`（`B:src/rui_dot2_bridge/adapter.py:243-263,409-417`）；固件补丁已装（`B:docs/p3-firmware-patch.md:3-5`） | **✅ 有一等公民位置**；默认 `ref_channels=0`，要显式打开：`DOT2_REF_CHANNELS=1` 或 `dev.params.ref_channels=1`，并在 `ack.features` 宣告 `dot2_hw_ref`（`B:src/rui_dot2_bridge/device_link.py:93,118,455-460`） |
| **oww_wake**（设备侧唤醒） | 只有 `Capability.NATIVE_KWS`（声明式，core 没有读它做逻辑，`R:src/rui/device/contract.py:23-31`）和 `drain_signals()` → `VendorSignalMsg` → `VendorWindow`（`R:src/rui/device/contract.py:161-167`；`R:src/rui/audio/process.py:544-563`；`R:src/rui/web/api/app.py:1521-1567`） | **未实现**：bridge 没有 `drain_signals`；设备控制消息里未知 type 只 `log.debug`（`B:src/rui_dot2_bridge/device_link.py:366-375`） | **⚠ 只有“让位”语义，没有“设备唤醒直接起回合”**。v0.2.2 收到 `kind="wake"` 只会 abort/静音/停播，不会开新 session（`R:src/rui/web/api/app.py:1552-1565`）。若要让 oww_wake 成为正式唤醒源，需要新的契约/核心改动，不是配置能解决 |
| **playback_stats**（设备播放统计） | `reports_consumption/consumed/sink_generation/wait_consumed`（`R:src/rui/device/contract.py:171-187`）；core 用它做节拍，否则退回墙钟（`R:src/rui/audio/process.py:849-870`）；`adapter.health()` 可带额外字段（`R:src/rui/audio/process.py:1519-1540`） | bridge 的 `stats` 只 `log.debug`（`B:src/rui_dot2_bridge/device_link.py:366-367`）；`health()` 有 `rtt_ms/dropped_audio/mic_frames` 等（`B:src/rui_dot2_bridge/adapter.py:304-324`），但没实现 `reports_consumption/wait_consumed` | **⚠ 部分**。v0.2.2 的 play-head 契约在，但 EchoMuse v2.18.1 没有 per-block play-head，`plug.json` 自己写明（`B:plug.json:64-69`），所以只能墙钟节拍；`health()` 里的 `rtt_ms/dropped_audio/mic_frames` 在 core 里没有被消费（metrics 只用 `connected/rejected`，`R:src/rui/audio/process.py:1539-1540`；设备详情只用 `connected/latency_ms/connected_at`，`R:src/rui/web/api/app.py:849-874,3583-3597`），**不会显示这些统计** |
| LED / 音量 | `Capability.LED/VOLUME`、`set_led()`、`services.set_volume()`（`R:src/rui/device/contract.py:23-31,112-137,169`） | 已实现 LED 12 灯、音量 0–255 映射（`B:src/rui_dot2_bridge/adapter.py:296-327,571-589`） | **✅**；音量上限/刻度是 bridge 自己的策略，v0.2.2 只给 0–100 |
| 按键 | `Capability.KEYS` + `drain_signals()`（`R:src/rui/device/contract.py:23-31,161-167`） | `button` 消息只打日志，`on_button` 回调未接到 adapter（`B:src/rui_dot2_bridge/device_link.py:359-365`；`B:src/rui_dot2_bridge/adapter.py` 未设置） | **⚠ 未接**；需要 bridge 实现 `drain_signals` 并在 core 消费 |
| 播放预缓冲 | `audio.playback.prebuffer_ms`，文档明确点名 Echo Dot 2 issue #1 的 400 ms 分批（`R:src/rui/config/schema.py:247-254`） | bridge 自己有 `PREBUFFER_S=0.45`（`B:src/rui_dot2_bridge/adapter.py:95-101,188-196`） | **✅ 但别叠加**；要么用 rui 的 `prebuffer_ms: 450`，要么保留 bridge 的预缓冲，两个都开会让首字延迟变成 ~0.9 s |

**D 的核心判断**：

- **2ch ref 是唯一“完全对得上”的高级 feature**；v0.2.2 的 AEC 链路就是按
  `reference_channels` 写的，bridge 的帧布局也一致。
- **oww_wake 和 playback_stats 都不是“配置一下就有”**：前者缺“设备唤醒→起回合”的契约，
  后者缺固件 play-head 和控制台展示位。它们可以作为“声明 + 日志 + 健康字段”存在，
  但不能承诺完整功能。
- `Capability` 主要影响控制台展示；真正决定 DSP 的是 `DeviceInfo.ref_channels` 和
  `AudioFrame.reference_channels`（`R:src/rui/audio/process.py:489-507`）。

---

## E. 建议方案

### E.1 主推：C1 + Dot2 作为 `.34` 的唯一设备

**适用前提**：`.34` 当前没有 LX06 设备，Dot2 是目标设备；可以接受“这一台 rui 只接 Dot2”。

**做法**

1. 在 bridge 仓库完成 v0.2.2 契约适配（只改 3 个文件：`adapter.py` import、`sitecustomize.py`
   import、`build()` 的参数映射；见 C.2）。
2. 用 `docker-compose.override.yml` 挂载 `B:src`，设 `PYTHONPATH`、
   `DOT2_SECRETS_DIR`、`DOT2_REF_CHANNELS`（2ch 时 1）。
3. `config.yaml` 写 `devices[0]`：`adapter: dot2`，`listen_port` 选 **4412**（单入口）
   或 **8870**（与 bridge 现有部署一致），`token_env` 指向设备 token，
   `audio.playback.rate: 48000`，2ch 时 `capture 2/1`。
4. 把 EchoMuse 的 `server.pem/server.key` 放进 `DOT2_SECRETS_DIR`；
   设备 `controller.json` 的 `tls_port` 指向所选端口。
5. 启动后确认：`ss -ltnp | grep <port>` 有 rui 音频面子进程监听；
   设备日志 `register → ack → mic_start`；`/api/v1/devices/dot2` 的 `connected=true`。
6. 验收 2ch：`probe()` 返回 `channels=2, mic_channels=1, ref_channels=1`；
   `metrics.aec_enabled=true`、`ref_active=true`；播放 48 kHz 不报错。
7. 回滚：设备 `controller.json` 指回旧 controller（有序 endpoints 会自动 fallback，
   `B:docs/deploy.md:144-149`）；停掉 override/容器即可。

**为什么主推它**

- 它是唯一不修改 rui 核心、又能让 v0.2.2 音频面真正驱动 Dot2 的路径；
- bridge 的方法/帧结构已经和 v0.2.2 高度一致，改造成本最低；
- 4412 可以作为单入口（adapter 自己说 wss），不需要额外反代；
- 2ch ref 在 v0.2.2 里有完整位置，能兑现 Dot2 的最高价值 feature。

**它的代价**

- 非官方注入，镜像升级要重新验证；
- 只能一台设备；LX06 不能同时在线；
- 控制台“连接”页不能直接用，需要手工配设备；
- token 是共享的，不是每设备凭据。

### E.2 备选：Dot2 继续留在 0.1.x / 第二实例

如果 Lead 认为“不能接受非官方注入”或“LX06 必须同时在线”，则：

- **E2a（最稳）**：Dot2 继续连现有 0.1.x rui（bridge 不动），v0.2.2 只负责 LX06/其他；
  两套实例、两套配置、两个助手，但零代码风险。
- **E2b（折中）**：在 `.34` 上再跑一个独立的 rui 0.1.x 容器/端口专供 Dot2，
  v0.2.2 保留给 LX06；需要两套模型/密钥/控制台，运维成本高。
- **E2c（长期）**：等 rui P3 的多设备/sidecar 能力，把 dot2 做成真正的 sidecar 插头；
  这是架构正解，但不在 v0.2.2。

### E.3 不推荐

- **C2 TLS 反代单独上**：不翻译协议，仍需 C1；只会多一层证书/端口。
- **C3 改 EchoMuse 去 TLS**：安全降级 + 固件风险 + 仍需 C1。
- **把 `B:plug.json` 直接丢进 `data/plugs/`**：schema 不匹配，不会被加载；
  即使改成 v0.2.2 manifest，`entrypoint` 也不会被执行。

### E.4 需要 Lead 决策的点

| # | 决策点 | 选项/影响 |
|---|---|---|
| 1 | **LX06 是否必须与 Dot2 同时在 `.34` 在线？** | 是 → v0.2.2 单设备做不到，选 E2 或等 P3；否 → C1 可行 |
| 2 | **端口用 4412 还是 8870？** | 4412 = 单入口、符合现有部署叙事，但该端口不再是 LX06 协议；8870 = 与 bridge 现部署一致、设备改动小、语义清晰，但多一个入口 |
| 3 | **接受非官方 `PYTHONPATH/sitecustomize` 注入吗？** | 接受 → C1，1–2 天；不接受 → C1b 改核心/PR，或 E2 |
| 4 | **是否接受“每次 rui 镜像升级都要回归 Dot2”？** | 这是 C1 的长期维护成本；C1b/上游 PR 可降低 |
| 5 | **TLS 证书由谁持有/轮换？** | 复用 EchoMuse CA/server.pem；设备信任的 CA 不变；换证书要同步设备 |
| 6 | **token 用哪一个？** | 复用 EchoMuse 设备 token 作为 `RUI_DEVICE_TOKEN`（两边一致），还是生成新 token 并改设备；没有每设备 token |
| 7 | **AEC 走 2ch ref 还是 mono + 设备 AEC？** | 2ch = rui AEC/barge 更好，但 CPU/带宽更高；mono = 现网稳定，但 barge 受限 |
| 8 | **oww_wake / playback_stats 是否必须？** | 必须 → 需要新增契约/核心改动（不是配置）；不必须 → 先按“声明+日志”接受 |
| 9 | **控制台连接向导是否必须正确？** | 必须 → 改 `device_connect` 支持 wss/header/path（1–2 天）；不必须 → 文档手工配设备 |
| 10 | **回滚窗口/责任人** | 设备 `controller.json` 回旧 controller；容器 override 可一键停；固件 A/B 槽已有 |

---

## F. 最小验收清单（C1 执行后）

- [ ] `config.yaml` 的 `devices[0].adapter=dot2`，`audio.playback.rate=48000`；
- [ ] 容器内 `PYTHONPATH` 生效，`python -c "from rui.device import registry; print(registry.available())"` 含 `dot2`；
- [ ] 启动后 `ss -ltnp | grep <port>` 有监听，且监听者是 `rui.audio.process` 子进程；
- [ ] 设备日志出现 `register → ack → mic_start`；错误 token 时出现 1008/拒绝；
- [ ] `GET /api/v1/devices/dot2` 的 `live.connected=true`、`capabilities` 含 `reference`（2ch 时）；
- [ ] 上行帧：`channels=1/2`、`sample_rate=16000`、`reference_channels=0/1`；
- [ ] 下行：48 kHz mono，`stop_playback` 后无残留，EOS 后设备回到 idle；
- [ ] 2ch 时 `metrics.aec_enabled=true`、`ref_active=true`，ERLE 有非零值；
- [ ] 唤醒 → ASR → LLM → TTS 一轮成功；断线后设备自动重拨；
- [ ] 回滚：`controller.json` 指回旧 controller，设备 30–40 s 内恢复；
- [ ] 记录 v0.2.2 镜像 digest / override 文件 / 证书路径，便于升级回归。

---

## G. 关键证据索引

| 结论 | 证据 |
|---|---|
| plug loader 不执行 entrypoint | `R:src/rui/plugins/loader.py:5-8,55-62`；`R:src/rui/plugins/models.py:12-14`；`R:docs/socket-plug.md:164-167` |
| registry 只有 xiaoai/mock，register 是 downstream 钩子 | `R:src/rui/device/registry.py:27-32,48-50` |
| v0.2.2 内置 plug 只有 LX06 | `R:src/rui/plugins/builtin.py:23-25,60-65,100-121` |
| 第三方 in-process 被拒；sidecar 向导不支持 | `R:src/rui/plugins/loader.py:205-217`；`R:src/rui/web/api/setup_api.py:114-122` |
| `DeviceConfig.adapter` 是字符串，可配 dot2 | `R:src/rui/config/schema.py:120-144` |
| 4412 由 XiaoaiAdapter 监听 | `R:src/rui/device/xiaoai/adapter.py:48,69-70,135-158` |
| 一个音频面只选第一台设备 | `R:src/rui/audio/client.py:387-416`；`R:docs/device-management-research.md:345` |
| stock 4412 明文 + query token | `R:src/rui/device/xiaoai/adapter.py:150-157,328-355,372-378` |
| EchoMuse wss + header + /control /data | `B:src/rui_dot2_bridge/device_link.py:46-47,161-185,216-247`；`B:plug.json:11-16` |
| v0.2.2 支持 2ch ref | `R:src/rui/device/contract.py:23-31,62-95`；`R:src/rui/audio/process.py:489-507` |
| oww_wake 只到“让位” | `R:src/rui/device/contract.py:23-31,161-167`；`R:src/rui/audio/process.py:544-563`；`R:src/rui/web/api/app.py:1521-1565` |
| playback_stats 只有 play-head 契约 | `R:src/rui/device/contract.py:171-187`；`R:src/rui/audio/process.py:849-870`；`B:plug.json:64-69` |
| 控制台连接 URL 不适配 EchoMuse | `R:src/rui/web/api/app.py:3631-3636` |
| bridge 的 0.1.x import/注册 | `B:src/rui_dot2_bridge/adapter.py:24`；`B:src/sitecustomize.py:13-15` |
| bridge 的 build 参数映射 | `B:src/rui_dot2_bridge/adapter.py:592-645` |
| bridge 的 TLS/token 行为 | `B:src/rui_dot2_bridge/device_link.py:216-247` |
| bridge 的 2ch 帧布局 | `B:src/rui_dot2_bridge/adapter.py:409-417` |
| `.34` 当前 `devices: []`、4412 未监听 | `B:docs/deploy-0.2.2-user-report.md:15,18,29` |

---

## H. 限制与未验证项

1. 本文是**源码/文档静态分析**，没有在 `.34` 上启动音频面、没有连真机；
   所有运行时结论都来自代码路径，未经本轮实测。
2. EchoMuse 设备端的 TLS 校验细节（是否校 hostname、是否接受 4412 端口）来自
   bridge 现有部署文档与代码，未重新读固件；实际改端口前需在设备上确认
   `controller.json` 的 `tls_port` 语义。
3. “1–2 天”是熟悉 bridge 的工程师的估算，不含上游 review、证书申请、真机声学调参。
4. `oww_wake` 的具体报文形状未在 bridge 代码中找到消费者；如果固件有 `oww_wake` 事件，
   当前会落入 `B:src/rui_dot2_bridge/device_link.py:374-375` 的 debug 分支。
5. v0.2.2 的 `data/plugs` 是否允许 `trust: trusted` 自声明、以及
   `_plug_error()` 不校验 adapter 是否注册，属于实现细节/潜在坑；
   本文不建议依赖该路径接入 Dot2。


## 决策记录（2026-10-24，9 哥）

- **暂停开发**：先把请求转 rui 侧，等其回复并完善 plug/接口文档后再实现，避免按旧假设返工。
- **端口**：Dot 保持 **8870**（wss），设备 `controller.json` 不变。
- **LX06**：**必须与 Dot2 共存**——卫星音箱是多台，唤醒后谁接管由 **rui 决定**，不是音箱决定。
- **AEC 归属**：等 rui 侧回复后再定（当前设备侧 AEC 实测可用）。
- 需求清单见 `docs/rui-0.3-requirements.md`。

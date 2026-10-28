# 新设备接入：一条命令

把一台**已解锁、已刷 EchoMuse** 的 Echo Dot 2 指到本部署上。手工步骤（改三个文件 + 杀进程 + 等重连）
收进 `scripts/onboard_device.py`，并且**不把任何真实地址写进仓库** —— 主机、端口、令牌、序列号全是参数。

## 前置条件

1. 设备已解锁（`adb shell su -c id` 返回 `uid=0(root)`），已刷 EchoMuse 设备端。
2. 设备用**数据线**插在这台机器上（`adb devices` 能看到它，模拟器会被自动忽略）。
3. rui 控制台里**已经建好这台设备**并启用了它 —— 脚本要从 rui 读它的 `listen_port`。新建时：
   - `listen_port` 每台唯一（例如 8870/8871/8873/8874 依次给不同房间）；
   - `params.control_port` 给 `0`（或每台唯一），否则第二台起 `/loudness` 通道会撞端口。
4. 手上有两样东西（**都不进仓库**）：rui 的 API 令牌；本部署的 `ca.pem` + `device_token`。

## 要填什么

| 参数 | 必填 | 从哪来 | 例子 |
|---|---|---|---|
| `--device-id` | ✅ | 控制台设备页的 id | `dot2-b` |
| `--rui` | ✅ | rui 的访问地址 | `http://192.0.2.10:8080` |
| `--rui-token-file` | ✅¹ | 宿主上的控制台令牌文件，拷到本机 `chmod 600` | `~/.rui/console-token.txt` |
| `--secrets-dir` | 换环境时✅² | 含 `ca.pem` + `device_token` 的目录 | `~/rui/dot2-secrets` |
| `--serial` | 否 | `adb devices` 的序列号；只有一台时自动识别 | `G090LF0000000000` |
| `--controller-host` | 否 | **设备**该拨的地址；默认取 `--rui` 的主机名 | `192.0.2.10` |
| `--controller-port` | 否 | 设备该拨的端口；默认取 rui 里该设备的 `listen_port` | `8871` |
| `--fallback HOST:PORT` | 否 | 兜底 endpoint，可重复，按顺序尝试 | `192.0.2.11:8870` |
| `--mdns` | 否 | 所有 endpoint 都失败后再试一次 mDNS（默认关） | — |

¹ 也可以改用环境变量：`export RUI_TOKEN=...`。
² 只重指向（设备已经信任本部署的 CA 和令牌）时**不要传** `--secrets-dir`，脚本就只重写 `controller.json`。

## 两种用法

**A. 只改地址**（设备本来就连本部署，换 IP / 换端口 / 加兜底）：

```bash
python scripts/onboard_device.py \
  --device-id dot2-b \
  --rui http://192.0.2.10:8080 \
  --rui-token-file ~/.rui/console-token.txt \
  --fallback 192.0.2.11:8871
```

**B. 新设备 / 换部署**（设备上是**别的** controller 的 CA 和令牌，例如刚刷完机）：

```bash
python scripts/onboard_device.py \
  --device-id dot2-b \
  --rui http://192.0.2.10:8080 \
  --rui-token-file ~/.rui/console-token.txt \
  --secrets-dir ~/rui/dot2-secrets
```

先看计划、不动设备：

```bash
python ... --dry-run          # 打印 plan，退出码 2
python ... --yes              # 真正写入并等待设备连上来
```

没有 `--yes` 时脚本只打印计划、不写任何东西。

## 它到底改了什么

全部在设备的 `/data/local/etc/echomuse/`（上游源码常量，见
`device/internal/discovery/static.go`、`device/internal/client/tlscreds.go`）：

| 文件 | 内容 | 为什么必须 |
|---|---|---|
| `controller.json` | `{"endpoints":[{"host":…,"port":…,"tls_port":…}],"mdns":false}` | 静态 controller 地址。**没有它设备只会走 mDNS**，而 mDNS 在很多部署里到不了设备 |
| `ca.pem` | 本部署的 controller CA | 设备 pin 这个 CA，TLS 证书对不上就不握手 |
| `token` | 本部署的 `device_token`（去行尾换行） | 作为 `X-EM-Token` 下发，对不上服务端直接关连接 |

流程：备份现有文件为 `*.bak-<时间戳>` → push 到 `/data/local/tmp` → `su cp` 进配置目录并设权限
（`644` / token `600`）→ **逐字节读回校验** → `kill -9` 设备端 server（`start_server.sh` 的
supervisor 会在 2 秒内拉起重读配置）→ 轮询 rui API 直到该设备 `connected`。

端口约定：一个音频平面的 TLS 端口就是它在 rui 里的 `listen_port`（`src/rui_dot2_bridge/adapter.py`，
`tls_port = listen_port`），生产部署不另开明文口，所以 `port` 和 `tls_port` 填同一个值。

## 验证

```bash
# rui 侧
curl -s -H "X-Auth-Token: $RUI_TOKEN" http://192.0.2.10:8080/api/v2/devices \
  | python3 -c 'import json,sys; [print(i["id"], i["listen_port"], i["status"], i["connected"]) for i in json.load(sys.stdin)["items"]]'

# 设备侧（能看到它拨向哪里）
adb shell 'su -c "cat /data/local/etc/echomuse/controller.json"'
```

脚本自己也会等到 `connected` 才返回 0。

## 回滚

脚本打印的备份后缀就是回滚点：

```
rollback: on the device, cp /data/local/etc/echomuse/<file>.bak-<时间戳> over <file>
          then kill -9 the server pid; start_server.sh restarts it
```

即：把 `*.bak-<时间戳>` 拷回原名 → 杀掉 server pid → supervisor 自动拉起。

## 常见错误

| 现象 | 原因 |
|---|---|
| `no online adb device` | 线不是数据线 / 设备没解锁 / 授权弹窗没点 |
| `more than one device online` | 同时插了多台，传 `--serial` |
| `device 'x' is not configured in rui` | 控制台里还没建这台设备（脚本要读它的 `listen_port`） |
| `adb root unavailable` | 设备没 root（未解锁），先走 firebreak 解锁 |
| `read-back mismatch` | 分区只读或空间不足；确认用的是 root 写 |
| **手敲命令时**报 `Read-only file system` | `adb shell su -c '...'` 的引号被本地 shell 吃掉，后半段以 `uid=2000(shell)` 执行；写成 `adb shell "su -c '...'"` 或直接用本脚本 |

## 安全

- 令牌只在机器 0700 临时目录里暂存，推送后立即删除；设备上也只留配置目录里的那一份。
- `ca.pem` / `device_token` / 真实主机地址**都不要提交**：脚本里全是参数，仓库里一个真实地址都没有。

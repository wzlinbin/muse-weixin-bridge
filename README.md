# Muse 微信桥（文字版 v1）

让一个 Muse 实例通过个人微信号收发文字消息：微信里发消息，Muse 能收到、能回复。
已在 2026-11-02 实测跑通文字单轮往返。

## 原理

本桥实现的是腾讯官方 OpenClaw 微信通道插件文档里的 iLink Bot 协议，
协议文档见 Tencent/openclaw-weixin 仓库的 `docs/protocol_zh_CN.md`（MIT 开源）。
后端默认 `https://ilinkai.weixin.qq.com`，无需公网 IP。

| 步骤 | 接口 |
| --- | --- |
| 扫码登录 | `POST /ilink/bot/get_bot_qrcode?bot_type=3`，轮询 `GET /ilink/bot/get_qrcode_status` |
| 收消息 | `POST /ilink/bot/getupdates`（长轮询，游标 `get_updates_buf` 持久化） |
| 发消息 | `POST /ilink/bot/sendmessage`（文本 item，带入站消息的 `context_token`） |
| 生命周期 | `POST /ilink/bot/msg/notifystart` / `notifystop` |

请求头要点：`AuthorizationType: ilink_bot_token`，`X-WECHAT-UIN` 为 base64(随机 uint32
十进制串），`iLink-App-Id: bot`，`iLink-App-ClientVersion` 为版本 2.4.10 的编码，
`base_info` 为 `{channel_version: "2.4.10", bot_agent: "MuseBridge/1.0"}`。

## 目录结构

```
muse-weixin-bridge/
├── bridge.py                  # 桥主程序（登录 + 运行，唯一需要长期跑的进程）
├── install.sh                 # 安装脚本：建 venv、装依赖、建 .secrets/
├── hooks/
│   ├── weixin-inbox.sh        # 收件箱轮询脚本：有新消息时唤醒 agent
│   └── weixin-inbox.prompt.md # 唤醒后 agent 的回复提示词模板
├── README.md
├── LICENSE                    # MIT
└── .gitignore                 # 凭证、状态、消息记录、日志一律不入库
```

运行时还会生成（均已列入 `.gitignore`，不要分享、不要入库）：

| 文件 | 用途 |
| --- | --- |
| `.secrets/credentials.json`（0600） | 登录凭证：bot_token、bot id、user id，只存本地 |
| `state.json` | 长轮询游标、`outbox_offset`、已见 message_id |
| `inbox.jsonl` / `inbox_raw.jsonl` | 收到的消息（去重后 / 原始） |
| `outbox.jsonl` | 待发送 / 已发送的回复队列 |
| `login_qr.png`、`login_status.json`、`verify_code.txt` | 登录过程文件 |
| `run.log` | 桥运行日志 |

## 安装

```bash
bash install.sh
```

需要 Python 3 和 `qrcode`、`pillow`（安装脚本会装进 `.venv/`）。

## 登录

```bash
.venv/bin/python bridge.py login
```

- 程序生成二维码（保存为 `login_qr.png`，同时输出可点开的链接），用微信扫码。
- **二维码服务端约 2 分钟过期**：扫旧图会报过期。程序最多自动刷新 3 次，
  整个登录等待上限约 8 分钟；超时请重新跑 `login`。
- 如果服务端要求配对码，程序会提示把微信里显示的数字写进 `verify_code.txt`，
  轮询会自动带上它继续。
- 登录成功后凭证写入 `.secrets/credentials.json`（0600），终端和日志里
  **永远不会打印 token**。

## 运行

```bash
.venv/bin/python bridge.py run
```

- 收消息：长轮询写入 `inbox.jsonl`，按 message_id 去重。
- 发消息：独立发送线程每 2 秒扫一次 `outbox.jsonl`，写好约 2 秒发出。
  （不要跟长轮询走同一循环，那样延迟会到 30–40 秒。）
- 回复格式（`outbox.jsonl` 每行一个 JSON）：

```json
{"to_user_id": "<对方微信 id>", "context_token": "<入站消息的 context_token>", "text": "<回复>", "in_reply_to": "<入站消息的 message_id>"}
```

- 去重约定：回复方按 `in_reply_to` 判断某条入站消息是否已回复，
  不要重复回复。
- 服务端返回 ret/errcode **-14 表示凭证失效**：桥会暂停所有请求 1 小时，
  之后需重新扫码登录。
- 退出时调 `notifystop`。注意：桥是普通前台/后台进程，VM 重启或替换后
  需要重新启动，恢复路径需自行用 systemd 或 cron 兜底。

## 接入 Muse 自动回复（hook）

1. 把 `hooks/weixin-inbox.sh` 放到 Muse 的 hook 脚本目录，设 30 秒轮询，
   并设置环境变量 `BRIDGE_DIR` 指向桥目录（默认 `~/workspace/weixin-bridge`）。
2. 唤醒后的 agent 按 `hooks/weixin-inbox.prompt.md` 的提示词工作：
   读 `inbox.jsonl`，只回 `in_reply_to` 未出现过的新消息，回复追加到
   `outbox.jsonl`，处理完向主会话简要汇报。
3. 媒体限制：文字版桥看不到图片/语音/文件内容，入站只有占位文本时，
   应如实说还看不到，不要假装看到了。

## 故障排查（都是实测踩过的坑）

- **扫码报过期**：二维码只有约 2 分钟寿命，务必扫最新的一张，不要扫聊天记录里的旧图。
- **回复迟迟发不出去**：先看 `outbox.jsonl` 是不是每行一个合法 JSON。
  曾有一次因为某行缺换行符，两条回复粘成一行，发送线程静默跳过了整行。
  修复办法：把粘住的行拆成两行、行尾补换行符，并确认 `state.json` 的
  `outbox_offset` 指向未发送的那一行。预防办法已写进提示词模板：
  **每条回复必须单独占一行，行尾必须有换行符**；发送线程遇到坏行要记日志，
  不要静默跳过。
- **-14 停摆**：凭证失效，暂停 1 小时后仍需重新扫码登录，填账号密码没用。
- **桥停了**：查 `run.log` 尾部；`state.json` 还在就能断点续跑，不会丢游标，
  但已写进 `outbox.jsonl` 又没发出的行要先确认 `outbox_offset` 是否对齐，
  避免重发。
- **收不到消息**：确认 `run` 进程还在、且 `notifystart` 成功；长轮询游标存在
  `state.json` 里，删了会从头拉取（去重靠 `seen_ids`，仍建议不要手动删）。

## 安全提醒

- `.secrets/` 只存本地、0600 权限，**绝不要入库、不要发给任何人、不要贴进聊天记录**。
- 换一台 Muse 安装时，只复制本仓库的程序文件，凭证必须重新扫码生成，
  不能把旧的 `credentials.json` 拷过去共用。
- 本桥是个人号兼容实现，行为上等同于一个挂机的微信号；请遵守微信的使用规范，
  不要用它群发、营销或做高频自动化。

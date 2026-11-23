# astrbot-plugin-private-settle

私聊话段合并插件，用于 [AstrBot](https://github.com/AstrBotDevs/AstrBot)。

群聊里用户把一句话拆成三条发，判官的 `burst_settle` 会等这一口气说完再回一次；
私聊没有这一层——私聊必回复，判官链在 `need_active_reply` 第一行就
`message_type != GROUP_MESSAGE` 短路了。结果就是用户连发三条，机器人回三条。

这个插件补这一层，而且**不是只回最后一条**：把连发的几条合成一条用户消息再请求模型，
模型看到的是完整的一段话。

## 行为

```
A 到达 → 当 owner，等 settle（默认 5s，每来一条新消息顺延）且距上次回复满 min_gap（默认 10s）
B、C 到达 → 文本/图片并进这一组，自己 stop_event（不回复、不进历史）
安静够了 → message_str = "A\nB\nC"，图片接进消息链 → 只请求一次 LLM
```

- **合并上限** `merge_window`（默认 20s）：一直说个不停到点就发，不丢话；
- **最短回复间隔** `min_gap`（默认 10s）：刚回完就来的话不另起一轮，继续攒成一批。
  只有 `settle` 时，真人停三四秒补一句，上一组早发出去了，就会变成"每句都回"
  （实测差 0.58 秒就落单）——这是 1.0.2 修的主要问题；
- **在途排队** `inflight_wait`（默认 45s）：上一条还在生成时先等它结束，
  否则 B、C 会各回一条；
- **图片**：连发中带的图会用原组件接进 owner 的消息链，识图照常生效；
- **历史**：被合并的那几条不落库，只有合并后的一条入库；
- **不送判官、不加任何"要不要回"的判定、0 token 额外开销**。

## 指令不会冲突

| 检查 | 结果 |
|---|---|
| 等待窗口内发 `/记忆 清理`、`／切换` | 不并入、不被本插件 `stop_event`，指令链路照常 |
| 首条就是指令 | 直接放行，0.000s，不等待 |
| 合并结果会不会冒充指令 | 不会：owner 必须是普通消息，合并文本首字符不是 `/`；AstrBot 的指令路由也在改文本之前就定了 |
| 合并后 message_str 多行 | 只改 owner 事件，不动 `raw_message`，basicZ 的斜杠/注入检查仍看首行 |

已知边界：等待窗口里发指令，指令会立刻回复，聊天回复仍在 `settle` 后回来，
短时间内可能看到两条（一条指令结果 + 一条聊天回复）。

## 配置

`data/private_settle.json`（按 mtime 缓存，改完下一条私聊生效）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `merge` | `true` | `false` 退回旧行为：前面让位，只回最后一条 |
| `settle` | `5.0` | 安静多少秒算说完；`0` = 不等待 |
| `merge_window` | `20.0` | 连续说话时的最长攒批时间（硬上限，到点必发） |
| `min_gap` | `10.0` | 两次回复之间至少隔多久；`0` = 不限 |
| `poll` | `0.2` | 轮询间隔 |
| `inflight_wait` | `45.0` | 上一条生成时的排队上限 |
| `whitelist_only` | `true` | 只对私聊白名单里的人生效 |
| `log` | `true` | 打 `[dm]` 日志 |

日志示例：

```
[dm] merged into #1 (3 msgs) | 我今天有点难受
[dm] quiet 5.0s/gap 10s done, reply, merged 2 (waited 12 / yielded 0) | 在吗
```

## 安装

1. 把本目录放到 `AstrBot/data/plugins/astrbot_plugin_private_settle/`；
2. 改 `main.py` 顶部的两个路径（`CONFIG_PATH`、`CMD_CONFIG`）为本机位置；
3. 重启 AstrBot（或只重启 AstrBot 进程，QQ 不需要重新扫码）。

依赖 AstrBot 4.29+ 的 `on_waiting_llm_request` 钩子；优先级用 `maxsize`，
抢在 basicZ 等闸门之前执行。

## 相关

- 完整机制、与判官的关系、验证记录：[QQNTbotBUTafter261101/docs/PRIVATE-CHAT.md](https://github.com/zincvolt/QQNTbotBUTafter261101/blob/main/docs/PRIVATE-CHAT.md)
- 整套 QQ 机器人方案主仓库：[zincvolt/QQNTbotBUTafter261101](https://github.com/zincvolt/QQNTbotBUTafter261101)

## 许可

AGPL-3.0，见 [LICENSE](LICENSE)。

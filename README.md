# astrbot_plugin_dsh_bridge

把 QQ 变成 **DeepSeek Harness（DSH）** 的远程入口：手机上发一句话，电脑上的 DSH 就帮你干活，结果以简报和文件的形式回到 QQ。

```
手机 QQ ──► 机器人 ──► AstrBot ──► 本机 dsh headless ──► 你的电脑
   ▲                                        │
   └────────── 简报（≤400 字）+ 产物文件 ◄────┘
```

## 特性

- **手机远程派活**：在 QQ 里发 `/dsh 任务`，DSH 在本机执行
- **多段收集**：任务可以分几条消息说，说 `/dsh send` 才提交，期间机器人不会插嘴
- **收附件**：直接把文件/图片发给机器人，DSH 能读到本地路径
- **回产物**：DSH 生成的文件自动作为 QQ 文件消息发回
- **三档权限**：只读 / 工作区写 / 完全访问，聊天里可切，上限可配
- **会话复用**：同一会话连续任务共享上下文，带 TTL，避免越聊越贵
- **零手改配置**：全部配置在 AstrBot 后台用表单填写，DSH 的 profile 由插件自动创建

## 环境要求

| 组件 | 要求 |
|---|---|
| AstrBot | ≥ 4.29.0（需要 `File` 消息组件与插件配置表单） |
| DeepSeek Harness | ≥ 0.2.1-rc.3，且能运行 `dsh headless` |
| NapCat / OneBot v11 | 4.x（AstrBot 的 aiocqhttp 适配器） |
| 操作系统 | Windows（当前仅实测 Windows；macOS/Linux 未验证） |
| 模型密钥 | 插件配置里填，或复用 AstrBot 已配的 DeepSeek provider |

## 安装

### 方式一：插件市场（推荐）

1. AstrBot 后台 → 插件市场 → 从 URL 安装
2. 粘贴本仓库的 Release zip 地址，或仓库地址
3. 启用插件

### 方式二：上传 zip

下载 `astrbot_plugin_dsh_bridge-vX.Y.Z.zip`，在插件市场里选「上传安装」。

### 方式三：手动

把本目录放到 `AstrBot/core/data/plugins/astrbot_plugin_dsh_bridge/`，重启 AstrBot。

## 三步配置

装好启用后，打开插件设置页，只需要填三处：

| 配置 | 填什么 |
|---|---|
| **允许的 QQ 号** | 你的 QQ 号，逗号分隔（如 `123456789`）。留空则谁都用不了 |
| **工作区目录** | DSH 干活的位置，留空则自动用 `用户目录/dsh-bot-workspace` |
| **DeepSeek API Key** | 可留空，插件会自动读 AstrBot 里已配置的 DeepSeek 密钥 |

保存后在 QQ 私聊机器人：

```
/dsh help
```

能看到帮助卡片就说明通了。

> DSH 侧的三个 profile（`bot` / `bot-ro` / `bot-full`）由插件首次启动时自动生成并校验，不需要手工创建。

## 命令

| 输入 | 含义 |
|---|---|
| `/dsh 内容` | 开始任务（可带第一段） |
| 直接发消息 / 发文件 | 追加片段或附件（此期间机器人**不回复**） |
| `/dsh send` | 提交执行 |
| `/dsh cancel` | 取消本次收集 |
| `/dsh status` | 查看状态、权限、会话 |
| `/dsh perm [ro\|ws\|full]` | 查看/切换权限（`full` 需 `confirm`） |
| `/dsh doctor` | 环境自检（CLI / 版本 / 工作区 / profile） |
| `/dsh init` | 补建缺失的 DSH profile |
| `/dsh reset` | 清空本会话的 DSH 记忆 |
| `/dsh help` | 用法 |

保留字：`send / cancel / status / help / reset / perm / doctor / init`。

## 权限三档

| 档位 | 命令 | 能力 |
|---|---|---|
| 只读 `ro` | `/dsh perm ro` | 只能读，写会被沙箱拒绝 |
| 工作区写 `ws`（默认） | `/dsh perm ws` | 只能写工作区目录 |
| 完全访问 `full` | `/dsh perm full confirm` | 全盘可写（需把「允许的最高档位」调到 full） |

安全建议：保持 `max_permission = ws`，确实需要时再放开。

## 工作目录

```
<工作区>/
├─ out/            DSH 产物
├─ inbox/<时间戳>/   QQ 发来的附件
└─ .dsh-bridge/    全文、运行状态
```

## 常见问题

| 现象 | 处理 |
|---|---|
| `/dsh` 没反应 | 先确认 QQ 号在白名单；群里需要开启「允许群聊」并 @ 机器人 |
| 回了「收到，开始执行」就没下文 | 任务超时或 CLI 挂了；发 `/dsh doctor` 看自检 |
| 发了消息机器人彻底不理人 | 可能在收集状态：发 `/dsh cancel` 退出 |
| `/dsh perm ro` 后还能写 | 切换权限后用的是该档位的新会话；若仍异常请发 `/dsh doctor` |
| 提示「配置的 dsh_cli_path 不存在」 | 把该配置清空即可自动探测 |
| 文件没发回来 | 检查是否在工作区内、是否超过「回传文件上限」 |

## 实现说明

- 任务通过 **stdin** 传给 `dsh`，避免 Windows 批处理在换行处截断参数
- 解析 `dsh --json` 事件流，取 `final` 文本与会话 id
- 会话按 `(会话, 权限)` 隔离：DSH 恢复会话会带回会话里的旧权限，必须分开
- 附件用 AstrBot 的 `File`/`Image` 组件下载到 `inbox/`，把本地路径写进任务
- 缺少 `--session-id` 或 `File` 组件时自动降级，不报错

## 许可证

MIT

# 安装指南（面向第一次用的人）

整个链路是：**手机 QQ → 机器人 → AstrBot（装了本插件）→ 你电脑上的 DeepSeek Harness（DSH）**。

## 第 0 步：前置检查

| 需要 | 说明 |
|---|---|
| DeepSeek Harness 桌面端 | 装好后命令行能跑 `dsh --version` |
| AstrBot | ≥ 4.29.0，已接上 NapCat（能收发 QQ 消息） |
| DeepSeek 密钥 | 已在 AstrBot 的「服务提供商」里配置 deepseek（插件会自动读取） |

```powershell
dsh --version
```

有版本号（如 `0.2.1-rc.3`）就说明通了。

## 第 1 步：安装插件

**从 URL 安装：**

```
https://github.com/NK666-rocket/astrbot_plugin_dsh_bridge/releases/latest/download/astrbot_plugin_dsh_bridge.zip
```

AstrBot 后台 → 插件市场 → 从 URL 安装 → 粘贴 → 启用。

也可以下载 zip 用「上传安装」。

## 第 2 步：拿到你的会话 ID

私聊机器人发：

```
/sid
```

会返回类似：

```
肥嘟嘟:FriendMessage:2809688590
```

**这一整行就是你的 UMO**，复制它。

> 群里用的话：在群里 @机器人 发 `/sid`，得到 `肥嘟嘟:GroupMessage:群号`。

## 第 3 步：配置（三处必填）

后台 → 插件 → DSH 桥接 → 配置：

| 分组 | 配置 | 填什么 |
|---|---|---|
| 💬 私聊 | 私聊白名单 | 刚才的 UMO 加一行 |
| 👑 管理员 | 管理员名单 | **同一条 UMO**（管理员才能切 full、改配置） |
| 👥 群聊 | 共用一个上下文的群 / 每人独立上下文的群 | 需要群聊就把群 UMO 加进其中一个（这两个列表同时是群白名单） |

其他保持默认即可。保存后**不用重启**。

## 第 4 步：验证

```
/dsh status
```

预期：

```
🪪 会话：肥嘟嘟:FriendMessage:2809688590
身份：管理员
权限：工作区写
💤 没有收集中的任务
```

再跑一个任务：

```
/dsh 在当前目录建 hello.txt，内容写 hi
```
```
/dsh send
```

几秒后收到 `✅ 完成`，工作区出现 `hello.txt`。

要测回传文件：

```
/dsh 给我发一份 test.txt，内容写 测试成功
```
+ `/dsh send` → 应该直接收到 test.txt 文件。

## 第 5 步（可选）：体检

```
/dsh doctor
```

逐项检查 CLI、版本、工作区、profile、密钥来源。

## 常用操作

| 场景 | 命令 |
|---|---|
| 切成只读 | `/dsh perm ro` |
| 切回工作区写 | `/dsh perm ws` |
| 完全访问（管理员） | `/dsh perm full confirm` |
| 群里临时全权（管理员，仅下一条） | `/dsh perm full confirm` |
| 改白名单（管理员） | `/dsh set private_whitelist 肥嘟嘟:FriendMessage:123 confirm` |
| 恢复某项后台值 | `/dsh set private_whitelist reset confirm` |

## 常见卡点

| 现象 | 原因 | 处理 |
|---|---|---|
| `/dsh` 完全没反应 | UMO 没加进白名单 | 发 `/sid` 复制整行加进去 |
| 群里没反应 | 群没加进两个上下文列表之一，或没开「允许群聊」 | 两个列表之一加群 UMO + 开开关 + @机器人 |
| 群里普通成员切权限被拒 | 设计如此 | 只有管理员能切 |
| 提示找不到 CLI | 不在 PATH | 配置里填 `dsh_cli_path` |
| 文件没回传 | 超出上限或不在工作区 | 看日志 `回传文件：N 个` |
| AstrBot 全局白名单拦截 | AstrBot 自己也有白名单 | 两处都要加 |

## 卸载

插件市场卸载。DSH 侧 profile 留在 `~/.dsh/profiles/` 下（`bot`、`bot-ro`、`bot-full`），需要时手动删。

# 安装指南（面向第一次用的人）

整个链路是：**手机 QQ → 机器人 → AstrBot（装了本插件）→ 你电脑上的 DeepSeek Harness（DSH）**。

所以你需要先有 DSH，再装插件。

## 第 0 步：确认前置

| 需要 | 说明 |
|---|---|
| DeepSeek Harness 桌面端 | 官方安装包，装好后命令行能跑 `dsh --version` |
| AstrBot | ≥ 4.29.0，且已经接上 NapCat（能收发 QQ 消息） |
| 一个模型 Key | DeepSeek API Key，或 AstrBot 里已经配好的 DeepSeek provider |

验证 DSH 是否可用：打开 PowerShell，输入

```powershell
dsh --version
```

有版本号（例如 `0.2.1-rc.3`）就说明通了。没有的话，先在插件设置里把 **DSH CLI 路径** 填成完整的 `dsh.cmd` 路径。

## 第 1 步：安装插件

**方式 A：从 URL 安装（推荐）**

1. AstrBot 后台 → 插件市场 → 从 URL 安装
2. 粘贴：

```
https://github.com/NK666-rocket/astrbot_plugin_dsh_bridge/releases/latest/download/astrbot_plugin_dsh_bridge.zip
```

3. 等安装完成 → 启用

**方式 B：上传 zip**

在插件市场选「上传安装」，选下载好的 zip。

## 第 2 步：填三项配置

后台 → 插件 → DSH 桥接 → 配置：

| 配置 | 填什么 | 例子 |
|---|---|---|
| ① 允许的 QQ 号 | 你自己的 QQ 号 | `123456789` |
| ③ 工作区目录 | 让它干活的位置（可留空） | `D:\dsh-ws` |
| ③ DeepSeek API Key | 可留空（自动读 AstrBot 的配置） | `sk-...` |

保存后**不需要重启**。

> 插件第一次启动会自动创建 DSH 的 `bot` / `bot-ro` / `bot-full` 三个 profile，日志里会看到 `校验通过`。

## 第 3 步：验证

在 QQ 私聊机器人：

```
/dsh help
```

收到帮助卡片 → 通了。

再试一个真实任务：

```
/dsh 在当前目录建一个 hello.txt，内容写 hi
```
再发：
```
/dsh send
```

几秒后会收到 `✅ 完成`，并且工作区里出现 `hello.txt`。

## 第 4 步（可选）：体检

```
/dsh doctor
```

会逐项检查：CLI、版本、session 支持、工作区可写、API Key、三个 profile。
哪项是 ❌，按提示处理即可。

## 常见卡点

| 现象 | 原因 | 处理 |
|---|---|---|
| `/dsh` 完全没反应 | QQ 号不在白名单 | 配置里填对 QQ 号；群里还要开「允许群聊」并 @ 机器人 |
| 提示找不到 dsh CLI | 不在 PATH，也不是默认安装位置 | 配置里填 `dsh_cli_path` 的完整路径 |
| 回「收到，开始执行」后没下文 | 任务超时 / CLI 崩溃 | `/dsh doctor` 看自检，或看 AstrBot 日志 |
| 机器人突然不理人 | 可能在多段收集状态 | 发 `/dsh cancel` 退出 |
| 写文件被拒绝 | 当前权限是只读 | `/dsh perm ws` 切到工作区写 |
| 文件没回传 | 超出大小/数量限制，或不在工作区 | 看简报里的提示 |

## 卸载

插件市场 → 卸载。DSH 侧的三个 profile 会留在 `~/.dsh/profiles/`，需要的话手动删 `bot*` 目录。

# 小软 · MaiBot 配置与人格

一套可以直接复现的 MaiBot 实例配置，外加「小软」这个角色的人格与身份设定。用于多人协作调教同一个 QQ 机器人。

## 目录结构

```
config/
  bot_config.example.toml     # 完整实例配置（人格、聊天行为、回复风格、长期记忆开关等）
  model_config.toml           # 任务 → 模型的分工（不含任何密钥）
host/
  model_catalog.example.toml  # 服务商与模型定义（api_key 已置空，需自己填）
plugins/
  snowluma-adapter.config.example.toml  # OneBot 适配器连接配置
napcat/
  onebot11.example.json       # NapCat 正向 WebSocket 服务配置（127.0.0.1:3001）
persona/
  xiaoruan-original.md        # 角色卡原文（源文件，未删改）
  identity-fields.md          # 精简后实际生效的三段字段
```

## 快速开始

```powershell
# 1. 装主程序（需要 uv）
uv tool install --python 3.13 maibot

# 2. 建实例
New-Item -ItemType Directory 我的麦麦 ; cd 我的麦麦
maibot init

# 3. 覆盖配置
Copy-Item ..\config\bot_config.example.toml .\config\bot_config.toml -Force
Copy-Item ..\config\model_config.toml      .\config\model_config.toml -Force
Copy-Item ..\host\model_catalog.example.toml .\host\model_catalog.toml -Force

# 4. 填自己的信息
#   host/model_catalog.toml        -> api_providers[0].api_key
#   config/bot_config.toml         -> [bot] qq_account / nickname / alias_names

# 5. 启动（首次会提示同意 EULA）
maibot run
```

WebUI 默认在 http://127.0.0.1:8001/，首次登录 Token 在 `host/webui.json`。

## 接 QQ（OneBot）

```powershell
git clone https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter.git plugins/MaiBot-SnowLuma-Adapter
Copy-Item ..\plugins\snowluma-adapter.config.example.toml .\plugins\MaiBot-SnowLuma-Adapter\config.toml
```

再用 NapCat 登录机器人小号，把 `napcat/onebot11.example.json` 放到 NapCat 的 `config/onebot11_<机器人QQ>.json`，开启 3001 端口正向 WebSocket。

> 搭建 QQ 机器人有被风控/封禁的风险，务必只用自己的小号。

## 协作约定

1. **人格改动以 `persona/identity-fields.md` 为准**，改完再同步进 `config/bot_config.example.toml` 的 `[personality]` 段，避免两处各写一份。
2. 改聊天行为（回复频率、引用、上下文条数）直接改 `bot_config.example.toml`，在 PR 里说明改之前和改之后的差别、为什么改。
3. 每项改动最好附一次真实对话作为验证证据，不要只凭"感觉更像了"。
4. 模型相关只说"用哪个任务、要什么能力"，不要把个人 Key 提进仓库。

## 安全须知

仓库里**不能出现**这些东西，`scripts/scan-secrets.ps1` 会在提交前扫描：

- API Key / access token（`sk-` 开头的、各家 Key 字段）
- WebUI 登录 Token（`host/webui.json`）
- 真实 QQ 号、群号（本仓库已用 `YOUR_BOT_QQ` 占位）
- `data/`、`logs/`、`temp/` 整个目录（含聊天记录、数据库、日志）

## 命名一致性

`[bot] nickname` 和 `[personality] personality` 里的自称必须一致。MaiBot 拼系统提示时是 `你的名字是{昵称}。{人格设定}`，两者不一致会让模型每轮都收到两个名字，表现成"一会儿叫这个名字一会儿叫那个"。

本仓库统一用「小软」，`alias_names` 放别人可能用的称呼（软软 / 软姐 / 小软同学）。换机器人号时只改 `qq_account`，不要动这两处。

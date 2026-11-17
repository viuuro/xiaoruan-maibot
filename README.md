# 小软 · MaiBot 配置与人格

一套可以直接复现的 MaiBot 实例配置，外加「小软」这个角色的人格与身份设定。用于多人协作调教同一个 QQ 机器人。

当前状态：**已跑通完整链路** —— QQ 消息经 SnowLuma 接入、由 DeepSeek 驱动对话、带长期记忆与表情包语义匹配。

---

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
deploy/                       # 给管理员的一键部署包（插件 + 提示词 + 脚本）
MEMORY-POLICY.md              # 长期记忆入库规范
```

---

## 技术栈

| 层次 | 选型 | 说明 |
|---|---|---|
| 主程序 | **MaiBot 1.4.5** | Python 3.12+，负责对话编排、记忆、插件运行时 |
| QQ 接入 | **SnowLuma 2.4.2** | Node.js 24.22.1+ 的 QQ 客户端互操作运行时，输出 OneBot v11 |
| 协议 | **OneBot v11** | 正向 WebSocket，`127.0.0.1:3001` |
| 对话模型 | **DeepSeek** `deepseek-v4-flash` | Planner / Replyer / 表情打标等任务 |
| 视觉模型 | **DeepSeek** `deepseek-flash-vision` | 图片理解、GIF 动图识别 |
| 文本嵌入 | **阿里云百炼** `text-embedding-v4` | 1024 维，用于表情包语义匹配与记忆向量化 |
| 图片嵌入 | **阿里云百炼** `multimodal-embedding-v1` | 1024 维，多模态嵌入，用于图片记忆 |
| 长期记忆 | **A_Memorix** | MaiBot 内置，SQLite + FTS5 + 向量双通道 |

### 备用接入路线

NapCat + `maibot-team.napcat-adapter` 同样可用，配置文件在 `napcat/` 下。两条路线共用 3001 端口的 OneBot 协议，切换只需换协议端和适配器插件。

---

## 插件清单

### 核心接入

| 插件 | 作用 |
|---|---|
| `maibot-team.snowluma-adapter` | OneBot v11 适配器，连接 SnowLuma / NapCat。连接后自动识别对端类型并应用对应能力画像 |

### 本次新增

| 插件 | 作用 | 状态 |
|---|---|---|
| `TAIY2020.smart_poke_plugin` | 智能戳一戳：被拍时按场景决定要不要回应、用文字还是表情，带限频与反刷屏 | 运行中 |
| `WiseXin.character-disguise` | 去 AI 味：去句号、去 Markdown、限长、清理 AI 套路句式 | 运行中，群聊限 30 字 |
| `hsd221.emoji-text-selector` | 表情包文案选择，配合语义匹配挑选合适的图 | 运行中 |
| `github.fabiocsa.read-gif` | GIF 动图识别，把动图拆帧交给视觉模型描述 | 运行中 |
| `xxxxx7258.google-search-plugin` | 联网搜索，让小软能查资料再回答 | 运行中 |

### 语音（尚未启用）

| 插件 | 状态 |
|---|---|
| `Emilia-awa.maibot-mimotts-voice` | 已安装、已加载，但**音色克隆尚未着手准备**：缺 MiMo API Key，`voices/` 目录为空 |
| `github.akenari011.gpt-sovits-voice-plugin` | 已停用。需要额外部署 GPT-SoVITS 服务并提供参考音频 |

> 语音相关插件目前只是占位，不影响主链路。真要启用需要单独准备音色素材与服务。

### 已停用的适配器

`Mai-with-u.qq-adapter`（QQ 官方开放平台）、`maibot-team.napcat-adapter`、`maibot-team.telegram-adapter`、`maibot-team.weixin-clawbot-adapter` —— 保持停用，避免多路适配器同时抢消息。

---

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
#   host/model_catalog.toml        -> api_providers 里的 api_key（对话/嵌入可能来自不同厂商）
#   config/bot_config.toml         -> [bot] qq_account / nickname / alias_names

# 5. 启动（首次会提示同意 EULA）
maibot run
```

WebUI 默认在 http://127.0.0.1:8001/，首次登录 Token 在 `host/webui.json`。

---

## 接 QQ（OneBot）

### 路线 A：SnowLuma（当前使用）

1. 从 [SnowLuma Releases](https://github.com/TomyJan/SnowLuma/releases) 下载对应平台发行包
2. 运行 `launcher.bat`，WebUI 在 http://127.0.0.1:5099
3. 首次启动需要同意 EULA / 隐私协议、修改初始密码
4. 挂载已登录的 QQ 进程
5. 在 OneBot 配置页确认 `wsServers` 为 `127.0.0.1:3001`，记下 `accessToken`
6. 把该 token 填进适配器配置的 `[client] token`

> Lite 版需要 Node.js 24.22.1+；完整版自带运行时。

### 路线 B：NapCat

```powershell
git clone https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter.git plugins/MaiBot-SnowLuma-Adapter
Copy-Item ..\plugins\snowluma-adapter.config.example.toml .\plugins\MaiBot-SnowLuma-Adapter\config.toml
```

用 NapCat 登录机器人小号，把 `napcat/onebot11.example.json` 放到 NapCat 的 `config/onebot11_<机器人QQ>.json`，开启 3001 端口正向 WebSocket。

### 两条路线的共同点

- **`accessToken` 两边必须一致。** 对端生成了 token 就必须填进 `[client] token`，否则会一直 `HTTP 401 鉴权失败`。
- **QQ 号填进 `[bot] qq_account`。** 留占位符会让 WebUI 聊天页报 `ValueError`。
- 搭建 QQ 机器人有被风控/封禁的风险，**务必只用自己的小号**。

---

## 部署到服务器

完整操作手册见 [`使用说明.md`](使用说明.md)。快速上手：

```bash
git clone git@github.com:viuuro/xiaoruan-maibot.git
cd xiaoruan-maibot/deploy

sudo bash deploy.sh --dry-run   # 先看会做什么
sudo bash deploy.sh             # 部署，插件停用，提示词不动
sudo bash deploy.sh --enable    # 部署并启用
```

细节见 [`deploy/README.md`](deploy/README.md)。要点：

- 插件装到 `/opt/xiaoruan/plugins/`
- **不碰 `config/bot_config.toml`** —— WebUI 里调的人格不会被覆盖
- 记忆规范提示词需显式 `--with-prompts`，已有自定义提示词时拒绝覆盖
- 清掉 `model_config.toml` 里的 qwen（决策专用，不参与生成）
- 自动从 `host/model_catalog.toml` 取 Key 填进插件
- 全部改动前先备份到 `/opt/xiaoruan/archive/`

> ⚠️ 服务器上的人格（WebUI 里改的）是当前实际生效版本，仓库的 `persona/identity-fields.md` 已落后。要同步请从服务器导回仓库，不要反过来覆盖。

## 安全性规范

提示词与命令层面的改动统一收敛为「安全性规范」，分两块：

**1. 记忆入库规范**（详见 [`MEMORY-POLICY.md`](MEMORY-POLICY.md)）

长期记忆不是聊天记录的备份，只保留对以后对话有用、且不伤害任何人的事实。绝对不入库的内容包括：他人隐私、国家政治、血腥暴力、违法违规、色情低俗；过度玩笑、对具体人的贬损与恶搞丑化图片；群友之间的引战与骂战。拿不准时默认不入库，但**不入库不等于不回答**。

规范落在两条链路上：聊天摘要提取器的提示词（中英日三语同步）、人物事实提取器的内联提示词。

**2. 表达约束**

- 群聊不贴系统内部信息（版本号、运行状态、端口、日志、完整报错堆栈）
- 协议端内置的状态命令（如 `#sl`）已关闭，避免以机器人身份直接把后台信息发进群

> 这类约束是提示词级别的，属尽力而为，不是硬性过滤器。定期抽查记忆库仍是必要的。

---

## 协作约定

1. **人格改动以 `persona/identity-fields.md` 为准**，改完再同步进 `config/bot_config.example.toml` 的 `[personality]` 段，避免两处各写一份。
2. 改聊天行为（回复频率、引用、上下文条数）直接改 `bot_config.example.toml`，在 PR 里说明改之前和改之后的差别、为什么改。
3. 每项改动最好附一次真实对话作为验证证据，不要只凭"感觉更像了"。
4. 模型相关只说"用哪个任务、要什么能力"，不要把个人 Key 提进仓库。

### 改人格的硬性要求

只**追加条件分支**，不改动原有文字。追加前先确认：

- 别人的说话方式不会被顺手改掉
- `personality`（同时影响"想不想回"和"怎么回"的字段）尽量不动
- 改完跟备份逐字比对，确认是纯追加而非重写

---

## 安全须知

仓库里**不能出现**这些东西，`scripts/scan-secrets.ps1` 会在提交前扫描：

- API Key / access token（`sk-` 开头的、各家 Key 字段）
- WebUI 登录 Token（`host/webui.json`）
- 真实 QQ 号、群号（本仓库已用 `YOUR_BOT_QQ` 占位）
- `data/`、`logs/`、`temp/` 整个目录（含聊天记录、数据库、日志）
- 改动前的本地备份（`*.bak-*` 已加入 `.gitignore`）

提交前跑一次：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ./scripts/scan-secrets.ps1
```

---

## 命名一致性

`[bot] nickname` 和 `[personality] personality` 里的自称必须一致。MaiBot 拼系统提示时是 `你的名字是{昵称}。{人格设定}`，两者不一致会让模型每轮都收到两个名字，表现成"一会儿叫这个名字一会儿叫那个"。

本仓库统一用「小软」，`alias_names` 放别人可能用的称呼（软软 / 软姐 / 小软同学）。换机器人号时只改 `qq_account`，不要动这两处。

---

## 已知限制

- **图片记忆没有内容审核**：图片链路位于 `src/A_memorix/core/`，属上游 `A-Dawn/A_memorix` 的实现层，行为变更应先改上游。目前只有"图片描述进摘要时会过安全性提示词"这一层间接保护。
- **记忆规范是提示词约束**：大模型绝大多数情况会遵守，但不保证 100%。需要定期抽查并按 `MEMORY-POLICY.md` 第十章清理。
- **语音链路未启用**：见上文插件清单。

# 小软 · 部署指引

给有 root 权限的管理员用。目标是把本仓库里准备好的东西装到服务器实例上。

## 前提

服务器上的小软实例位于 `/opt/xiaoruan`，由 systemd 服务 `maibot.service` 以 root 运行，MaiBot 本体通过 `uv tool install maibot` 安装。

本目录提供的是**实例层**的东西：插件、提示词覆盖、配置修正。不改动 MaiBot 安装包。

## 快速开始

```bash
git clone git@github.com:viuuro/xiaoruan-maibot.git
cd xiaoruan-maibot/deploy

# 先看会做什么，不改文件
sudo bash deploy.sh --dry-run

# 正式部署（插件保持停用）
sudo bash deploy.sh

# 部署并启用
sudo bash deploy.sh --enable
```

## 脚本做四件事

| 步骤 | 内容 | 备份 |
|---|---|---|
| 1 | 安装 `xiaoruan-jev-adapter` 插件到 `/opt/xiaoruan/plugins/` | 旧版本 |
| 2 | 记忆入库规范作为自定义提示词覆盖到 `/opt/xiaoruan/data/custom_prompts/` | 旧覆盖 |
| 3 | 从 `model_config.toml` 清掉 qwen 模型 | 原文件 |
| 4 | 从 `host/model_catalog.toml` 取阿里云 Key 填进插件决策后端 | — |

全部备份到 `/opt/xiaoruan/archive/deploy-<时间戳>/`。脚本幂等，可重复执行。

### 为什么第 2 步走 custom_prompts

MaiBot 的提示词解析优先级是 **自定义 → 内置**（`src/common/prompt_i18n.py` 的 `resolve_prompt_path`）。所以把文件放到实例的 `data/custom_prompts/<locale>/main/` 就能覆盖安装包里的版本，不用动 `/root/.local/share/uv/tools/maibot/` 下的任何文件。

### 为什么第 3 步要清 qwen

qwen 只用于决策判定，不参与生成场景。早期 `fast_model` 和 `emoji` 两个任务混用过 `qwen3.8-omni-flash`，会让决策模型和生成模型混在一起，任何一方调整都会波及另一方。

脚本只会删掉含 `qwen` 的条目，其他模型原样保留；删空的列表回落到 `deepseek-v4-flash`。

## 还需要手工做的一步

### 人物事实提取器的提示词

`deploy/patches/memory_flow_service.patch` 是对 MaiBot 安装包内 `src/services/memory_flow_service.py` 的改动，给「人物事实提取」加了三条排除规则。

它**不在实例层**，所以脚本不自动打。两种处理方式：

**方式 A（推荐）：跳过**

记忆规范的主路径（聊天摘要）已经通过 custom_prompts 覆盖了。人物事实这条链路漏掉规则的影响面较小，可以先不管。

**方式 B：打补丁**

```bash
# 找到安装包里的文件
PKG=$(python3 -c "import src, pathlib; print(pathlib.Path(src.__file__).parent)" 2>/dev/null || echo "")

# 如果上面为空，用这个：
PKG=/root/.local/share/uv/tools/maibot/lib/python3.13/site-packages/src

cd "$PKG/.."
cp services/memory_flow_service.py services/memory_flow_service.py.bak
patch -p1 < /path/to/deploy/patches/memory_flow_service.patch
```

注意：**升级 MaiBot 会覆盖这个改动**，需要重新打。

## 灰度建议

不要一次全量放开。先在插件的 `config.toml` 里限定一个群：

```toml
[speak]
only_sessions = ["目标群的session_id"]
```

`session_id` 从日志里找（形如 `session=xxxx`）。观察几天确认：

- 日志里 `Jev 决策适配已加载：后端=logprob(...)` 出现
- 不该说话的时候确实没说话
- 回复延迟没有明显上升
- Replyer 缓存命中率没掉到 30% 以下

## 验证

```bash
# 服务状态
systemctl status maibot.service

# 看插件是否加载
journalctl -u maibot -n 200 | grep -i "Jev 决策适配"

# 看决策是否生效
journalctl -u maibot -n 200 | grep -iE "发言|决策"
```

WebUI 里也有两个接口可以手动验证：

- `decide_speak` —— 贴一段聊天记录，跑一次决策
- `test_backend` —— 检查后端配置和 Key 是否就位

## 回滚

```bash
# 停用插件就够了，不用回滚文件
sudo bash deploy.sh           # 重新部署时保持停用

# 或者从备份恢复
cp -a /opt/xiaoruan/archive/deploy-<时间戳>/xiaoruan-jev-adapter.old \
      /opt/xiaoruan/plugins/xiaoruan-jev-adapter
systemctl restart maibot.service
```

自定义提示词同理，备份在 `custom_prompts.old`。

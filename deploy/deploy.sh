#!/bin/bash
# 小软部署脚本 —— 必须以 root 运行
#
# 用法：
#   sudo bash deploy.sh              # 只部署，插件保持停用
#   sudo bash deploy.sh --enable     # 部署并启用插件
#   sudo bash deploy.sh --dry-run    # 只看会做什么，不改任何文件
#
# 做的事：
#   1. 安装 xiaoruan-jev-adapter 插件到 /opt/xiaoruan/plugins/
#   2. 把记忆入库规范作为自定义提示词覆盖到 /opt/xiaoruan/data/custom_prompts/
#   3. 从 model_config.toml 里清掉 qwen（决策专用模型不该参与生成场景）
#   4. 从 host/model_catalog.toml 取阿里云 Key，填进插件的决策后端配置
#
# 幂等：可重复执行。所有改动前都会先备份到 /opt/xiaoruan/archive/deploy-<时间戳>/

set -euo pipefail

INSTANCE="${XIAORUAN_INSTANCE:-/opt/xiaoruan}"
PAYLOAD="$(cd "$(dirname "$0")/payload" && pwd)"
STAMP="$(date +%Y%m%d%H%M%S)"
BACKUP="$INSTANCE/archive/deploy-$STAMP"

DRY_RUN=0
ENABLE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --enable)  ENABLE=1 ;;
        *) echo "未知参数：$arg" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    echo "错误：必须以 root 运行（sudo bash $0）" >&2
    echo "提示：可以先加 --dry-run 看看会做什么，那个不需要 root。" >&2
    exit 1
fi

if [ ! -d "$INSTANCE" ]; then
    echo "错误：找不到实例目录 $INSTANCE" >&2
    exit 1
fi

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "    [dry-run] $*"
    else
        "$@"
    fi
}

# 找一个装了 tomlkit 的 python。
# 服务器系统 python3 是 3.10 且没装 tomlkit，但 MaiBot 自己的 venv 里有。
# 本脚本以 root 运行时可以访问 /root 下的 venv。
find_python() {
    for candidate in "${MAIBOT_PYTHON:-}" "python3" "/root/.local/share/uv/tools/maibot/bin/python"; do
        [ -z "$candidate" ] && continue
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import tomlkit" >/dev/null 2>&1; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
    echo "警告：找不到装了 tomlkit 的 python，第 3、4 步会被跳过。" >&2
    echo "      可以设置 MAIBOT_PYTHON 指向一个可用的解释器。" >&2
fi

echo "=== 部署开始 ==="
echo "  实例目录：$INSTANCE"
echo "  载荷目录：$PAYLOAD"
if [ "$DRY_RUN" -eq 1 ]; then
    echo "  模式：DRY RUN（不会修改任何文件）"
else
    echo "  备份位置：$BACKUP"
    mkdir -p "$BACKUP"
fi

# ---------- 1. 插件 ----------
echo
echo "[1/4] 安装插件"
PLUGIN_DIR="$INSTANCE/plugins/xiaoruan-jev-adapter"
if [ -d "$PLUGIN_DIR" ]; then
    echo "  发现已存在的插件，先备份"
    run cp -a "$PLUGIN_DIR" "$BACKUP/xiaoruan-jev-adapter.old"
fi
run mkdir -p "$INSTANCE/plugins"
run cp -a "$PAYLOAD/plugins/xiaoruan-jev-adapter" "$INSTANCE/plugins/"
run rm -rf "$PLUGIN_DIR/__pycache__"
run chown -R root:root "$PLUGIN_DIR"
echo "  已安装到 $PLUGIN_DIR"

# ---------- 2. 自定义提示词 ----------
echo
echo "[2/4] 覆盖记忆规范提示词"
CUSTOM="$INSTANCE/data/custom_prompts"
if [ -d "$CUSTOM" ] && [ -n "$(find "$CUSTOM" -type f 2>/dev/null | head -1)" ]; then
    echo "  已有自定义提示词，先备份"
    run cp -a "$CUSTOM" "$BACKUP/custom_prompts.old"
fi
for locale in zh-CN en-US ja-JP; do
    src="$PAYLOAD/prompts/$locale/main/a_memorix_chat_summary.prompt"
    if [ -f "$src" ]; then
        run mkdir -p "$CUSTOM/$locale/main"
        run cp -a "$src" "$CUSTOM/$locale/main/"
        run chown -R root:root "$CUSTOM/$locale"
        echo "  $locale ✓"
    fi
done

# ---------- 3. 清理 model_config 里的 qwen ----------
echo
echo "[3/4] 从 model_config.toml 清理 qwen"
MODEL_CONFIG="$INSTANCE/config/model_config.toml"
if [ -f "$MODEL_CONFIG" ]; then
    run cp -a "$MODEL_CONFIG" "$BACKUP/model_config.toml.old"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "    [dry-run] 将检查并移除各任务里的 qwen 模型"
    else
        "$PYTHON" - "$MODEL_CONFIG" <<'PYEOF'
import sys
try:
    import tomlkit
except ImportError:
    print("  警告：缺 tomlkit，跳过。请手动把 qwen 从各任务 model_list 里删掉。")
    sys.exit(0)

path = sys.argv[1]
doc = tomlkit.parse(open(path, encoding="utf-8").read())
fallback = "deepseek-v4-flash"
changed = []
for name, cfg in doc.get("model_task_config", {}).items():
    models = [str(m) for m in (cfg.get("model_list") or [])]
    kept = [m for m in models if "qwen" not in m.lower()]
    if kept != models:
        cfg["model_list"] = kept if kept else [fallback]
        changed.append((name, models, list(cfg["model_list"])))
if changed:
    open(path, "w", encoding="utf-8").write(tomlkit.dumps(doc))
    for name, before, after in changed:
        print(f"  {name}: {before} -> {after}")
else:
    print("  无需清理")
PYEOF
    fi
else
    echo "  找不到 model_config.toml，跳过"
fi

# ---------- 4. 决策后端 Key ----------
echo
echo "[4/4] 配置决策后端 Key"
PLUGIN_CONFIG="$PLUGIN_DIR/config.toml"
cat > /tmp/.xiaoruan_config_bootstrap.py <<'PYEOF'
import sys

plugin_path, example_path, catalog_path = sys.argv[1], sys.argv[2], sys.argv[3]

try:
    import tomlkit
except ImportError:
    print("  警告：缺 tomlkit，跳过。请手动从 config.toml.example 复制配置。")
    sys.exit(0)

import os

# 配置不存在时从 example 复制
if not os.path.exists(plugin_path):
    with open(example_path, encoding="utf-8") as src, open(plugin_path, "w", encoding="utf-8") as dst:
        dst.write(src.read())
    print("  已从 config.toml.example 生成 config.toml")

doc = tomlkit.parse(open(plugin_path, encoding="utf-8").read())
logprob = doc["backend"]["logprob"]

if str(logprob.get("api_key", "")).strip():
    print("  config.toml 已有 api_key，跳过（不覆盖）")
    sys.exit(0)

key = ""
try:
    catalog = tomlkit.parse(open(catalog_path, encoding="utf-8").read())
    for provider in catalog.get("api_providers", []):
        if str(provider.get("name")) == "Alibaba":
            key = str(provider.get("api_key", "")).strip()
            break
except FileNotFoundError:
    pass

if not key:
    print(f"  model_catalog 里没找到 Alibaba 的 key")
    print(f"  请手动填 {plugin_path} 的 [backend.logprob] api_key")
    sys.exit(0)

logprob["api_key"] = key
open(plugin_path, "w", encoding="utf-8").write(tomlkit.dumps(doc))
print(f"  已从 model_catalog 复制 Alibaba Key（长度 {len(key)}，不显示内容）")
PYEOF
if [ "$DRY_RUN" -eq 1 ]; then
    echo "    [dry-run] 将生成 config.toml 并从 model_catalog 填充 Alibaba Key"
else
    "$PYTHON" /tmp/.xiaoruan_config_bootstrap.py \
        "$PLUGIN_CONFIG" \
        "$PLUGIN_DIR/config.toml.example" \
        "$INSTANCE/host/model_catalog.toml"
    rm -f /tmp/.xiaoruan_config_bootstrap.py
    chown root:root "$PLUGIN_CONFIG" 2>/dev/null || true
    chmod 600 "$PLUGIN_CONFIG" 2>/dev/null || true
fi

# ---------- 可选：启用 ----------
if [ "$ENABLE" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
    echo
    echo "[+] 启用插件"
    "$PYTHON" - "$PLUGIN_CONFIG" <<'PYEOF'
import sys
import tomlkit
path = sys.argv[1]
doc = tomlkit.parse(open(path, encoding="utf-8").read())
doc["plugin"]["enabled"] = True
open(path, "w", encoding="utf-8").write(tomlkit.dumps(doc))
print("  [plugin] enabled = true")
PYEOF
fi

# ---------- 完成 ----------
echo
echo "=== 部署完成 ==="
if [ "$DRY_RUN" -eq 0 ]; then
    echo "  备份：$BACKUP"
fi
echo
if [ "$ENABLE" -eq 0 ]; then
    echo "插件当前是停用状态。启用方法："
    echo "  sudo bash deploy.sh --enable"
    echo "  或手动把 $PLUGIN_CONFIG 里的 [plugin] enabled 改成 true"
    echo
fi
echo "建议先灰度：编辑 $PLUGIN_CONFIG"
echo "  在 [speak] only_sessions 里填一个群的 session_id，只在那一个群生效"
echo "  （session_id 可以从日志里找，形如 session=xxxx）"
echo
echo "然后重启服务："
echo "  systemctl restart maibot.service"
echo
echo "验证：看日志里是否出现"
echo "  Jev 决策适配已加载：后端=logprob(...)"

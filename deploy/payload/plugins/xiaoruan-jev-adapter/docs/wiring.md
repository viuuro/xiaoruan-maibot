# Jev 判定 → 注入 Replyer → LLM 生成：接线设计

> 本文由项目答疑子代理核实源码后整理，2026-11-17。
> 所有结论均附文件路径与行号，可复查。

## 一、插入层：必须用 Hook，不能用 @Tool

| 维度 | `@Tool` | Hook |
|---|---|---|
| 触发 | Planner **自己决定**要不要调 | 每次请求**必然触发** |
| 可靠性 | 取决于模型有没有想到 | 100% 执行 |
| 位置 | Planner 思考中途（占一轮 tool call） | 请求发出之前 |
| 能否阻断消息 | 不能 | `chat.receive.after_process` 可以 |
| 失败后果 | 模型可能无视返回结果 | `error_policy=SKIP`，静默跳过、走原路径 |

**结论**：判定是「每条消息都要做」的动作，不能寄望模型每轮想起来调工具。Tool 只适合当辅助查询，不适合当闸门。

## 二、可用的 Hook 扩展点

`plugins/WiseXin_character-disguise/plugin.py` 一共挂了 6 个，可作范本：

| # | Hook 名 | 行号 | 签名 | 模式 |
|---|---|---|---|---|
| 1 | `chat.receive.after_process` | 186 | `(message=None, **kwargs)` | BLOCKING / EARLY / SKIP |
| 2 | `maisaka.planner.before_request` | 257 | `(**kwargs)` | BLOCKING / NORMAL / SKIP |
| 3 | `maisaka.replyer.before_request` | 305 | `(**kwargs)` | BLOCKING / NORMAL / SKIP |
| 4 | `maisaka.replyer.before_model_request` | 338 | `(**kwargs)` | BLOCKING / NORMAL / SKIP |
| 5 | `maisaka.replyer.after_response` | 387 | `(**kwargs)` | BLOCKING / NORMAL / SKIP |
| 6 | `send_service.before_send` | 445 | `(**kwargs)` | BLOCKING / LATE / SKIP |

### Hook 1 能拿到的字段（plugin.py:194-251）

`message` 字典含：

- `session_id`（或 `message_info.additional_config.session_id`）
- `message_info.user_info.user_id`
- `message_info.group_info.group_id`
- `platform`
- `processed_plain_text`
- `raw_message`

硬阻断方式（plugin.py:250）：

```python
return {"action": "abort"}
```

## 三、能改 Planner 到什么程度

| 手段 | Hook | 效果 | 证据 |
|---|---|---|---|
| 硬阻断 | `chat.receive.after_process` → `{"action":"abort"}` | 消息根本不进流水线 | plugin.py:250 |
| 软约束 | `maisaka.planner.before_request` → 改 `items` | 影响怎么想，但它仍自主 | chat_loop_service.py:1258-1280 |
| 改工具集 | 同上，改 `tool_definitions` | 增删本轮可用工具 | chat_loop_service.py:1288-1295 |
| **强制回复** | —— | **不存在** | 无此返回值 |

Planner Hook 完整载荷（chat_loop_service.py:1258-1267）：

```python
invoke_hook(
    "maisaka.planner.before_request",
    items=deepcopy(serialized_items),
    item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION,
    tool_definitions=serialize_tool_definitions(all_tools),
    selected_history_count=len(selected_history),
    built_message_count=len(built_messages),
    selection_reason=selection_reason,
    session_id=self._session_id,
)
```

**失败模式**（chat_loop_service.py:1281-1282）：改完的 `items` 要过 `deserialize_prompt_items` + `_validate_function_call_context_anchors`，不合格就**整份丢弃只打一条 warning**。注入必须严格符合 item schema。

## 四、注入 Replyer 的两条通道

| 通道 | 位置 | 形态 | 用途 |
|---|---|---|---|
| **A. `extra_prompt`** | `maisaka.replyer.before_request`（maisaka_generator_base.py:1149） | 纯字符串，追加到回复要求 | 简单、稳、够用 |
| **B. `request_messages`** | `maisaka.replyer.before_model_request`（maisaka_generator_base.py:1262） | 完整 messages / items | 精细控制 |

**建议走 A**：Jev 判定本质是「一段约束文本」，不值得为它冒 item schema 校验失败被整份丢弃的风险。

通道 A 载荷（maisaka_generator_base.py:1143-1158）里还能改 `reply_tool_args`、`task_name`、`model_name`，也可以 abort（1160 的 `raise_if_aborted`）。

### 生命周期：per-request，不持久

- Planner / Replyer 的 items 每轮重新构建，Hook 每轮重新注入
- 注入内容进请求载荷，**不进存库的聊天历史**
- disguise 用 marker 串做幂等（prompt_builder.py:13-14），防的是同一份 items 被处理两次

若要多轮延续，需自己按 `session_id` 做状态缓存。

## 五、缓存污染：最容易踩的坑

DeepSeek 是**前缀缓存**，注入位置决定代价：

| 注入方式 | 代码位置 | 缓存影响 |
|---|---|---|
| 追加到已有 SystemMessageItem 的 parts 末尾 | plugin.py:626 | ✅ 前缀保留 |
| 在 items 头部 insert 新 SystemMessageItem | plugin.py:630-641 | ❌ 整段前缀作废 |
| 追加到 system message content | plugin.py:658 | ✅ 与第一种同效 |

**千万别图省事往头部插。**

Jev 判定每条不同，注入点之后必然 miss —— 但注入点之后本来就是聊天历史（每轮都变），净损失≈0。

## 六、硬约束核实

### 记忆入库：插件插不进去（已确认）

```
grep -cE "invoke_hook|hook" src/services/memory_flow_service.py
→ 0
```

全仓搜 `"memory.*"` / `"a_memorix.*"` 匹配到的全是配置项名，不是 Hook。

### attach_emoji：语义选择无 hook

`src/maisaka/builtin_tool/context.py` 的 `_resolve_emoji_attachment`(320) 与 `load_emoji_by_description`(364) 都没有 hook。该文件唯一的 hook 在 406 行，是 `maisaka.reply.after_split`，与表情选择无关。

### 但表情包入库有三个可 abort 的 hook（意外收获）

| Hook | 位置 | 拦什么 |
|---|---|---|
| `emoji.receive.before_cache` | emoji_system/emoji_manager.py:335 | 刚收到的表情图，缓存前 |
| `emoji.register.after_build_description` | emoji_manager.py:1012 | 描述构建完、注册前 |
| `emoji.maintenance.before_register` | emoji_manager.py:1176 | 自动维护注册前 |

这**部分补上了 MEMORY-POLICY 3.2「恶搞丑化图片不入库」**在表情包库这一侧的缺口。

注意区分：这是**表情包库**，不是 A_Memorix 的**图片记忆**（后者在 `src/A_memorix/core/image/`，属上游，无 hook）。

## 七、落地分层表

| 插入点 | 纯插件可做 | 需改主程序 | 做不到 |
|---|---|---|---|
| 消息接收前硬阻断 | ✅ `chat.receive.after_process` | | |
| 在 Planner 前跑 Jev 判定 | ✅ | | |
| Planner 提示词注入 | ✅ `maisaka.planner.before_request` | | |
| Planner 工具集裁剪 | ✅ 改 `tool_definitions` | | |
| **强制 Planner 必须回复** | | | ❌ |
| Replyer `extra_prompt` 注入 | ✅ | | |
| Replyer 最终 messages 注入 | ✅ | | |
| 按判定切换模型/任务 | ✅ 改 `task_name` / `model_name` | | |
| 回复文本后处理 | ✅ `maisaka.replyer.after_response` | | |
| 发送前拦截/改写 | ✅ `send_service.before_send` | | |
| 表情包入库拦截 | ✅ 三个 hook | | |
| **attach_emoji 语义选择介入** | | | ❌ |
| **记忆入库拦截** | | ✅ `memory_flow_service.py`（需许可） | |
| **A_Memorix 图片记忆审核** | | ✅ 上游 | |

## 八、两个隐坑

**坑 1：Hook 异常是静默的。**
所有 disguise 的 Hook 都是 `error_policy=ErrorPolicy.SKIP`。Jev 挂了、超时了，日志里可能只有一行 warning，行为退化成「没判定」。**必须自己做降级标记**。

**坑 2：`chat.receive.after_process` 的 abort 会连记忆一起断。**
这个 abort 发生在入库之前。所以用它拦，被 abort 的消息**不会进任何存储**。

反面看这是可利用的硬闸门：对「绝对不该记」的消息直接 abort，比在记忆链路里拦更彻底 —— 代价是这条消息也完全不回。

## 九、验证路径

### 阶段 1：`ctx.llm.generate` 占位（不接真 Jev）

| 验证项 | 怎么验 | 判定标准 |
|---|---|---|
| Hook 是否被调用 | 打日志 | 每条消息一次 |
| 注入是否进了 prompt | 读 `logs/maisaka_prompt/maisaka.replyer/` | 能看到 marker |
| 决策是否影响回复 | A/B 对比开/关插件 | 有可观察差异 |
| 延迟增量 | 统计面板「耗时」 | < 1.5s |
| 缓存命中率 | 统计面板「缓存命中」 | 掉到 30% 以下要重新设计 |
| item schema 是否合法 | 看有无「items 无效」警告 | 一条都不能有 |

### 阶段 2：必须真 Jev 才能测

概率校准度、毫秒级延迟、按次计价成本、无 JSON 抖动的稳定性。

### 建议顺序

1. 最窄链路：只做意图分类 → 只注入 `extra_prompt` → 只对一个群生效
2. 跑一周看缓存命中率与延迟
3. 数据可接受，再决定上不上真 Jev、要不要扩到 Planner 层

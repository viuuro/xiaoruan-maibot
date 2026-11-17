# 小软 · Jev 决策适配

把结构化决策能力装进 MaiBot，核心用途是判断**此刻该不该说话、以什么姿态说话**。

目前落地的能力：发言决策（潜水 / 接话 / 开话题 / 应答）+ 开话题前的钩子校验。

---

## 三种后端

| `backend.kind` | 概率来源 | 可信度 | 备注 |
|---|---|---|---|
| `logprob`（默认） | 模型输出层真实 logprobs | ✅ 可用 | 实测 `qwen-plus` 可用 |
| `jev` | TypeSafe System One | ✅ 可用 | 国内需第三方中转 |
| `llm` | 模型自己报的数字 | ❌ 不可信 | 零额外成本，仅作降级 |

### 关于 logprobs 的实测结论

| Provider | 结论 |
|---|---|
| `deepseek-v4-flash` | ❌ 落选 token 全是 `-9999.0` 哨兵，**没有真实分布**，代码会自动拒绝 |
| `qwen-plus` | ✅ 给出真实且各不相同的 logprob |
| `qwen3.8-omni-flash` | ⚠️ 数值真实但落在推理段上，配 `max_tokens=1` 不可用 |
| `qwen-max` | ❌ 只返回 1 个候选 |

---

## 模型使用的硬边界

**qwen 只用于决策判定，不参与任何生成场景。**

- 对话、回复、记忆摘要、表情打标 → 全部走 `deepseek-v4-flash`
- 决策后端独立配置在 `[backend.logprob]`，自己带 key、自己带模型名，**不经过 MaiBot 的 `model_task_config`**
- 因此 `config/model_config.toml` 里**不应出现任何 qwen 模型**；早期 `fast_model` 与 `emoji` 曾混用过 qwen，已清理

这样做的原因：决策要的是「稳定的概率读数」，生成要的是「好文笔」。让同一个模型兼顾两件事时，任何一方调整都可能波及另一方。

---

## 发言决策

### 两段式

```
第 1 步（每次必判）：
  A 潜水    —— 现在不该说话
  B 接话    —— 可以参与当前话题
  C 开话题  —— 可以引出新话题（需第 2 步确认）
  D 应答    —— 有人在叫我，必须回应

第 2 步（仅当 C）：
  A 有钩子  —— 上下文里确实存在可自然引出的内容
  B 没钩子  —— 没有切入点，此时开话题会突兀
```

第 2 步判成「没钩子」时，动作自动**降级为接话**，避免莫名其妙地起话题。

### 不依赖频率

这个决策**没有随机数、没有概率触发、没有频率限制**。输入是最近消息 + 自己的人设，输出是纯语义判断。同一段对话判 100 次结果一致。

与 MaiBot 自带的 `talk_value` 这类「按概率扔骰子」的触发是两回事。

---

## 概率的真实含义

实测 qwen-plus 的 logprob **间隔**（第一名与第二名之差）通常落在 10~22 之间：

| 温度 T | 间隔 11.75 的头部概率 | 间隔 24.25 的头部概率 |
|---|---|---|
| 1 | 99.999% | 99.999% |
| 4 | 95% | 99.8% |
| **8（默认）** | **81%** | **95%** |
| 16 | 68% | 82% |

两个必须知道的限制：

1. **这个数字是「模型自己的把握」，不是「判断正确的概率」**，不能当准确率用。
2. **间隔在两次相同调用之间会波动**（`temperature=0` 也不完全确定），所以固定阈值不够稳。

代码里的应对：间隔小于 `uncertain_gap`（默认 12）时输出 `uncertain: true`，用来触发保守分支。

---

## Hook 注入

挂在 `maisaka.planner.before_request`，在 Planner 发起请求前跑决策并注入结论。

### 注入位置的讲究

必须**追加到已有 `SystemMessageItem` 的 `parts` 末尾**。

往 `items` 头部插入新 item 会让 DeepSeek 的前缀缓存整段作废 —— 日志里 Replyer 缓存命中率本来就在 34%~67% 波动，再插头部会明显更差。

找不到 `SystemMessageItem` 时**宁可不注入**，也不为了兜底去破坏缓存。

### 降级不能静默

Hook 的 `error_policy` 是 `SKIP`，异常会被静默吞掉。所以：

- 决策失败时注入一段「本轮评估未完成」的说明，让降级可见
- 所有状态记录在 `_last_hook`，可通过 `stats` API 查看

---

## 配置

```toml
[backend]
kind = "logprob"

[backend.logprob]
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
model = "qwen-plus"
api_key = ""                      # 留空则读环境变量
api_key_env = "DASHSCOPE_API_KEY"
top_logprobs = 5                  # 阿里上限 5
softmax_temperature = 8.0
uncertain_gap = 12.0

[speak]
hook_enabled = true               # 是否启用 Planner Hook
check_opening = true              # 开话题前是否查钩子
opening_min_confidence = 0.6      # 开话题置信度门槛
constraint_style = "block"        # block=明确禁止；hint=只给建议
only_sessions = []                # 灰度：只在这些 session 生效，留空=全部
```

### 灰度上线

先填一个群的 `session_id` 到 `only_sessions`，只在那一个群跑，观察几天再放开。

`session_id` 可以从 MaiBot 日志里找到（`session=xxxx`）。

---

## WebUI API

| API | 用途 |
|---|---|
| `decide_speak` | 贴一段聊天记录，跑一次发言决策 |
| `test_backend` | 检查后端配置，jev 会真实打一次请求 |
| `selftest` | 跑一次固定决策验证链路 |
| `stats` | 调用次数、失败次数、最近一次 Hook 状态 |

---

## 验证记录

| 项目 | 结果 |
|---|---|
| 发言决策（4 动作） | 7/7 命中设计预期 |
| 钩子判断 | 4/4 命中 |
| Hook 注入端到端 | 3/3 命中，灰度白名单正常 |
| Hook 单元逻辑 | 16 项全过 |
| 核心解析回归 | 30 项全过 |

---

## 已知限制

- 概率是模型把握，不是准确率；固定阈值不够稳
- 阿里 `top_logprobs` 上限 5，选项多于 5 个时部分标签拿不到概率
- 决策不产生话题内容 —— 判成「开话题」后，具体说什么仍由 Planner/Replyer 生成
- 记忆入库那道闸插件**插不进去**（链路在 `src/services/memory_flow_service.py`，需改主程序）

---

## 许可

MIT

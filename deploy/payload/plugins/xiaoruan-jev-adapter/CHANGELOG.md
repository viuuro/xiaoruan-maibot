# Changelog

## 0.2.0

### 主要功能

- 新增 `logprob` 后端：直接读模型输出层的 logprobs，拿到**真实概率分布**
- 新增两段式发言决策 `should_speak`：先定动作（潜水/接话/开话题/应答），
  判成「开话题」时再跑一道钩子判断，没有自然切入点就降级为接话
- 新增模块：`logprob.py`（分类器）、`decisions.py`（决策定义）

### 细节

- **哨兵检测**：DeepSeek 的 `top_logprobs` 全是 `-9999.0` 填充，代码会自动拒绝并提示换 provider
- **温度缩放**：实测 logprob 间隔常落在 10~22，softmax 后必然塌成 1.0；
  默认温度改为 8.0，把分布摊到 0.7~0.87 的可用区间
- **不确定标记**：间隔小于 `uncertain_gap`（默认 12）时标记 `uncertain=True`，
  用于触发保守分支
- **间隔符号修正**：初版把 `second - top` 写反了，导致所有结果被误判为「拿不准」
- 修复配置注入测试用错属性名（应为 `_plugin_config_instance`）

### 已知限制

- 概率是「模型自己的把握」，不是「判断正确的概率」，不能当准确率用
- 间隔在两次相同调用之间会波动（temperature=0 也不完全确定），
  固定阈值因此不够稳；要更稳需要多采样投票
- 阿里 `top_logprobs` 上限为 5，选项多于 5 个时部分标签拿不到概率

## 0.1.0

### 主要功能

- 搭建 Jev 决策适配插件骨架：`Choice` / `Score` / `Noul` 三种原语
- 实现提示词渲染、JSON 提取、答案解析、概率归一化
- 暴露 `classify_intent` 与 `decide` 两个工具
- 提供 WebUI 自检 API `selftest` 与统计 API `stats`

### 细节

- 决策调用带超时兜底，超时抛 `DecisionError` 而不阻塞主链路
- 模型调用统一走 `ctx.llm.generate`，插件不持有任何 API Key
- 概率归一化对 NaN、负数、全零做防御

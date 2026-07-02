# PRD 全量审查报告

> 生成时间: 2026-07-02
> 审查范围: 9 个子项目，50+ PRD 文件
> 分析方式: 4 个并行 Agent 深度分析

---

## 总览

| 项目 | PRD 文件 | 问题数 | 高严重度 |
|------|----------|--------|----------|
| TrendScope | docs/PRD.md | 13 | 8 |
| platform-orchestrator | docs/PRD.md | 12 | 0 (已修复 8) |
| shared-models | docs/PRD.md | 10 | 5 |
| content-aggregator | 4 份 PRD | 4 | 3 |
| smart-sentence-splitter | docs/PRD.md | 5 | 2 |
| prompt-engine | docs/PRD.md | 5 | 3 |
| Story2Video | docs/prd.md | 17 | 6 |
| Multi-Publish | 01-docs/PRD.md | 18 | 10 |

---

## 跨项目 TOP 8 高危问题

| # | 问题 | 涉及项目 | 风险 |
|---|------|---------|------|
| 1 | TrendScope 与 orchestrator 双用户系统冲突 ✅ orchestrator 已增加认证统一章节 — 谁是身份权威源？JWT 互认机制未定义 | TrendScope + Orchestrator | 身份分裂致权限混乱 |
| 2 | shared-models 未覆盖 Story2Video / Multi-Publish — 管道最后两站不在契约范围内 | 全部 | 数据流转无保障 |
| 3 | 异步 pipeline 缺少状态持久化 ✅ 已增加状态持久化章节 + async_tasks 表 — FastAPI BackgroundTasks 无持久化，进程崩溃丢失所有任务 | Orchestrator + 管线 | 任务丢失 |
| 4 | 4G 内存 + 同进程 SDK = OOM 风险 ✅ 已增加内存预算分配表 — 6 个模块共享进程，内存隔离策略为零 | Orchestrator + 全部 | 生产 OOM |
| 5 | LLM 选型全回避 — 所有 PRD 都未定义具体模型选择、成本预算、质量保证方案 | 全部 | 核心能力无保障 |
| 6 | PRD 去重未完成 — content-aggregator 的 PROJECT-001-PRD 和 hot-content-rewrite-v2.0 重叠约80% | content-aggregator | 开发参考混乱 |
| 7 | 发布状态粒度不一致 ✅ 已增加 PublishStage 枚举 — pipeline 只有 GENERATED → PUBLISHED，实际有 4+ 中间状态 | Story2Video + Multi-Publish | 状态机断裂 |
| 8 | Feature Gate 三处散落 ✅ 已增加统一管理章节 — Story2Video 用 localStorage，orchestrator 用 YAML，Multi-Publish 无 gate | 3 个子项目 | 功能开关不统一 |

---

## 一、TrendScope（热榜）

### 高严重度问题

1. **跨平台热度归一化公式未定义** — 微博热度 vs B站播放量，量纲完全不同
2. **公开 API 限流 QPS 未量化** — "合理限流"到底是多少？
3. **代理池来源与成本未提及**
4. **全文搜索技术选型未定** — 直接影响 P99 < 200ms 能否达标
5. **Pipeline 推送接口格式未定义** — 依赖 shared-models 哪些模型？
6. **评分矩阵无排期** — LLM 还是规则引擎？调用成本？
7. **第三方 API 付费计价完全空白**
8. **13个平台采集深度/频率未定义** — Top50？Top100？5分钟 or 1小时？

### 接口问题

- TrendScope 独立用户系统 vs orchestrator SSO 身份冲突
- API Key 管理与 orchestrator 功能重叠
- 评分结果是否回写 shared-models？TrendingTopicModel 是否需要 score 字段？

---

## 二、platform-orchestrator（统一入口）

### 高严重度问题

1. **异步任务状态管理完全未定义** — 进度查询、取消、重试、失败回调
2. **视频串行队列容量/超时丢弃策略未定义**
3. **未定义内存预算分配** — 6模块共享进程
4. **Story2Video/Multi-Publish 不依赖 shared-models** — 与契约层定位矛盾
5. **API Key 加密算法/密钥管理/轮换策略未定义**
6. **Token 有效期矛盾** — 2h+7d vs 30d，两处 PRD 不一致
7. **Pipeline 错误处理策略未定义** — 步骤3失败后1-2的产出物怎么办？
8. **orchestrator 可用性目标未定义** — 作为统一入口 = 整个平台可用性

### 接口问题

- TrendScope 前端是否走 orchestrator :3000？
- 两套 JWT 互认机制空白
- Pipeline 中间状态持久化无方案
- 进程崩溃后恢复机制无

---

## 三、shared-models（数据契约层）

### 高严重度问题

1. **"9个子项目"但只覆盖6个模块** — Story2Video/Multi-Publish/Orchestrator 模型缺失
2. **pipeline.py "冻结"状态定义不清** — 紧急 bug fix 如何处理？
3. **缺少 TrendingTopicModel 与 Pipeline 关联字段**
4. **JWTPayload 缺少 iss/aud 字段** — 跨模块无法区分 Token 签发来源
5. **全局契约的强制力在定义时已被打破** — Story2Video/Multi-Publish 绕过

### 关键矛盾

- JWTAuthManager 含业务逻辑（密码哈希、Token 创建） vs "不含业务逻辑"边界
- 缺少错误码枚举模型
- ProviderConfig 缺少 Rate Limit/Quota 字段
- RewriteResult 未定义改写质量评分字段

---

## 四、content-aggregator（内容聚合）

### 关键问题

1. **PRD 去重** — PROJECT-001-PRD 与 hot-content-rewrite-v2.0 重叠约80%，需选定唯一权威版本
2. **LLM 选型和约束缺失** — 核心技术假设，回避了具体模型选择和质量保证
3. **共享库能力缺口** — v0.1.0 标注核心功能完成，但 Cookie 持久化/浏览器池实际未实现
4. **接口对齐** — 数据结构需与 shared_models 做一次正式对齐

---

## 五、smart-sentence-splitter（语义分句）

### 高严重度问题

1. **"语义完整性"无可度量定义** — 无法写测试用例
2. **SSE 流式分句格式规范完全空白** — 无 event type、data schema、heartbeat

### 缺少约束

- 延迟 SLA（P95）
- 并发限制和排队机制
- 标准错误码体系
- 超长文本分块合并策略（overlap 机制）
- 字幕时长分配逻辑

---

## 六、prompt-engine（提示词优化）

### 高严重度问题

1. **6大平台覆盖优先级不明** — 文心一格已下线，是否替换？
2. **"小黑分镜"业务定位模糊** — PRD 正文未解释

### 缺少约束

- LLM 调用成本上限（单次 token 预算 + 日累计）
- API 认证机制（是否继承 orchestrator JWT？）
- 数据持久化策略（反馈/权重数据存储在哪？）
- 25维风格维度完整清单
- 模板 DSL 语法规范

---

## 七、Story2Video（视频合成）

### 高严重度问题

1. **视频时长上限未定义**
2. **音频上传缺文件大小/时长上限**
3. **多用户并发策略未提及**
4. **会员"次/天"定义模糊** — 生成？下载？发布？

### 接口问题

- VideoAsset/ScenePrompt 无法承载分段模式的混合输入（缺 audio_file 字段）
- 发布进度缺中间状态（GENERATED → PUBLISHED 太粗）
- publish_results 只是 Dict[str, Any]

### 其他

- PRD 混入实现状态标记，需求与进度应分离
- localStorage 控制功能开关不适用于生产环境

---

## 八、Multi-Publish（多平台发布）

### 高严重度问题

1. **抖音重复定义** — P0 和 P2 各出现一次
2. **V1.0 未发布但 PRD 标注 v2.0.0** — 版本混乱
3. **缺少用户认证章节**
4. **6 并发 Tab 内存/CPU 预估缺失**
5. **BaseRpaPublisher 接口只有伪代码**
6. **WebSocket 重连策略未定义**
7. **缺回滚/降级策略** — RPA 半成功状态处理
8. **缺审计日志需求**

### 接口问题

- 两入口（Story2Video + Multi-Publish）调同一接口但鉴权方式不同
- SQLite 本地数据与 orchestrator PostgreSQL 无同步规范
- 轮询频率/超时/任务锁未协商定义

---

## 建议优先行动

1. **统一用户认证方案** — 明确 TrendScope 与 orchestrator 谁是身份权威
2. **补全 shared-models 契约** — 为 Story2Video/Multi-Publish 增加 PublishResult、PublishSubStage、AudioInput 等类型
3. **设计异步 pipeline 状态持久化** — 选择方案（DB 轮询 / Redis / Celery）
4. **产出 LLM 选型文档** — 具体模型、成本预算、质量评估标准
5. **PRD 去重** — content-aggregator 选出唯一权威版本，其余归档
6. **发布状态机对齐** — shared-models 增加 PublishSubStage 枚举
7. **统一 Feature Gate** — 三处 gate 管理归并到 orchestrator 的 feature_gates.yaml
8. **定义错误码体系** — shared-models 增加 ErrorCode 枚举，所有子项目引用

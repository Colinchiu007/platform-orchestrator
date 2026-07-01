# 全平台统一会员系统设计方案

> 日期：2026-06-29 | 状态：已实施 (Phase 1 + Phase 2) | 作者：Architect

---

## 1. 现状总结

### 1.1 已有（orchestrator 后端）

| 模块 | 状态 | 文件 |
|------|------|------|
| JWT 认证 (HS256) | 已实现 | `middleware/auth.py`, `routers/auth.py` |
| 注册/登录/刷新/登出 | 已实现 | `routers/auth.py` |
| 4 级会员体系 (free/basic/pro/enterprise) | 已实现 | `routers/auth.py` FEATURES_MAP |
| 会员权益清单 | 已实现 | free→articles+basic_split, pro→+voice_clone+video_fixed_template |
| 支付端到端 (create-checkout/webhook/history) | 已实现 | `routers/payment.py` |
| Webhook HMAC-SHA256 验签 | 已实现 | `routers/payment.py` |
| Mock Payment Provider (可替换 Stripe) | 已实现 | `routers/payment.py` MockPaymentProvider |
| 订阅生命周期 (30天过期自动降级) | 已实现 | `services/subscription_lifecycle.py` |
| 每日配额 (free=3, basic=10, pro=50, enterprise=200) | 已实现 | `services/quota.py` |
| Feature Gate 装饰器 (@requires_feature) | 已实现 | `middleware/feature_gate.py` |
| 管理员用户管理 | 已实现 | `routers/admin_users.py` |
| 双数据库 (aiosqlite + PostgreSQL SQLAlchemy) | 已实现 | `db.py`, `db_pg.py`, `models/auth_models.py` |

### 1.2 Story2Video 前端 — 会员体系（已全部实现）

#### Phase 1 完成项

| 功能 | 状态 | 实现方式 |
|------|------|---------|
| 会员信息展示 | ✅ 已实现 | ProfilePage MembershipCard 显示等级/权益/配额/到期时间 |
| 会员查询 Hook | ✅ 已实现 | `src/hooks/useOrchestratorMembership.ts` — 调 orchestrator `/api/auth/subscription` |
| 配额查询 Hook | ✅ 已实现 | 同 Hook 内返回 usage/quota，缓存于 localStorage `orchestrator_usage_cache` |
| 支付升级 UI | ✅ 已实现 | `MembershipUpgradeDialog.tsx` — 5 步弹窗（套餐网格→确认→支付→成功/失败） |
| 前端 Feature Gate | ✅ 已实现 | `useFeatureGate` hook + `RequireFeature` 组件 + `LockedFeature` 占位 |
| 配额展示 + 预警 | ✅ 已实现 | `QuotaWidget` 内联组件（3 态：隐藏/黄色≤30%/红色0），CreatePage 预提交检查 |
| 路由级保护 | ✅ 已实现 | `RouteGuard` 包裹 MainLayout 内所有路由 |
| 预付配额检查 | ✅ 已实现 | `CreatePage.handleGenerate()` 中读取 `orchestrator_usage_cache` 拦截超额 |

#### Phase 2 完成项

| 功能 | 状态 | 实现方式 |
|------|------|---------|
| API Key 按 tier 限制 | ✅ 已实现 | `ApiSettingsDialog.tsx` 接入 `useOrchestratorMembership` |
| TIER_PROVIDERS 映射 | ✅ 已实现 | free→[publish], basic→[tts,image,publish], pro/enterprise→全量 |
| 锁定交互 | ✅ 已实现 | 受限 tab 显示 LockedFeature + 升级引导，tab 按钮 disabled |
| 自动切换 | ✅ 已实现 | 会员等级变化时自动切换到发布设置 tab |

---

## 2. 架构方案

### 采用的方案: Option C（纯 API Key 对接）

实际实现选择了 Option C（而非推荐的 Option B），原因是：

- Story2Video 保持 Supabase Auth 不变
- 所有会员/支付 API 调用通过 `X-API-Key` 头（`useApiKey()` 从 localStorage 读取）
- JWT 也支持（`useAuth` 中存储 orchestrator JWT）作为备用
- 无需改造 Story2Video 登录流程
- `get_current_user_or_api_key()` 双认证模式已在 orchestrator 实现

### 与 Option B 的差异

| 项目 | Option B (原推荐) | 实际实现 (Option C) |
|------|-------------------|-------------------|
| Auth | 两套 token 协调刷新 | 仅 API Key |
| 用户映射 | 自动懒注册 | 手动在 ApiSettingsDialog 配置 |
| Addr | 需额外配置 orchestrator URL | 已在 ApiSettingsDialog |

---

## 3. 核心数据流

```
┌──────────────────────┐         ┌─────────────────────┐
│    Story2Video        │         │   platform-orchestrator │
│  (React + Supabase)   │         │   (FastAPI + JWT)      │
└────────┬─────────────┘         └──────────┬──────────────┘
         │                                   │
         │  API Key (localStorage)           │
         │                                   │
  Membership ┼────────X-API-Key──────────► GET /api/auth/subscription
         │                                   │
  Upgrade ─┼────────X-API-Key──────────► POST /api/payment/create-checkout
         │                                   │
  Confirm ─┼────────JWT──────────────► POST /api/payment/confirm-mock
         │                                   │
  Quota ───┼────────X-API-Key──────────► GET /api/user/usage
         │                                   │
  Upgrade ─┼────────JWT──────────────► POST /api/auth/upgrade
```

---

## 4. 实施记录

### Phase 1: 前端会员体系（Story2Video）

#### 1.1 会员状态展示 — ProfilePage 增加会员卡片 ✅

**文件变更：** `src/components/MembershipCard.tsx`（新增），`src/pages/ProfilePage.tsx`（修改）

- MembershipCard 显示：当前会员等级、权益列表、配额进度条、到期时间
- 使用 `useOrchestratorMembership` Hook 获取实时数据
- orchestrator 不可用时降级显示"免费版"

#### 1.2 会员查询 Hook ✅

**新增文件：** `src/hooks/useOrchestratorMembership.ts`

- `useOrchestratorMembership()` Hook 封装 orchestrator API 调用
- 调用链路：`GET /api/auth/subscription` + `GET /api/user/usage`
- 通过 localStorage 的 orchestrator JWT 认证（fallback 到 API Key）
- 配额缓存 60 秒防抖

#### 1.3 支付升级 UI ✅

**新增文件：** `src/components/MembershipUpgradeDialog.tsx`（445 行）

- ProfilePage 点击"升级会员"按钮触发弹窗
- 5 步流程：套餐网格选择 → 确认弹窗 → 支付处理中 → 成功 → 关闭刷新
- 后端流程：`create-checkout` → 用户跳转 → `confirm-mock` 完成支付 → 自动刷新会员状态

#### 1.4 前端 Feature Gate ✅

**文件变更：** `src/hooks/useFeatureGate.ts`（新增），`src/components/common/RequireFeature.tsx`（新增），`src/App.tsx`（修改）

- `useFeatureGate(featureName)` → `{allowed, planRequired, planLabel, loading}`
- `FEATURE_TIER_MAP`: free→articles+basic_split, basic→+voice_clone+video_fixed_template, pro→+batch_split, enterprise→all
- `RequireFeature` 组件包裹需要 gating 的子组件
- `LockedFeature` 占位组件展示升级引导
- `RouteGuard` 包裹 MainLayout 内所有路由

#### 1.5 配额展示 + 预警 ✅

**文件变更：** `src/components/QuotaWidget.tsx`（新增），`src/pages/CreatePage.tsx`（修改）

- `QuotaWidget` 内联组件：3 态（quota 充足隐藏 / ≤30% 黄色预警 / 0 红色阻止）
- `CreatePage.handleGenerate()` 预提交配额检查
- 实际配额不足时显示 toast 阻止生成

### Phase 2: API Key 按 Tier 限制 ✅

**文件变更：** `src/components/ApiSettingsDialog.tsx`（修改）

| 会员等级 | 可用 Provider |
|---------|--------------|
| free | publish（发布设置） |
| basic | tts, image, publish |
| pro | llm, tts, video, image, publish |
| enterprise | llm, tts, video, image, publish |

- 受限 tab 显示 LockedFeature 图标 + "升级至 xx 以解锁"文字
- tab 按钮 disabled
- 会员等级变化时自动切换到发布设置 tab

---

## 5. 支付流程（详细）

```
User (Story2Video)              Orchestrator                  Mock Payment
       │                             │                            │
       │  1. 选择套餐（Pro ¥29.99）     │                            │
       ├─────────────────────────────►│                            │
       │                             │  2. create_checkout_session │
       │                             ├───────────────────────────►│
       │                             │  3. checkout_url + ID      │
       │                             │◄───────────────────────────┤
       │  4. 返回 {checkout_url}      │                            │
       │◄─────────────────────────────┤                            │
       │                             │                            │
       │  5. 用户点击"确认支付"         │                            │
       ├─────────────────────────────►                            │
       │  6. POST /api/payment/confirm-mock                        │
       │     (JWT 鉴权 + 归属校验)      │                            │
       │                             │  7. _complete_payment       │
       │                             │     双库更新 (aiosqlite+PG)│
       │                             │                            │
       │  8. 返回 {plan_type, status}  │                            │
       │◄─────────────────────────────┤                            │
       │                             │                            │
       │  9. 自动刷新 useOrchestratorMembership                    │
```

---

## 6. LLM API Key 与会员等级

实际实现：ApiSettingsDialog 接入 `useOrchestratorMembership`

| 会员等级 | API Key 自由度 | 可用 Provider |
|---------|---------------|--------------|
| free | 仅发布设置 | publish |
| basic | 可配置自定义 Key | TTS + 图片生成 + 发布 |
| pro | 完全自定义 | 所有 provider |
| enterprise | 完全自定义 | 所有 provider |

实现方式：前端 `TIER_PROVIDERS` 常量映射 → `isProviderRestricted()` → tab disabled + LockedFeature 占位

---

## 7. 测试状态

### Story2Video 前端
- `useOrchestratorMembership` 单元测试：待补（见 Phase X）
- `MembershipUpgradeDialog` 组件测试：待补
- `useFeatureGate` 单元测试：待补

### Orchestrator 后端

| 模块 | 测试数量 | 状态 |
|------|---------|------|
| `test_payment.py` | 13 tests | ✅ ALL PASS |
| `test_subscription.py` | 9 tests | ✅ ALL PASS |
| 合计 | 22 tests | ✅ ALL PASS |

修复问题：
- 测试隔离：改用 `_fresh_login()` 唯一用户模式，移除 `clean_tables` 硬编码 `orchestrator.db`
- Feature Gate：VM 环境缺少 `D:/Data/projects/feature_gates.yaml`，使用 `patch` 注入测试用 gate
- 验证方式：直接 DB 检查改为 API 验证（用户数据在 PG schema）

---

## 8. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| Orchestrator 不可用时 Story2Video 无法展示会员信息 | 前端降级 | 缓存最后一次会员数据，离线显示"免费版" |
| API Key 未配置时会员信息不可用 | 前端逻辑隐藏 | `useOrchestratorMembership` 在无 key 时返回 free |
| 两套 auth 系统状态不一致 | 用户困惑 | 统一由 orchestrator 管理会员状态，Story2Video 只读 |
| 支付 Mock 到真实 Stripe 的迁移 | 需改代码 | MockPaymentProvider 接口已预留 Stripe 替换点 |

---

## 9. 实施顺序（已完成）

```
Phase 1.1: 会员查询 Hook (useOrchestratorMembership) ──── ✅
Phase 1.2: ProfilePage 会员卡片 ────────────────────────── ✅
Phase 1.3: MembershipUpgradeDialog ─────────────────────── ✅
Phase 1.4: 前端 Feature Gate ─────────────────────────────  ✅
Phase 1.5: 配额展示 + 预警 ──────────────────────────────── ✅
Phase 2: API Key 按 tier 限制 ──────────────────────────── ✅
```

---

## 10. 待确认问题（已解决）

以下是实施过程中的关键决策记录：

| 问题 | 决策 |
|------|------|
| 用户映射方式 | 无需映射 — 使用 API Key 认证，不涉及多系统用户关联 |
| Mock 支付 vs 真实 Stripe | MVP 继续使用 Mock + `confirm-mock` 端点 |
| Orchestrator 部署地址 | 通过 ApiSettingsDialog 手动配置 |
| free 版配额 | 每天 3 条（已通过 QuotaWidget 前端展示） |
| 定价 (USD) | 基础 $9.99 / 专业 $29.99 / 企业 $99.99 |

---

## 11. Changelog

| 日期 | 变更 | 作者 |
|------|------|------|
| 2026-06-29 | 初始草案 | Architect |
| 2026-06-29 | Phase 1.1-1.5 完成，更新实施状态 | Agent |
| 2026-06-29 | Phase 2 (API Key tier restriction) 完成 | Agent |
| 2026-06-29 | 全量 22 测试通过 + 修复 3 个已存在测试隔离问题 | Agent |

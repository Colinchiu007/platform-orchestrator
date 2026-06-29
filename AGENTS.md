# platform-orchestrator — 开发流程规范

> 整合薄壳的开发流程与编码约定。AI 工具启动时自动读取。

---

## 核心原则

1. **薄壳模式**：orchestrator 只做路由+鉴权+编排，业务逻辑全在模块中
2. **零侵入**：不修改任何被引用模块的代码。模块保持独立 Git 仓库和可独立部署能力
3. **单进程**：所有模块作为库导入，同进程运行。不引入多进程/微服务通信
4. **先测试再编码**：所有新路由和中间件必须有对应的测试
5. **TDD**：RED → GREEN → REFACTOR

## AI 角色分工

| 角色 | 阶段 | 产出物 |
|------|------|--------|
| **PM（产品经理）** | 需求分析 | PRD、用户故事、功能列表 |
| **架构师** | 技术设计 | 模块接入方案、API 设计、数据流 |
| **开发工程师** | 编码实现 | 路由实现、中间件、TDD |
| **QA** | 质量验证 | 端到端测试、内存监控 |
| **CTO** | 代码评审 | 安全审查、性能审查 |

## 7 阶段开发流程

### 阶段 1：想法澄清
把模糊想法变成一句话需求，确认：模块名称、接入方式集成方式、前置依赖

### 阶段 2：PRD（PM）
产出：PRD 或变更说明，包含功能描述、API 端点清单、鉴权要求、数据流
**批准后才能进入下一阶段。**

### 阶段 3：技术架构（架构师）
产出：模块对接方案（Phase 1 SDK 导入 / Phase 2 BackgroundTasks）、路由设计、数据模型
**原则：选最简单的方案，不加新服务、不加重框架。**

### 阶段 4：开发计划（PM）
拆成 ≤4h 的任务，标注依赖关系，标注可并行项。

### 阶段 5：编码实现（开发 + TDD）
- 先写测试，再写路由
- 每次完成做手动验证：能启动 / 核心功能 / 非法输入不崩溃 / 错误提示友好

### 阶段 6：代码评审（CTO）
整库扫描以下维度：
- **安全**：硬编码密钥、Shell 注入、eval
- **鉴权**：保护端点是否加了 `Depends(get_current_user)`
- **错误处理**：异步异常是否被 try/except 捕获
- **SQL 注入**：使用参数化查询（`?` 占位符），避免 f-string 拼接
- **日志污染**：console.log 在生产代码中
- **功能开关**：新功能是否挂了 `@requires_feature`

分类输出：
```
🔴 CRITICAL | 文件:行号 | 描述 | 修复建议
🟠 MAJOR   | 文件:行号 | 描述 | 修复建议
🟢 MINOR   | 文件:行号 | 描述 | 修复建议
```
CRITICAL 必须修复才能继续。

### 阶段 7：发布（运维）
- 更新 CHANGELOG.md
- git 提交 + tag
- 更新 feature_gates.yaml（新功能开关正式上线）

## 质量门禁

**PRD 阶段**：MVP 范围清晰 / 鉴权要求明确 / 功能开关需求明确
**架构阶段**：最简单方案 / 目录结构明确
**开发阶段**：测试全通过 / 手动验证核心功能 / 错误处理到位
**Code Review**：CRITICAL 问题已修复 / 鉴权覆盖到位
**发布阶段**：CHANGELOG 更新 / git 已提交并 tag

## TDD 流程

```
RED   → 在 tests/ 下写失败测试（TestClient 模拟请求）
GREEN → 实现最小路由让测试通过
REFACTOR → 重构中间件/服务层，保持测试通过
```

### 测试规范（TestClient）

```python
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_protected_route_no_auth():
    response = client.get("/api/protected-endpoint")
    assert response.status_code == 401

def test_authenticated():
    from middleware.rate_limit import reset_rate_limits
    reset_rate_limits()
    client.post("/api/auth/register", json=TEST_USER)
    resp = client.post("/api/auth/login", json=TEST_USER)
    token = resp.json()["access_token"]
    response = client.get("/api/protected-endpoint",
                          headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
```

---

## 技术约定（以下为项目特有内容）

### 添加新路由

#### 1. 确定模块位置

如果新增独立功能，在 `routers/` 下创建新文件：

```python
# routers/my_module.py
from fastapi import APIRouter

router = APIRouter()

@router.get("/")
async def list_items():
    return {"items": []}
```

#### 2. 注册到 main.py

```python
from routers import my_module
app.include_router(my_module.router, prefix="/api/my-module", tags=["my-module"])
```

#### 3. 功能开关（如果需要）

```python
from middleware.feature_gate import requires_feature

@router.post("/")
@requires_feature("my_feature_name")
async def create_item(current_user = Depends(get_current_user)):
    ...
```

### 添加功能开关

#### 1. 在 feature_gates.yaml 中定义

```yaml
features:
  my_new_feature:
    tier: 2
    description: "新功能说明"
```

#### 2. 在代码中使用装饰器

```python
@requires_feature("my_new_feature")
```

#### 3. 重启服务后生效

修改 `feature_gates.yaml` 后重启即可，无需改代码。

### 鉴权模式

#### 保护端点

```python
from middleware.auth import get_current_user

@router.get("/protected")
async def protected_route(current_user = Depends(get_current_user)):
    return {"user": current_user["username"]}
```

#### 公开端点

不加 `Depends(get_current_user)` 即为公开端点。

### 数据库

- **引擎**：`aiosqlite`（异步 SQLite）
- **模式**：WAL（Write-Ahead Logging），支持并发读
- **建表**：`db.py` 的 `init_db()` 在应用启动时自动执行
- **访问**：通过 FastAPI 依赖注入

```python
from db import get_db

@router.get("/data")
async def get_data(db = Depends(get_db)):
    async with db.execute("SELECT * FROM jobs") as cursor:
        return await cursor.fetchall()
```

### 对接模块

#### Phase 1（当前阶段）：导入模块 SDK

```python
from splitter import SmartSentenceSplitter, SplitResult

splitter = SmartSentenceSplitter()

@router.post("/articles/{id}/split")
async def split_article(id: str, db = Depends(get_db)):
    # 1. 从数据库获取文章内容
    # 2. result: SplitResult = splitter.split(text)
    # 3. 保存结果
    ...
```

#### Phase 2+（未来阶段）：Task Queue

长耗时任务使用 `BackgroundTasks`：

```python
from fastapi import BackgroundTasks

@router.post("/jobs/video")
async def create_video(background_tasks: BackgroundTasks, ...):
    background_tasks.add_task(run_video_pipeline, article_id)
    return {"status": "queued", "job_id": job_id}
```

### 提交规范

```
feat: 添加新路由 /api/articles/batch-split
fix: 修复 JWT token 过期不刷新问题
docs: 更新 API 文档
refactor: 重构 feature_gate 为异步加载
```

### 资源守则

| 规则 | 说明 |
|------|------|
| 不引入新服务 | 不加 Redis、Celery、RabbitMQ |
| 不引入重框架 | 不加 Django ORM（用 aiosqlite 直接操作） |
| 内存上限 2.5G | systemd MemoryMax=2.5G |
| 视频任务串行 | 同时只允许 1 个视频任务 |

## 版本

**0.1.0** — Phase 0：骨架搭建完成。

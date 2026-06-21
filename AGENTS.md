# platform-orchestrator — 开发规范

> 整合薄壳的开发流程与编码约定。

## 核心原则

1. **薄壳模式**：orchestrator 只做路由+鉴权+编排，业务逻辑全在模块中。
2. **零侵入**：不修改任何被引用模块的代码。模块保持独立 Git 仓库和可独立部署能力。
3. **单进程**：所有模块作为库导入，同进程运行。不引入多进程/微服务通信。
4. **先测试再编码**：所有新路由和中间件必须有对应的测试。

## AI 角色分工

| 角色 | 职责 |
|------|------|
| **架构师** | 模块接入方案、API 设计、数据流 |
| **开发工程师** | 路由实现、中间件、TDD |
| **QA** | 端到端测试、内存监控 |
| **CTO** | 安全审查、性能审查 |

## 添加新路由

### 1. 确定模块位置

如果新增独立功能（非现有模块的子功能），在 `routers/` 下创建新文件：

```python
# routers/my_module.py
from fastapi import APIRouter

router = APIRouter()

@router.get("/")
async def list_items():
    return {"items": []}
```

### 2. 注册到 main.py

```python
from routers import my_module
app.include_router(my_module.router, prefix="/api/my-module", tags=["my-module"])
```

### 3. 功能开关（如果需要）

```python
from middleware.feature_gate import requires_feature

@router.post("/")
@requires_feature("my_feature_name")
async def create_item(current_user = Depends(get_current_user)):
    ...
```

## 添加功能开关

### 1. 在 feature_gates.yaml 中定义

```yaml
features:
  my_new_feature:
    tier: 2        # 1=入门版, 2=高级版
    description: "新功能说明"
```

### 2. 在代码中使用装饰器

```python
@requires_feature("my_new_feature")
```

### 3. 重启服务后生效

修改 `feature_gates.yaml` 后重启即可，无需改代码。

## 鉴权模式

### 保护端点

```python
from middleware.auth import get_current_user

@router.get("/protected")
async def protected_route(current_user = Depends(get_current_user)):
    # current_user: {"sub": "user-uuid", "username": "...", "tier": 1}
    return {"user": current_user["username"]}
```

### 公开端点

不加 `Depends(get_current_user)` 即为公开端点。

## 数据库

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

## 对接模块

### Phase 1（当前阶段）：导入模块 SDK

```python
# 示例：对接 smart-sentence-splitter
from splitter import SmartSentenceSplitter, SplitResult

splitter = SmartSentenceSplitter()

@router.post("/articles/{id}/split")
async def split_article(id: str, db = Depends(get_db)):
    # 1. 从数据库获取文章内容
    # 2. result: SplitResult = splitter.split(text)
    # 3. 保存结果到数据库
    # 4. 返回
    ...
```

### Phase 2+（未来阶段）：Task Queue

长耗时任务（TTS、图片生成、视频合成）使用 `BackgroundTasks`：

```python
from fastapi import BackgroundTasks

@router.post("/jobs/video")
async def create_video(background_tasks: BackgroundTasks, ...):
    background_tasks.add_task(run_video_pipeline, article_id)
    return {"status": "queued", "job_id": job_id}
```

## 测试

### 运行测试

```bash
cd /srv/projects/platform-orchestrator
python -m pytest tests/ -v
```

### 编写测试

```python
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_protected_route_no_auth():
    response = client.get("/api/articles/")
    assert response.status_code == 401  # 未认证
```

## 提交规范

```
feat: 添加新路由 /api/articles/batch-split
fix: 修复 JWT token 过期不刷新问题
docs: 更新 API 文档
refactor: 重构 feature_gate 为异步加载
```

## 资源守则

| 规则 | 说明 |
|------|------|
| 不引入新服务 | 不加 Redis、Celery、RabbitMQ |
| 不引入重框架 | 不加 Django ORM、SQLAlchemy（用 aiosqlite 直接操作） |
| 内存上限 2.5G | systemd `MemoryMax=2.5G` |
| 视频任务串行 | 同时只允许 1 个视频任务 |

## 版本

**0.1.0** — Phase 0：骨架搭建完成。

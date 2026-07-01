# platform-orchestrator — 技术约定

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


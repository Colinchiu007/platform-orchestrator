# platform-orchestrator — 测试规范

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


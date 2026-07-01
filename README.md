# platform-orchestrator — 统一平台入口

> 薄壳 FastAPI 应用，整合 5 个独立模块为完整视频生成平台。
>
> **当前状态**：Phase 0-4 全部完成，完整管道可用。

## 管道

```
register → login → fetch URL → LLM rewrite → split (scenes+subtitles)
    → TTS → prompt optimize → image gen → composit → publish
```

## 架构

```
                    platform-orchestrator (1 进程，~150MB 常驻)
                    ├── JWT 鉴权中间件
                    ├── 功能开关装饰器 (@requires_feature)
                    ├── SQLite 任务状态数据库（WAL 模式，4 表）
                    │
        ┌───────────┼───────────┬───────────┬───────────┐
        ▼           ▼           ▼           ▼           ▼
  aggregator    splitter   prompt-engine Story2Video  Multi-Publish
  (核心提取)    (直接导入)   (核心提取)    (Python 重写) (微信接入)

  所有模块：editable install，同进程内函数调用，零网络开销
```

## 快速启动

```bash
cd /srv/projects/platform-orchestrator

# 安装依赖
pip install fastapi uvicorn aiosqlite pyyaml python-jose passlib pydantic-settings
pip install httpx trafilatura lxml_html_clean jinja2 python-multipart
pip install email-validator

# 安装模块
pip install -e ../shared-models/
pip install -e ../smart-sentence-splitter/

# 启动
uvicorn main:app --reload --port 8000

# 访问
open http://localhost:8000/docs        # OpenAPI 文档
open http://localhost:8000/login       # Web 管理面板
```

## API 端点

### Auth（无需认证）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/register` | 注册 |
| POST | `/api/auth/login` | 登录 |
| POST | `/api/auth/refresh` | 刷新 Token |
| GET | `/health` | 健康检查 |
| GET | `/api/features` | 功能开关列表 |

### Articles（需认证）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/articles/` | 文章列表（分页） |
| GET | `/api/articles/{id}` | 文章详情 |
| POST | `/api/articles/fetch` | 抓取 URL + 可选改写 |
| POST | `/api/articles/{id}/split` | 分句（场景+字幕） |
| GET | `/api/articles/{id}/split` | 查看分句结果 |

### Jobs（需认证）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/jobs/video` | 创建视频生成任务 |
| GET | `/api/jobs/video/{id}` | 查询任务进度 |
| GET | `/api/jobs/video/` | 任务列表 |
| POST | `/api/jobs/publish` | 创建发布任务 |
| GET | `/api/jobs/publish/{id}` | 查询发布状态 |

### Web 页面

| 路径 | 说明 |
|------|------|
| `/login` | 登录页 |
| `/dashboard` | 控制台（文章/视频/发布统计） |
| `/articles` | 文章管理（抓取+列表） |
| `/jobs` | 任务列表（自动刷新） |

## 目录结构

```
platform-orchestrator/
├── main.py                  # FastAPI 应用入口
├── config.py                # 配置管理（18 项，PO_* 环境变量）
├── db.py                    # aiosqlite（WAL，4 表）
├── middleware/
│   ├── auth.py              # JWT HS256 鉴权
│   └── feature_gate.py      # 功能开关装饰器
├── routers/
│   ├── auth.py              # 注册/登录/刷新/me
│   ├── aggregator.py        # URL 抓取 + LLM 改写
│   ├── splitter.py          # 语义分句
│   ├── prompt.py            # 提示词优化
│   ├── video.py             # 视频生成编排（TTS→图片→合成）
│   ├── publish.py           # 多平台发布
│   └── web.py               # Jinja2 Web 页面
├── services/
│   ├── collect.py           # trafilatura 网页采集
│   ├── rewrite.py           # LLM 改写（4 风格 × 3 长度）
│   ├── tts_service.py       # 豆包 TTS + 语音克隆
│   ├── prompt_service.py    # 场景→提示词优化
│   ├── image_service.py     # 多供应商图片生成（MiniMax/SenseNova/Kling）
│   ├── video_service.py     # 多供应商视频生成（Kling/Jimeng）
│   ├── compositor.py        # FFmpeg 视频合成（替代 Canvas）
│   └── publish_service.py   # 微信发布
├── templates/               # 4 个 HTMX 页面
├── static/                  # CSS 暗色主题
└── pyproject.toml
```

## 功能开关

18 个功能开关由 `/srv/projects/feature_gates.yaml` 控制：

```yaml
features:
  article_manual_fetch:    { tier: 1 }   # 入门版
  split_batch:             { tier: 2 }   # 高级版
  video_voice_clone:       { tier: 2 }   # 高级版
  publish_batch_platforms: { tier: 2 }   # 高级版
  ...
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `PO_SECRET_KEY` | JWT 密钥 | — |
| `PO_OPENAI_API_KEY` | LLM API Key | — |
| `PO_OPENAI_BASE_URL` | LLM API 地址 | `https://api.openai.com/v1` |
| `PO_OPENAI_MODEL` | LLM 模型 | `gpt-4o-mini` |
| `PO_DOUBAO_API_KEY` | 豆包 TTS | — |
| `PO_MINIMAX_API_KEY` | MiniMax 图片 | — |
| `PO_WECHAT_APPID` | 微信 AppID | — |
| `PO_WECHAT_APPSECRET` | 微信 AppSecret | — |

## 资源约束

| 指标 | 目标 |
|------|------|
| 常驻内存 | <200MB |
| 峰值内存（视频任务） | <800MB |
| 数据库 | SQLite（WAL 模式） |
| 并发视频任务 | 1（严格串行） |
| 服务器 | 4G 阿里云 ECS |

## 版本

**0.2.0** — Phase 0-4 完成，全管道可用。

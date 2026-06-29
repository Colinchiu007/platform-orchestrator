## [0.6.0] - 2026-06-29

### Added
- Unified jobs API: `GET /api/jobs/`, `GET /api/jobs/{id}`, `POST /api/jobs/{id}/retry`
- User settings API: `GET/PATCH /api/settings/profile`, `CRUD /api/settings/api-keys`
- API key management: hashed storage + last-used tracking
- 26 new tests (21 API alignment + 5 video pipeline fixes)

### Fixed
- video.py missing QuotaExceededError import
- Test isolation: add user_daily_usage to clean_tables fixture

# Changelog

## [0.5.2] - 2026-06-27

### Changed
- pipeline_v2 feature gate enabled (Block 引擎管线，取代旧 hardcode 4 步流程)
- feature_gates.yaml: pipeline_v2.enabled → true

### Added
- 全内容管道编排完成：趋势发现 → 采集 → 改写 → 分句 → 提示词 → 视频 (F5)



All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-06-27

### Added

- **ProviderRouter**: Unified LLM provider config management system
  - Fernet AES-GCM encrypted storage for API keys
  - aiosqlite persistence with tier-based access control
  - Admin CRUD API + user provider query API
  - Admin frontend page at `/admin/providers`
  - User provider config page at `/settings/providers`
- **ProviderRouter Migration**: 5 services migrated from settings.xxx to ProviderRouter
  - rewrite.py, tts_service.py, image_service.py, video_service.py, publish_service.py
- **Usage Tracking**: User daily usage quota enforcement (free=3/basic=10/pro=50/enterprise=200)
  - `GET /api/user/usage` endpoint, 429 response when over quota
- **Subscription Lifecycle Daemon**: Auto-expire subscriptions on daily startup
- **Admin User Management**: List/detail/toggle-status API with pagination and filtering
- **E2E Test Suite**: 15 test cases across health/auth/provider CRUD/user operations/usage
- **Admin Users Tests**: 16 tests with pagination/filter/search/status toggle
- **Subscription Lifecycle Tests**: 8 tests for expiry detection and maintenance

### Changed

- routers/video.py: Fixed queue-status route shadowing, added quota check before video creation
- db_pg.py: Wrapped init_pg_db() in try/except for graceful PostgreSQL fallback

### Security

- API keys now encrypted at rest via Fernet AES-GCM
- ProviderRouter tier enforcement: admin-only write, admin+user read

## [0.3.2] - 2026-06-27

### Fixed
- main.py: create_app() now returns the FastAPI app instance (was returning None)
- feature_gates.yaml: added premium_content gate (tier 3, enabled false) to prevent silent-allow
- shared-models: migrated routers/auth.py RefreshRequest and JWTPayload to shared_models.auth
- services/__init__.py: fixed truncated __all__ string literal

### Added
- tests/test_integration_routes.py: 11 HTTP-level integration tests for trending/video/publish routes

### Changed
- middleware/auth.py: imports JWTPayload from shared_models

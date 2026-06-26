# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- routers/auth.py: RefreshRequest now uses shared_models.auth.RefreshRequest
- Total test count: 150 (was 139, +11 integration tests)

## [0.3.1] - 2026-06-26

### Changed
- RegisterRequest.email 改为可选（EmailStr → str | None）
- 前端请求失败时自动解析 FastAPI 422 错误，显示可读消息而非原始 JSON
- 注册页邮箱改为选填

### Added
- ECS 生产部署完成（nginx + orchestrator + frontend）
- systemd 进程保活
- POST /generate 接入 BackgroundTask

### Changed
- N 合 1 前端路由代理配置
- 统一认证 SSO (P3-01) 完成
- 全链路集成测试通过

## [0.2.0] - 2026-06-26

### Added
- N 合 1 前端路由代理配置
- 统一认证 SSO (P3-01) 完成
- 全链路集成测试通过

## [0.1.0] - 2026-06-25

### Added
- 初始版本，FastAPI 薄壳统一入口搭建
- feature_gates.yaml 集成

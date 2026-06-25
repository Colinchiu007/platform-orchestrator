#!/usr/bin/env python3
"""P3-01 Phase B: 三系统用户迁移 -> orchestrator auth.users

从 trendscope.users 和 content-aggregator.users 读取用户数据，
导入到 platform-orchestrator 的 auth.users 表中（PostgreSQL，schema: auth）。

合并策略:
  - username 重复 -> orchestrator 已有的保留，外来用户跳过
  - email 重复 -> 同 username
  - 密码哈希 -> 所有系统均用 passlib+bcrypt，直接复用

用法:
    python scripts/migrate_users.py --dry-run     # 预览
    python scripts/migrate_users.py               # 执行
"""

import argparse
import asyncio
import os
import sys

try:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
except ImportError:
    print("需要 sqlalchemy[asyncio]: pip install sqlalchemy[asyncio]")
    sys.exit(1)


def parse_args():
    p = argparse.ArgumentParser(description="三系统用户迁移至 orchestrator auth.users")
    p.add_argument("--dry-run", action="store_true", help="预览模式")
    p.add_argument("--ts-db", default=os.getenv(
        "TS_DATABASE_URL",
        "postgresql+asyncpg://trendscope:trendscope_dev@localhost:5432/tendscope"))
    p.add_argument("--ca-db", default=os.getenv(
        "CA_DATABASE_URL",
        "postgresql+asyncpg://trendscope:trendscope_dev@localhost:5432/content_aggregator"))
    p.add_argument("--orchestrator-db", default=os.getenv(
        "PO_DATABASE_URL",
        "postgresql+asyncpg://trendscope:trendscope_dev@localhost:5432/tendscope"))
    return p.parse_args()


async def read_users(engine, table_path: str) -> list[dict]:
    async with engine.connect() as conn:
        try:
            result = await conn.execute(text(f"SELECT * FROM {table_path} ORDER BY id"))
            rows = result.all()
            cols = result.keys()
            return [dict(zip(cols, row)) for row in rows]
        except Exception as e:
            print(f"  [跳过] 读取 {table_path} 失败: {e}")
            return []


def normalize_user(record: dict, source: str, source_id) -> dict:
    return {
        "uuid": str(record.get("uuid", "")),
        "username": str(record.get("username", "")),
        "email": str(record.get("email", "")),
        "password_hash": str(record.get("password_hash", "")),
        "subscription_type": str(record.get("subscription_type", "free")),
        "is_active": bool(record.get("is_active", True)),
        "source": source,
        "source_id": source_id,
    }


async def migrate_users(args):
    dry = args.dry_run
    if dry:
        print("=== 干跑模式 (--dry-run) ===\n")

    ts_engine = create_async_engine(args.ts_db)
    ca_engine = create_async_engine(args.ca_db)
    orch_engine = create_async_engine(args.orchestrator_db)

    # Ensure auth schema + tables
    async with orch_engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS auth"))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS auth.users (
                id SERIAL PRIMARY KEY,
                uuid VARCHAR(36) UNIQUE NOT NULL,
                username VARCHAR(50) UNIQUE NOT NULL,
                email VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(128) NOT NULL,
                subscription_type VARCHAR(20) DEFAULT 'free',
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS auth.refresh_tokens (
                id SERIAL PRIMARY KEY,
                token_jti VARCHAR(36) UNIQUE NOT NULL,
                user_uuid VARCHAR(36) NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                revoked BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS auth.subscriptions (
                id SERIAL PRIMARY KEY,
                user_uuid VARCHAR(36) UNIQUE NOT NULL,
                plan_type VARCHAR(20) DEFAULT 'free',
                status VARCHAR(20) DEFAULT 'active',
                start_date TIMESTAMP,
                end_date TIMESTAMP,
                auto_renew BOOLEAN DEFAULT FALSE
            )
        """))
    print("auth schema + tables ensured\n")

    # Read existing orchestrator users
    async with orch_engine.connect() as conn:
        result = await conn.execute(text("SELECT username, email, uuid FROM auth.users"))
        existing = {(r[0], r[1]): r[2] for r in result.all()}

    # Read source users
    ts_users = await read_users(ts_engine, "public.users")
    print(f"TrendScope 用户: {len(ts_users)} 条")
    ca_users = await read_users(ca_engine, "public.users")
    print(f"Content-Aggregator 用户: {len(ca_users)} 条")

    # Merge + dedup
    candidates = []
    for u in ts_users:
        candidates.append(normalize_user(u, "trendscope", u.get("id", 0)))
    for u in ca_users:
        candidates.append(normalize_user(u, "content-aggregator", u.get("id", 0)))

    to_insert = []
    skipped = 0
    for u in candidates:
        key = (u["username"], u["email"])
        if key in existing:
            skipped += 1
            if dry:
                print(f"  [冲突] {u['username']} ({u['email']})")
            continue
        to_insert.append(u)

    print(f"\n合并候选: {len(candidates)} 条 | 跳过(冲突): {skipped} | 待导入: {len(to_insert)}")

    if not to_insert:
        print("\n无需迁移。")
        return

    if dry:
        print("\n=== 以下用户将被导入 ===")
        for i, u in enumerate(to_insert, 1):
            print(f"  {i:3}. {u['username']:20} {u['email']:30} [{u['source']}]")
        return

    async with orch_engine.begin() as conn:
        for u in to_insert:
            await conn.execute(
                text(
                    "INSERT INTO auth.users "
                    "(uuid, username, email, password_hash, subscription_type, is_active) "
                    "VALUES (:uuid, :username, :email, :ph, :st, :ia)"
                ),
                {"uuid": u["uuid"], "username": u["username"], "email": u["email"],
                 "ph": u["password_hash"], "st": u["subscription_type"], "ia": u["is_active"]},
            )

    print(f"\n迁移完成: {len(to_insert)} 条用户已导入 auth.users")

    await ts_engine.dispose()
    await ca_engine.dispose()
    await orch_engine.dispose()


if __name__ == "__main__":
    asyncio.run(migrate_users(parse_args()))

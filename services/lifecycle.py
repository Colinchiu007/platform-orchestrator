"""
AppLifecycle — 应用生命周期管理。

从 MediaCrawler tools/app_runner.py 适配。
为 FastAPI 提供：
- 后台任务注册与追踪
- 优雅关闭（SIGTERM 时 drain 运行中任务）
- 强制退出保底（cleanup_timeout）
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Set

logger = logging.getLogger(__name__)


class AppLifecycle:
    """
    单例生命周期管理器。

    用法：
        lifecycle = AppLifecycle()

        # 在 lifespan startup 时初始化
        lifecycle.init()

        # 创建后台任务时注册
        task = asyncio.create_task(async_fn())
        lifecycle.register(task)

        # 在 lifespan shutdown 时优雅停止
        await lifecycle.shutdown()
    """

    def __init__(self, cleanup_timeout: float = 30.0, force_exit_code: int = 130):
        self._tasks: Set[asyncio.Task] = set()
        self._cleanup_timeout = cleanup_timeout
        self._force_exit_code = force_exit_code
        self._shutdown_requested = False
        self._drain_callbacks: list[asyncio.coroutines] = []

    def register(self, task: asyncio.Task) -> None:
        """注册后台任务，关闭时将等待它完成"""
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def on_drain(self, callback):
        """注册关闭前需要 drain 的回调（如 VideoConcurrencyController.drain）"""
        self._drain_callbacks.append(callback)
        return callback

    def init(self):
        """初始化信号处理"""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
            except NotImplementedError:
                pass

    def _request_shutdown(self):
        if self._shutdown_requested:
            logger.warning("第二次中断信号，强制退出")
            os._exit(self._force_exit_code)
        self._shutdown_requested = True
        logger.info(
            "关闭信号已接收，等待 %d 个后台任务完成..."
            % len(self._tasks)
        )

    async def shutdown(self) -> None:
        """执行优雅关闭"""
        # 阶段 1: 执行 drain 回调（拒绝新任务、排空队列）
        for cb in self._drain_callbacks:
            try:
                await cb()
            except Exception as e:
                logger.warning("drain 回调失败: %s", e)

        # 阶段 2: 取消所有剩余后台任务
        if not self._tasks:
            logger.info("无运行中的后台任务，直接退出")
            return

        count = len(self._tasks)
        logger.info("等待 %d 个后台任务（最多 %.1fs）...", count, self._cleanup_timeout)

        for task in self._tasks:
            task.cancel()

        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=self._cleanup_timeout,
            )
            logger.info("所有 %d 个后台任务已安全结束", count)
        except asyncio.TimeoutError:
            logger.warning(
                "关闭超时（%.1fs），%d 个任务未完成",
                self._cleanup_timeout,
                len([t for t in self._tasks if not t.done()]),
            )

    @property
    def active_count(self) -> int:
        return len(self._tasks)


# 模块级单例
lifecycle = AppLifecycle()

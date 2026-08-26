import asyncio
import threading
from typing import Dict, Optional, Any, Callable, Coroutine
from polymarket.logger import logger
from polymarket import risk_logger

class BoundedDropOldestQueue(asyncio.Queue):
    """
    带背压保护与丢旧保新机制的异步队列 (Bounded Drop-Oldest Queue)。
    当队列满载时，自动丢弃队头最旧的一帧，确保下游策略永远读取到毫秒级最新行情快照。
    """
    def __init__(self, maxsize: int = 50):
        super().__init__(maxsize=maxsize)

    def put_nowait(self, item: Any) -> None:
        """非阻塞压入队列，满载时弹出最旧元素"""
        if self.full():
            try:
                self.get_nowait()
            except asyncio.QueueEmpty:
                pass
        super().put_nowait(item)


class MarketTaskSupervisor:
    """
    市场协程任务监管中心 (Market Task Supervisor)。
    
    职责：
    1. 维护所有活跃市场协程的强引用注册表，防止 Python 3.12+ GC 过早回收；
    2. 绑定任务完成回调，自动注销完成任务并统一捕获未处理异常上报；
    3. 支持按任务标识优雅取消与全局排空。
    """
    def __init__(self):
        self._tasks: Dict[str, asyncio.Task] = {}
        self._lock = threading.Lock()

    def register_task(self, key: str, task: asyncio.Task, strategy_id: str = "default", market_id: str = "") -> None:
        """注册并监管协程任务"""
        with self._lock:
            # 若已存在同名旧任务且未完成，先取消旧任务
            if key in self._tasks and not self._tasks[key].done():
                self._tasks[key].cancel()
            self._tasks[key] = task

        def _on_done(t: asyncio.Task):
            with self._lock:
                self._tasks.pop(key, None)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.critical(f"[TaskSupervisor] 任务崩溃 ({key}): {exc}", exc_info=exc)
                risk_logger.push_risk_event(
                    market_id=market_id or key,
                    asset="UNKNOWN",
                    strategy=strategy_id,
                    reason=f"异步任务崩溃: {exc}",
                    level="critical"
                )

        task.add_done_callback(_on_done)

    def cancel_task(self, key: str) -> bool:
        """取消指定任务"""
        with self._lock:
            task = self._tasks.get(key)
            if task and not task.done():
                task.cancel()
                return True
        return False

    def get_active_task_count(self) -> int:
        """获取当前受监管的活跃任务数"""
        with self._lock:
            return len(self._tasks)

    def cancel_all(self) -> None:
        """取消全部受监管的任务"""
        with self._lock:
            for task in list(self._tasks.values()):
                if not task.done():
                    task.cancel()
            self._tasks.clear()


class AsyncRuntime:
    """
    全局统一异步运行时与事件循环引擎 (Unified Async Runtime)。
    以单例模式运行。
    
    核心特性：
    1. 自适应事件循环绑定 (Adaptive Loop Binding)：
       - 若当前已有运行中的 Loop (如 Uvicorn / Pytest)，自动绑定使用；
       - 若处于纯同步环境，拉起后台长驻守护线程与 Loop。
    2. 集成 MarketTaskSupervisor 强引用与异常兜底。
    """
    _instance: Optional["AsyncRuntime"] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(AsyncRuntime, cls).__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        with self._lock:
            if self._initialized:
                return

            self.supervisor = MarketTaskSupervisor()
            self._loop: Optional[asyncio.AbstractEventLoop] = None
            self._daemon_thread: Optional[threading.Thread] = None
            self._loop_ready_event = threading.Event()

            # 自适应初始化事件循环
            self._ensure_loop()
            self._initialized = True
            logger.info("[AsyncRuntime] 全局统一异步运行时初始化完毕。")

    @classmethod
    def get_instance(cls) -> "AsyncRuntime":
        return cls()

    def _ensure_loop(self) -> None:
        """自适应探测并启动主事件循环"""
        # 1. 尝试获取当前线程运行中的 Loop (如 Uvicorn)
        try:
            running_loop = asyncio.get_running_loop()
            if running_loop and running_loop.is_running():
                self._loop = running_loop
                self._loop_ready_event.set()
                logger.info("[AsyncRuntime] 成功绑定到当前线程的运行中事件循环 (Managed Loop Mode)")
                return
        except RuntimeError:
            pass

        # 2. 独立守护线程模式 (Daemon Thread Mode)
        def _run_event_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._loop_ready_event.set()
            logger.info("[AsyncRuntime] 后台常驻事件循环已启动 (Daemon Thread Mode)")
            try:
                loop.run_forever()
            finally:
                loop.close()

        self._daemon_thread = threading.Thread(target=_run_event_loop, daemon=True, name="UnifiedAsyncRuntime")
        self._daemon_thread.start()
        self._loop_ready_event.wait(timeout=5.0)

    def get_loop(self) -> asyncio.AbstractEventLoop:
        """获取当前统一主事件循环"""
        if self._loop is None or not self._loop_ready_event.is_set():
            self._ensure_loop()
        return self._loop

    def spawn_task(
        self,
        coro: Coroutine[Any, Any, Any],
        key: Optional[str] = None,
        strategy_id: str = "default",
        market_id: str = ""
    ) -> asyncio.Task:
        """
        统一提交异步协程至主事件循环执行，并注册到 TaskSupervisor。
        支持从任何线程安全调用。
        """
        loop = self.get_loop()
        task_key = key or f"task_{id(coro)}"

        # 检查是否就在当前 Loop 线程内
        try:
            curr_loop = asyncio.get_running_loop()
            if curr_loop is loop:
                task = loop.create_task(coro)
                self.supervisor.register_task(task_key, task, strategy_id=strategy_id, market_id=market_id)
                return task
        except RuntimeError:
            pass

        # 跨线程提交
        future = asyncio.run_coroutine_threadsafe(self._create_and_register(coro, task_key, strategy_id, market_id), loop)
        return future.result(timeout=5.0)

    async def _create_and_register(
        self, coro: Coroutine[Any, Any, Any], key: str, strategy_id: str, market_id: str
    ) -> asyncio.Task:
        """在 Loop 内部创建并注册 Task"""
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        self.supervisor.register_task(key, task, strategy_id=strategy_id, market_id=market_id)
        return task

    def run_coroutine_sync(self, coro: Coroutine[Any, Any, Any], timeout: float = 10.0) -> Any:
        """在外部同步线程中阻塞等待协程在主 Loop 中执行完成"""
        loop = self.get_loop()
        try:
            curr_loop = asyncio.get_running_loop()
            if curr_loop is loop:
                raise RuntimeError("不能在事件循环线程内部调用 run_coroutine_sync (会导致死锁)")
        except RuntimeError:
            pass

        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

import asyncio
import socket
import ctypes
import sys
import win32api
import win32process
from loguru import logger
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
from config import settings


class AsyncModbusConnection:
    """增强版实时Modbus连接池"""

    def __init__(self):
        self._connections = []
        self._lock = asyncio.Lock()
        self._initialized = False
        self._monitor_task = None
        self._init_realtime()

    def _init_realtime(self):
        """Windows实时环境初始化"""
        if sys.platform != "win32":
            return

        try:
            # 提高系统时钟精度
            self._winmm = ctypes.WinDLL('winmm')
            self._winmm.timeBeginPeriod(1)

            # 设置CPU亲和性
            if hasattr(settings, 'REALTIME_CPU_CORE'):
                mask = (1 << settings.REALTIME_CPU_CORE)
                ctypes.windll.kernel32.SetProcessAffinityMask(
                    ctypes.windll.kernel32.GetCurrentProcess(),
                    mask
                )
        except Exception as e:
            logger.error(f"实时初始化失败: {e}")

    async def initialize(self):
        """初始化连接池"""
        if not self._initialized:
            async with self._lock:
                if not self._initialized:
                    # 预创建所有连接
                    self._connections = await asyncio.gather(
                        *[self._create_connection(i)
                          for i in range(settings.CONNECTION_POOL_SIZE)],
                        return_exceptions=True
                    )

                    # 启动连接监控
                    self._monitor_task = asyncio.create_task(self._monitor_connections())
                    self._initialized = True
                    logger.info(f"连接池初始化完成，大小: {settings.CONNECTION_POOL_SIZE}")

    async def _create_connection(self, conn_id):
        """创建带实时优化的连接"""
        client = None
        try:
            # 每个连接使用不同本地端口
            local_port = settings.CLIENT_BASE_PORT + conn_id if hasattr(settings, 'CLIENT_BASE_PORT') else 0

            client = AsyncModbusTcpClient(
                host=settings.CONTROLLER_IP,
                port=settings.CONTROLLER_PORT,
                timeout=settings.RESPONSE_TIMEOUT,
                retries=settings.CONNECT_RETRIES,
                local_port=local_port,
                socket_options=[
                    (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
                    (socket.SOL_SOCKET, socket.SO_LINGER, 0),
                    (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                ]
            )

            # 带超时和重试的连接
            for attempt in range(1, settings.CONNECT_RETRIES + 1):
                try:
                    await asyncio.wait_for(
                        client.connect(),
                        timeout=settings.CONNECT_TIMEOUT
                    )
                    if client.connected:
                        logger.debug(f"连接{conn_id}建立成功")
                        return client
                    raise ConnectionError("连接状态异常")
                except (asyncio.TimeoutError, ModbusException) as e:
                    if attempt == settings.CONNECT_RETRIES:
                        raise
                    await asyncio.sleep(1)

        except Exception as e:
            if client:
                await client.close()
            logger.error(f"创建连接{conn_id}失败: {e}")
            raise

    async def get_connection(self, conn_id=None):
        """获取连接(支持指定连接ID或轮询获取)"""
        await self.initialize()

        async with self._lock:
            # 如果指定了conn_id且有效
            if conn_id is not None and 0 <= conn_id < len(self._connections):
                conn = self._connections[conn_id]
                if conn is None or not getattr(conn, 'connected', False):
                    self._connections[conn_id] = await self._create_connection(conn_id)
                return self._connections[conn_id]

            # 轮询获取第一个可用连接
            for i, conn in enumerate(self._connections):
                if conn is not None and getattr(conn, 'connected', False):
                    return conn

            # 无可用连接时创建新连接
            if len(self._connections) < settings.CONNECTION_POOL_SIZE:
                new_conn = await self._create_connection(len(self._connections))
                self._connections.append(new_conn)
                return new_conn

            raise ConnectionError("连接池已满且无可用连接")

    async def _monitor_connections(self):
        """连接健康监控"""
        while True:
            await asyncio.sleep(2)  # 每2秒检查一次

            async with self._lock:
                for i, conn in enumerate(self._connections):
                    if conn is None:
                        continue

                    try:
                        if not getattr(conn, 'connected', False):
                            logger.warning(f"连接{i}断开，尝试重连...")
                            self._connections[i] = await self._create_connection(i)
                    except Exception as e:
                        logger.error(f"连接{i}监控异常: {e}")

    async def close_all(self):
        """安全关闭所有连接"""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            close_tasks = []
            for conn in self._connections:
                if conn and hasattr(conn, 'close'):
                    close_tasks.append(conn.close())

            await asyncio.gather(*close_tasks, return_exceptions=True)

            if hasattr(self, '_winmm'):
                self._winmm.timeEndPeriod(1)

            self._connections = []
            self._initialized = False
            logger.info("所有连接已关闭")

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close_all()

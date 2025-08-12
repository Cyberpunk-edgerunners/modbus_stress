import asyncio
import socket
import ctypes
import sys
import time
import win32api
import win32process
from loguru import logger
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from config import settings

# Windows API 常量
PROCESS_ALL_ACCESS = 0x1F0FFF
REALTIME_PRIORITY_CLASS = 0x00000100
HIGH_PRIORITY_CLASS = 0x00000080

class AsyncModbusConnection:
    """异步Modbus连接池"""

    def __init__(self):
        self._connections = []
        self._lock = asyncio.Lock()
        self._initialized = False
        self._init_realtime()

    def _init_realtime(self):
        if sys.platform != "win32":
            return

        try:
            # 提高系统时钟精度
            self._winmm = ctypes.WinDLL('winmm')
            self._winmm.timeBeginPeriod(1)

            # 设置socket低延迟
            if settings.DISABLE_NAGLE:
                self._set_socket_options()

        except Exception as e:
            logger.warning(f"实时初始化失败：{e}")

    def _set_socket_options(self):
        """配置Socket低延迟参数"""
        socket.SO_LINGER = 0x0080
        socket.TCP_NODELAY = 0x0001
        logger.debug("已启用Socket低延迟配置")

    async def initialize(self):
        """初始化连接池"""
        if not self._initialized:
            async with self._lock:
                if not self._initialized:
                    self._connections = await asyncio.gather(
                        *[self._create_realtime_connection()
                          for _ in range(settings.CONNECTION_POOL_SIZE)]
                    )
                    self._initialized = True
                    logger.info(f"实时连接池初始化完成，大小: {settings.CONNECTION_POOL_SIZE}")

    async def _create_realtime_connection(self):
        """创建实时优化连接"""
        client = None
        try:
            client = AsyncModbusTcpClient(
                host=settings.CONTROLLER_IP,
                port=settings.CONTROLLER_PORT,
                timeout=settings.RESPONSE_TIMEOUT,
                retries=settings.CONNECT_RETRIES,
                socket_options=[
                    (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
                    (socket.SOL_SOCKET, socket.SO_LINGER, 0)
                ]
            )

            # 添加连接超时和重试机制
            for attempt in range(1, settings.CONNECT_RETRIES + 1):
                try:
                    await asyncio.wait_for(client.connect(),
                                           timeout=settings.CONNECT_TIMEOUT)
                    break
                except (asyncio.TimeoutError, ModbusException) as e:
                    if attempt == settings.CONNECT_RETRIES:
                        raise ConnectionError(
                            f"连接失败（尝试{attempt}次）: {str(e)}")
                    logger.warning(f"连接尝试{attempt}失败，重试中...")
                    await asyncio.sleep(1)

            if not client.connected:
                raise ConnectionError("连接状态异常")

            return self._wrap_realtime_client(client)

        except Exception as e:
            if client:
                await client.close()
            logger.error(f"创建实时连接失败: {e}")
            raise

    def _wrap_realtime_client(self, client):
        """包装客户端以添加实时监控"""
        original_send = client.protocol._send

        def patched_send(request):
            # 记录发送时间戳
            request.timestamp = time.perf_counter()
            return original_send(request)

        client.protocol._send = patched_send
        return client

    async def get_connection(self):
        """获取实时连接（带健康检查）"""
        await self.initialize()

        async with self._lock:
            for i, conn in enumerate(self._connections):
                try:
                    if conn is None or not getattr(conn, 'connected', False):
                        self._connections[i] = await self._create_realtime_connection()
                    return self._connections[i]
                except Exception as e:
                    logger.warning(f"连接{i}异常: {e}")
                    self._connections[i] = None

        raise ConnectionError("无法获取有效连接")

    async def _monitor_connections(self):
        """后台连接健康监测"""
        while True:
            await asyncio.sleep(5)  # 每5秒检查一次
            async with self._lock:
                for i, conn in enumerate(self._connections):
                    if conn is None:
                        continue

                    try:
                        if not conn.connected:
                            logger.warning(f"连接{i}已断开，尝试重连...")
                            self._connections[i] = await self._create_realtime_connection()
                    except Exception as e:
                        logger.error(f"连接{i}监测失败: {e}")

    async def close_all(self):
        """安全关闭所有连接"""
        async with self._lock:
            if not hasattr(self, '_connections'):
                return

            close_tasks = []
            for conn in self._connections:
                if conn and hasattr(conn, 'close'):
                    close_tasks.append(conn.close())

            await asyncio.gather(*close_tasks, return_exceptions=True)

            # 恢复系统设置
            if hasattr(self, '_winmm'):
                self._winmm.timeEndPeriod(1)

            self._connections = []
            self._initialized = False
            logger.info("所有实时连接已关闭")

    def __enter__(self):
        raise TypeError("请使用异步上下文管理器")

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close_all()

"""        
    async def _create_connection(self):
        # 创建新连接
        try:
            client = AsyncModbusTcpClient(
                host=settings.CONTROLLER_IP,
                port=settings.CONTROLLER_PORT,
                timeout=settings.RESPONSE_TIMEOUT,
                retries=settings.CONNECT_RETRIES
            )
            await client.connect()

            # 新方法检查连接状态
            if not client.connected:
                raise ConnectionError("连接未建立")

            logger.debug("创建新连接成功")
            return client
        except Exception as e:
            logger.error(f"连接创建失败: {e}")
            raise

    async def get_connection(self):
        # 获取长连接（带重试机制）
        await self.initialize()

        max_retries = 3
        for attempt in range(max_retries):
            for i, conn in enumerate(self._connections):
                try:
                    if conn is None:
                        continue

                    if not hasattr(conn, 'connected'):
                        logger.warning(f"无效连接对象: {type(conn)}")
                        self._connections[i] = None
                        continue

                    if conn.connected:
                        return conn

                except Exception as e:
                    logger.error(f"检查连接状态出错: {e}")
                    self._connections[i] = None

            # 所有连接都不可用，尝试重建
            if attempt < max_retries - 1:
                logger.warning(f"无可用连接，尝试重建... (尝试 {attempt + 1}/{max_retries})")
                await asyncio.sleep(1)
                await self.initialize()

        raise ConnectionError("无法获取有效连接")

    async def close_all(self):
        async with self._lock:
            if not hasattr(self, '_connections'):
                logger.warning("连接池已被清空，无需关闭")
                return

            for i in range(len(self._connections)):
                conn = self._connections[i]
                try:
                    # 使用getattr安全检查，避免属性错误
                    if conn is None:
                        logger.debug(f"连接{i}已为None，跳过")
                        continue

                    if not getattr(conn, 'connected', False):
                        logger.debug(f"连接{i}已断开，无需关闭")
                        continue

                    # 终极保护：检查是否可await
                    if hasattr(conn, 'close') and callable(getattr(conn, 'close', None)):
                        await conn.close()
                        logger.debug(f"连接{i}已关闭")
                    else:
                        logger.warning(f"连接{i}没有可调用的close方法")

                except Exception as e:
                    logger.error(f"关闭连接{i}时出错: {type(e).__name__}: {str(e)}")
                finally:
                    # 确保设置为None
                    self._connections[i] = None

            self._connections = []
            self._initialized = False
            logger.info("连接池已完全关闭")

"""
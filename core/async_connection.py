import asyncio
import socket
from loguru import logger
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
from config import settings


class AsyncModbusConnection:
    """异步Modbus连接池"""

    def __init__(self):
        self._connections = []
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self):
        """初始化连接池"""
        if not self._initialized:
            async with self._lock:
                if not self._initialized:
                    self._connections = [
                        await self._create_connection()
                        for _ in range(settings.CONNECTION_POOL_SIZE)
                    ]
                    self._initialized = True
                    logger.info(f"连接池初始化完成，大小: {settings.CONNECTION_POOL_SIZE}")

    async def _create_connection(self):
        """创建新连接"""
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
        """获取长连接（带重试机制）"""
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
        """终极安全关闭方法"""
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




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
        """获取长连接"""
        await self.initialize()
        while True:
            for conn in self._connections:
                if conn.connected:
                    return conn
            logger.warning("无可用连接，尝试重建...")
            await asyncio.sleep(1)
            await self.initialize()

    async def close_all(self):
        """安全关闭所有连接"""
        async with self._lock:
            for i in range(len(self._connections)):
                conn = self._connections[i]
                try:
                    if conn and hasattr(conn, 'close') and conn.connected:
                        await conn.close()
                except Exception as e:
                    logger.error(f"关闭连接时出错: {e}")
                finally:
                    self._connections[i] = None
            self._connections = []
            self._initialized = False
            logger.info("所有连接已关闭")


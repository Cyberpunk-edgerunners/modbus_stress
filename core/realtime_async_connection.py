import asyncio
import time
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
        """连接池初始化"""
        if not self._initialized:
            async with self._lock:
                if not self._initialized:
                    self._connections = []
                    success_count = 0

                    # 逐个创建连接，明确处理结果
                    for i in range(settings.CONNECTION_POOL_SIZE):
                        try:
                            conn = await self._create_connection(i)
                            if conn and conn.connected:
                                self._connections.append(conn)
                                success_count += 1
                                logger.success(f"连接{i}初始化成功")
                            else:
                                self._connections.append(None)
                                logger.warning(f"连接{i}创建后状态异常")
                        except Exception as e:
                            self._connections.append(None)  # 确保填充None
                            logger.error(f"连接{i}初始化失败: {str(e)}", exc_info=True)

                    # 关键验证
                    if success_count == 0:
                        raise RuntimeError("所有连接初始化失败")

                    self._monitor_task = asyncio.create_task(self._monitor_connections())
                    self._initialized = True
                    logger.success(
                        f"连接池就绪 | 总计: {settings.CONNECTION_POOL_SIZE} | "
                        f"可用: {success_count}"
                    )

    async def _create_connection(self, conn_id):
        """创建带实时优化的异步连接"""
        client = None
        try:
            # 基础连接参数
            client_params = {
                'host': settings.CONTROLLER_IP,
                'port': settings.CONTROLLER_PORT,
                'timeout': settings.RESPONSE_TIMEOUT,
                'retries': settings.CONNECT_RETRIES,
            }

            # 设置本地绑定端口
            if hasattr(settings, 'CLIENT_BASE_PORT'):
                client_params['source_address'] = (
                    '0.0.0.0',
                    settings.CLIENT_BASE_PORT + conn_id
                )

            # 创建客户端实例
            client = AsyncModbusTcpClient(**client_params)
            logger.debug(f"连接{conn_id}客户端创建成功")

            # === 正确的异步连接流程 ===
            connected = False
            for attempt in range(1, settings.CONNECT_RETRIES + 1):
                try:
                    logger.debug(f"连接{conn_id}异步尝试#{attempt}...")

                    # 异步连接（必须await）
                    await client.connect()

                    # 验证连接状态
                    if client.connected:
                        connected = True
                        logger.success(f"连接{conn_id}建立成功")
                        break
                    else:
                        logger.warning(f"连接{conn_id}状态异常")
                        await asyncio.sleep(0.5)  # 异步等待

                except Exception as e:
                    logger.warning(f"连接{conn_id}尝试#{attempt}失败: {str(e)}")
                    await asyncio.sleep(1)  # 异步等待

            if not connected:
                raise ConnectionError(f"连接{conn_id}无法建立")

            # 打印连接详情
            logger.success(
                f"连接{conn_id}已就绪 | "
                f"本地端口: {client.comm_params.source_address[1]} | "
                f"远端: {client.comm_params.host}:{client.comm_params.port}"
            )
            return client

        except Exception as e:
            if client is not None:
                await client.close()  # 异步关闭
            logger.error(f"创建连接{conn_id}失败: {str(e)}", exc_info=True)
            raise

    async def get_connection(self, conn_id=None):
        """获取连接(支持指定连接ID或轮询获取)"""
        # 确保连接池已初始化
        if not self._initialized:
            await self.initialize()

        async with self._lock:
            # 优先处理指定连接ID
            if conn_id is not None:
                if 0 <= conn_id < len(self._connections):
                    conn = self._connections[conn_id]
                    # 修复无效连接
                    if conn is None or not getattr(conn, 'connected', False):
                        try:
                            self._connections[conn_id] = await self._safe_create_connection(conn_id)
                            conn = self._connections[conn_id]
                        except Exception as e:
                            logger.error(f"连接{conn_id}修复失败: {str(e)}")
                            raise ConnectionError(f"连接{conn_id}不可用") from e
                    return conn
                raise IndexError(f"无效连接ID: {conn_id}")

            # 自动分配模式：优先返回有效连接
            for i, conn in enumerate(self._connections):
                if conn and getattr(conn, 'connected', False):
                    return conn

            # 尝试修复失效连接（第一次修复尝试）
            for i, conn in enumerate(self._connections):
                if conn is None or not getattr(conn, 'connected', False):
                    try:
                        self._connections[i] = await self._safe_create_connection(i)
                        if self._connections[i] and self._connections[i].connected:
                            return self._connections[i]
                    except:
                        pass  # 首次修复失败暂不处理

            # 终极验证：连接池真满还是假满
            active_conns = [c for c in self._connections if c and getattr(c, 'connected', False)]
            if active_conns:
                return active_conns[0]  # 返回首个可用连接

            raise ConnectionError("连接池无可用连接")

    async def _monitor_connections(self):
        """鲁棒的连接监控"""
        logger.info("连接监控任务启动")
        while True:
            try:
                await asyncio.sleep(settings.MONITOR_INTERVAL)

                async with self._lock:
                    for i, conn in enumerate(self._connections):
                        try:
                            # 处理未初始化连接
                            if conn is None:
                                logger.warning(f"连接{i}未初始化，尝试创建...")
                                self._connections[i] = await self._safe_create_connection(i)
                                continue

                            # 检查连接活性
                            if not await self._check_connection_active(conn):
                                logger.warning(f"连接{i}失效，重建中...")
                                await conn.close()
                                self._connections[i] = await self._safe_create_connection(i)

                        except Exception as e:
                            logger.error(f"连接{i}监控异常: {str(e)}", exc_info=True)
                            # 确保位置标记为无效
                            self._connections[i] = None

            except asyncio.CancelledError:
                logger.info("连接监控任务正常终止")
                break
            except Exception as e:
                logger.critical(f"监控任务崩溃: {str(e)}", exc_info=True)
                await asyncio.sleep(5)  # 防止错误风暴

    async def _check_connection_active(self, conn):
        """深度连接活性检测"""
        if not conn or not getattr(conn, 'connected', False):
            return False

        try:
            # 发送心跳请求验证
            rr = await asyncio.wait_for(
                conn.read_holding_registers(0, 1),
                timeout=1.0
            )
            return not rr.isError()
        except Exception:
            return False

    async def _safe_create_connection(self, conn_id):
        """带异常隔离的连接创建"""
        try:
            return await self._create_connection(conn_id)
        except Exception as e:
            logger.error(f"创建连接{conn_id}失败: {str(e)}", exc_info=True)
            return None  # 确保返回可控值

    async def validate_pool_health(self):
        """连接池健康诊断"""
        if not self._initialized:
            await self.initialize()

        active_count = 0
        for i, conn in enumerate(self._connections):
            if conn and getattr(conn, 'connected', False):
                active_count += 1
            elif conn is None:
                logger.warning(f"连接{i}未初始化")
            else:
                logger.error(f"连接{i}状态异常")

        logger.info(f"连接池健康检查: {active_count}/{len(self._connections)} 活跃")
        return active_count > 0  # 至少一个有效连接

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

import os
import socket
import threading
from turtledemo.penrose import start
import time

from pymodbus import ModbusException
from pymodbus.client import ModbusTcpClient
from loguru import logger
from config import settings


class ModbusConnectionPool:
    """线程安全的Modbus TCP连接池"""

    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance.__initialized = False
        return cls._instance

    def __init__(self):
        if self.__initialized:
            return
        self.__initialized = True

        # 初始化连接池属性
        self._pool = []
        self._lock = threading.Lock()
        self._host = settings.CONTROLLER_IP
        self._port = settings.CONTROLLER_PORT
        self._size = settings.CONNECTION_POOL_SIZE
        self._initialize_pool()

    def _initialize_pool(self):
        """初始化连接池"""
        with self._lock:
            for _ in range(self._size):
                conn = self._create_connection()
                if conn:
                    self._pool.append(conn)
            logger.info(f"连接池初始化完成，当前连接数: {len(self._pool)}")

    def _create_connection(self):
        """兼容 Windows 和 pymodbus 3.10 的连接创建"""
        client = None
        try:
            client = ModbusTcpClient(
                host=self._host,
                port=self._port,
                timeout=settings.CONNECT_TIMEOUT
            )

            # 先建立连接再设置参数
            if not client.connect():
                raise ConnectionError("TCP连接失败")

            # 设置 Keepalive（兼容性写法）
            if hasattr(client, 'socket') and client.socket is not None:
                sock = client.socket
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

                # Windows 特殊处理
                if os.name == 'nt':
                    try:
                        # 不同Windows版本的ioctl参数可能不同
                        sock.ioctl(socket.SIO_KEEPALIVE_VALS,
                                   (1, 10000, 3000))  # 1=启用, 10s空闲, 3s间隔
                    except AttributeError:
                        logger.warning("当前系统不支持高级Keepalive设置")

            # 测试连接有效性
            test_result = client.read_holding_registers(0, 1)
            if test_result.isError():
                raise ModbusException("测试读取失败")

            logger.success(f"连接成功 {self._host}:{self._port}")
            return client

        except Exception as e:
            logger.error(f"连接异常: {type(e).__name__} - {str(e)}")
            if client:
                client.close()
            return None

    def get_connection(self):
        """获取连接（带连接测试）"""
        logger.debug(f"尝试获取锁，当前锁状态: {self._lock.locked()}")
        with self._lock:
            if not self._pool:
                new_conn = self._create_connection()
                if not new_conn or not self._test_connection(new_conn):
                    raise ConnectionError("无法获取有效连接")
                return new_conn

            conn = self._pool.pop()
            if not self._test_connection(conn):  # 取出时验证
                conn.close()
                return self.get_connection()  # 递归获取新连接
            return conn

    def _test_connection(self, conn):
        """验证连接有效性"""
        try:
            return conn.read_holding_registers(0, 1).isError() == False
        except:
            return False

    def release_connection(self, conn):
        """释放连接 (带状态检查)"""
        if conn is None:
            return

        try:
            # 检查连接是否有效
            test_result = conn.read_holding_registers(address=0, count=1)
            is_valid = not test_result.isError()
        except Exception as e:
            is_valid = False
            logger.warning(f"连接测试失败: {e}")

        with self._lock:
            if is_valid and len(self._pool) < self._size:
                self._pool.append(conn)
                logger.debug("连接已回收")
            else:
                try:
                    conn.close()
                    logger.debug("无效连接已关闭")
                except Exception as e:
                    logger.error(f"连接关闭异常: {e}")

    def __del__(self):
        """析构时关闭所有连接"""
        with self._lock:
            for conn in self._pool:
                try:
                    conn.close()
                except:
                    pass
            self._pool.clear()
            logger.info("连接池已销毁")

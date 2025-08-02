import threading
from pymodbus.client import ModbusTcpClient
from loguru import logger
from config import settings
import socket
import time


class ModbusConnectionPool:
    """Modbus TCP连接池"""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init_pool()
        return cls._instance

    def _init_pool(self):
        self._pool = []
        self._lock = threading.Lock()
        self._host = settings.CONTROLLER_IP
        self._port = settings.CONTROLLER_PORT
        self._size = settings.CONNECTION_POOL_SIZE
        self._initialize_pool()

    def _initialize_pool(self):
        """初始化连接池"""
        for _ in range(self._size):
            conn = self._create_connection()
            if conn:
                self._pool.append(conn)

    def _create_connection(self):
        """创建新连接 (最小参数兼容版)"""
        retry_count = 0
        max_retries = 3

        while retry_count < max_retries:
            client = None
            try:
                # 使用最小参数集创建客户端
                client = ModbusTcpClient(
                    host=self._host,
                    port=self._port,
                    timeout=settings.CONNECT_TIMEOUT  # 仅使用基础timeout参数
                )

                # 手动设置socket选项
                if client.connect():
                    sock = client.socket if hasattr(client, 'socket') else None
                    if sock and settings.DISABLE_NAGLE:
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                    # 简单心跳测试
                    try:
                        test_result = client.read_input_registers(address=0, count=1)
                        if not test_result.isError():
                            logger.success(f"成功建立稳定连接 {self._host}:{self._port}")
                            return client
                    except Exception as test_e:
                        logger.warning(f"心跳测试失败: {str(test_e)}")

                logger.warning(f"连接不稳定，重试 {retry_count + 1}/{max_retries}")
                if client:
                    client.close()

            except Exception as e:
                logger.error(f"连接异常: {str(e)}")
                if client:
                    client.close()

            retry_count += 1
            time.sleep(1)  # 重试间隔

        logger.error("无法建立有效连接")
        return None

    def get_connection(self):
        """获取连接"""
        with self._lock:
            if not self._pool:
                new_conn = self._create_connection()
                if new_conn:
                    return new_conn
                raise ConnectionError("无法创建新连接")
            return self._pool.pop()

    def release_connection(self, conn):
        """释放连接"""
        with self._lock:
            if len(self._pool) < self._size and conn.is_socket_open():
                self._pool.append(conn)
            else:
                conn.close()

    def close_all(self):
        """关闭所有连接"""
        with self._lock:
            for conn in self._pool:
                conn.close()
            self._pool.clear()

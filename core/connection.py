import socket
import threading
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

    # def _create_connection(self):
    #     """创建新连接"""
    #     try:
    #         # 创建原生socket手动控制
    #         sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    #         sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    #         sock.settimeout(settings.CONNECT_TIMEOUT)
    #
    #         # 建立连接
    #         sock.connect((self._host, self._port))
    #
    #         # 创建Modbus客户端并注入socket
    #         client = ModbusTcpClient(
    #             host=self._host,
    #             port=self._port,
    #             timeout=settings.RESPONSE_TIMEOUT
    #         )
    #
    #         # 不同版本属性名兼容
    #         for attr in ['socket', '_socket', 'connection']:
    #             if hasattr(client, attr):
    #                 setattr(client, attr, sock)
    #                 break
    #
    #         logger.success(f"成功创建连接到 {self._host}:{self._port}")
    #         return client
    #
    #     except Exception as e:
    #         logger.error(f"创建连接失败: {type(e).__name__}: {str(e)}")
    #         if 'sock' in locals():
    #             sock.close()
    #         return None

    def _create_connection(self):
        """创建连接（兼容对端限制）"""
        try:
            client = ModbusTcpClient(
                host=self._host,
                port=self._port,
                timeout=settings.CONNECT_TIMEOUT
            )

            # 添加连接验证
            if client.connect():
                # 测试读取保持寄存器（地址0）
                test_result = client.read_holding_registers(address=0, count=1)
                if not test_result.isError():
                    logger.success(f"验证连接成功 {self._host}:{self._port}")
                    return client
            client.close()
        except Exception as e:
            logger.error(f"连接异常: {str(e)}")
        return None

    def get_connection(self):
        """获取连接 (线程安全)"""
        with self._lock:
            if not self._pool:
                new_conn = self._create_connection()
                if new_conn:
                    return new_conn
                raise ConnectionError("无法创建新连接")
            return self._pool.pop()

    def release_connection(self, conn):
        """释放连接 (线程安全)"""
        if conn is None:
            return

        with self._lock:
            if len(self._pool) < self._size:
                self._pool.append(conn)
                logger.debug("连接已回收")
            else:
                conn.close()
                logger.debug("连接已关闭")

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

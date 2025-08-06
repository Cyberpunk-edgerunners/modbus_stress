import socket
import threading
import time
import ctypes
from loguru import logger
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from config import settings


class ModbusConnectionPool:
    """支持长连接模式的线程安全连接池（适配PyModbus 3.x）"""

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

        # 连接池配置
        self._host = settings.CONTROLLER_IP
        self._port = settings.CONTROLLER_PORT
        self._size = settings.CONNECTION_POOL_SIZE
        self._lock = threading.Lock()
        self._pool = []

        # 长连接专用属性
        self._persistent_conn = None
        self._persistent_lock = threading.Lock()
        self._last_heartbeat = 0

    def _setup_socket_options(self, sock):
        """配置Socket参数（Windows/Linux通用）"""
        try:
            # 禁用Nagle算法（关键修改点）
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, int(settings.DISABLE_NAGLE))

            # 启用KeepAlive
            if hasattr(socket, 'SO_KEEPALIVE'):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                # Windows特定配置
                if hasattr(ctypes, 'windll'):
                    try:
                        sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 3000))  # 10s空闲,3s间隔
                    except:
                        logger.warning("无法设置Windows KeepAlive参数")
        except Exception as e:
            logger.error(f"Socket配置失败: {e}")

    def _create_connection(self, persistent=False):
        """创建新连接（适配PyModbus 3.x API）"""
        try:
            # 创建原生socket并配置
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._setup_socket_options(sock)
            sock.settimeout(settings.CONNECT_TIMEOUT)
            sock.connect((self._host, self._port))

            # 创建Modbus客户端（关键修改：移除no_delay参数）
            client = ModbusTcpClient(
                host=self._host,
                port=self._port,
                timeout=settings.RESPONSE_TIMEOUT
            )

            # 手动注入预配置的socket（PyModbus 3.x兼容方案）
            if hasattr(client, 'socket'):
                client.socket = sock
            elif hasattr(client, '_socket'):
                client._socket = sock
            else:
                logger.warning("无法注入预配置socket，连接可能不符合参数要求")

            # 显式连接（PyModbus 3.x要求）
            if not client.connect():
                raise ConnectionError("连接失败")

            # 长连接需验证
            if persistent and not self._test_connection(client):
                raise ConnectionError("长连接验证失败")

            logger.success(f"连接建立 {self._host}:{self._port} [{'持久' if persistent else '临时'}]")
            return client

        except Exception as e:
            logger.error(f"连接创建失败: {e}")
            if 'sock' in locals():
                sock.close()
            if 'client' in locals():
                client.close()
            return None

    def _test_connection(self, conn):
        """测试连接有效性（优化版心跳检测）"""
        try:
            start_time = time.time()
            result = conn.read_holding_registers(address=0, count=1)
            latency = (time.time() - start_time) * 1000
            if latency > 100:  # 记录慢速心跳
                logger.warning(f"心跳延迟过高: {latency:.2f}ms")
            return not result.isError()
        except Exception as e:
            logger.debug(f"心跳检测失败: {e}")
            return False

    def get_persistent_connection(self):
        """获取/维持长连接（带自动重连和熔断机制）"""
        with self._persistent_lock:
            # 存在可用连接时直接返回
            if self._persistent_conn and self._test_connection(self._persistent_conn):
                self._last_heartbeat = time.time()
                return self._persistent_conn

            # 需要重建连接
            retry_count = 0
            while retry_count < 3:  # 最大重试3次
                self._persistent_conn = self._create_connection(persistent=True)
                if self._persistent_conn:
                    self._last_heartbeat = time.time()
                    return self._persistent_conn

                retry_count += 1
                time.sleep(2 ** retry_count)  # 指数退避

            raise ConnectionError("长连接建立失败，已达最大重试次数")

    # 短连接池方法保持不变
    def get_connection(self):
        """从池中获取短连接"""
        with self._lock:
            return self._pool.pop() if self._pool else self._create_connection()

    def release_connection(self, conn):
        """释放短连接回池（带健康检查）"""
        if conn is None:
            return

        with self._lock:
            if len(self._pool) < self._size and self._test_connection(conn):
                self._pool.append(conn)
            else:
                try:
                    conn.close()
                except:
                    pass

    def check_persistent_connection(self):
        """增强版长连接检查（带自动恢复）"""
        with self._persistent_lock:
            if not self._persistent_conn:
                return False

            # 心跳超时检测
            if time.time() - self._last_heartbeat > 30:
                if not self._test_connection(self._persistent_conn):
                    logger.warning("长连接异常，触发自动恢复...")
                    try:
                        self._persistent_conn.close()
                    except:
                        pass
                    self._persistent_conn = self._create_connection(persistent=True)

                self._last_heartbeat = time.time()

            return self._persistent_conn is not None

    # 其他方法保持不变...

    def close_persistent_connection(self):
        """主动关闭长连接"""
        with self._persistent_lock:
            if self._persistent_conn:
                self._persistent_conn.close()
                self._persistent_conn = None

    def __del__(self):
        """析构时清理所有连接"""
        with self._lock:
            for conn in self._pool:
                try:
                    conn.close()
                except:
                    pass
            self._pool.clear()

        self.close_persistent_connection()
        logger.info("连接池已销毁")

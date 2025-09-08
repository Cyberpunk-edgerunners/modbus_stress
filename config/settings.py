import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

# 网络配置
# CONTROLLER_IP = os.getenv("CONTROLLER_IP", "192.168.2.100")
# CONTROLLER_PORT = int(os.getenv("CONTROLLER_PORT", 502))

CONTROLLER_IP = "192.168.2.100"  # 直接写死IP，不使用环境变量
CONTROLLER_PORT = 502
CLIENT_BASE_PORT = 10000  # 客户端起始端口

# 客户端数量配置
CLIENT_COUNT = 1  # 可配置1-4个客户端

# 连接池配置
CONNECTION_POOL_SIZE = CLIENT_COUNT
CONNECT_TIMEOUT = 5.0
CONNECT_RETRIES = 3
RESPONSE_TIMEOUT = 60.0  # 响应超时
MONITOR_INTERVAL = 0  # 连接监控间隔(秒)


# 测试配置
TEST_DURATION = timedelta(hours=0.001).total_seconds()

# 性能配置
BUSY_WAIT_PRECISION = 0.0001  # 1ms
TARGET_FREQUENCY = 500  #500HZ(2ms)

# 新增实时调度配置
REALTIME_PRIORITY = True  # 是否启用实时优先级
REALTIME_CPU_CORE = 3     # 绑定到指定CPU核心

MAX_REGISTERS_PER_READ = 120  # 单次最多读取寄存器数量
MAX_REGISTERS_PER_WRITE = 10  # 单次最多写入寄存器数量
DISABLE_NAGLE = True  # 禁用Nagle算法

# 寄存器配置
INPUT_REGISTER_RANGE = (0, 9)
# HOLDING_REGISTER_RANGE = (0, 999)

# 多主站测试配置
MASTER_CONFIGS = {
    "master_1": {
        "description": "主站1",
        "HOLDING_REGISTER_RANGE": (0,499),
        "disconnect_prob": 0,
        "reconnect_delay": (0, 0),
        "cycle_time": 0.001
    },
    "master_2": {
        "description": "主站2",
        "HOLDING_REGISTER_RANGE": (500, 999),
        "disconnect_prob": 0,
        "reconnect_delay": (60, 300),
        "cycle_time": 0.001
    },
    "master_3": {
        "description": "主站3",
        "HOLDING_REGISTER_RANGE": (1000, 1499),
        "disconnect_prob": 0,
        "cycle_time": 0.001
    },
    "master_4": {
        "description": "主站4",
        "HOLDING_REGISTER_RANGE": (1500, 1999),
        "disconnect_prob": 0,
        "cycle_time": 0.001
    }
}

PACKET_CAPTURE_DURATION = timedelta(minutes=1).total_seconds()  # 抓包持续时间
CAPTURE_FILE_ENCODING = "utf-8"

# 日志配置
LOG_LEVEL = "DEBUG"
LOG_ROTATION = "100 MB"  # 日志轮转大小



import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

# 网络配置
# CONTROLLER_IP = os.getenv("CONTROLLER_IP", "192.168.2.100")
# CONTROLLER_PORT = int(os.getenv("CONTROLLER_PORT", 502))

CONTROLLER_IP = "192.168.2.100"  # 直接写死IP，不使用环境变量
CONTROLLER_PORT = 502

# 测试配置
TEST_DURATION = timedelta(hours=12).total_seconds()

# 性能配置
BUSY_WAIT_PRECISION = 0.001  # 1ms
MAX_REGISTERS_PER_READ = 120  # 单次最多读取寄存器数量
MAX_REGISTERS_PER_WRITE = 120  # 单次最多写入寄存器数量
DISABLE_NAGLE = True  # 禁用Nagle算法

# 寄存器配置
INPUT_REGISTER_RANGE = (0, 9)
HOLDING_REGISTER_RANGE = (0, 999)

# 多主站测试配置
MASTER_CONFIGS = {
    "master_1": {
        "description": "随机断开立即重连",
        "disconnect_prob": 0.01,
        "reconnect_delay": (0, 0),
        "cycle_time": None
    },
    "master_2": {
        "description": "随机断开延迟重连",
        "disconnect_prob": 0.005,
        "reconnect_delay": (60, 300),
        "cycle_time": None
    },
    "master_3": {
        "description": "50ms周期长帧",
        "disconnect_prob": 0,
        "cycle_time": 0.05
    },
    "master_4": {
        "description": "1ms周期忙等待",
        "disconnect_prob": 0,
        "cycle_time": 0.001
    }
}

# 连接池配置
CONNECTION_POOL_SIZE = 3
CONNECT_TIMEOUT = 3.0
CONNECT_RETRIES = 3
RESPONSE_TIMEOUT = 2.0  # 响应超时

# 日志配置
LOG_LEVEL = "DEBUG"
LOG_ROTATION = "100 MB"  # 日志轮转大小

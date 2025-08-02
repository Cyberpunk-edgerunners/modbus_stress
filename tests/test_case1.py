import pytest
from core.client import HighPrecisionModbusClient
from config import settings

def test_1ms_precision_modbus():
    """测试用例1: 1ms精度的Modbus压力测试"""
    client = HighPrecisionModbusClient()
    client.run_test(settings.TEST_DURATION, use_busy_wait=True)

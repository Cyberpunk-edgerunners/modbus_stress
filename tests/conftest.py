# tests/conftest.py
import pytest
from core.client import HighPrecisionModbusClient
from config import settings

@pytest.fixture
def modbus_client():
    """提供Modbus客户端实例的fixture"""
    client = HighPrecisionModbusClient()
    yield client
    client.pool.close_all()

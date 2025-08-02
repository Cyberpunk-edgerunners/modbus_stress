import pytest
import threading
import random
import time
from core.client import HighPrecisionModbusClient
from config import settings
from loguru import logger


class TestMultiMasterScenario:
    @pytest.fixture(scope="class")
    def clients(self):
        return [HighPrecisionModbusClient() for _ in range(4)]

    def test_multiple_masters(self, clients):
        """测试用例2: 多主站混合场景测试"""

        def master_worker(client_id, client):
            config = settings.MASTER_CONFIGS[f"master_{client_id + 1}"]
            logger.info(f"启动主站{client_id + 1}: {config['description']}")

            end_time = time.time() + settings.TEST_DURATION
            while time.time() < end_time:
                # 随机断开逻辑
                if (config["disconnect_prob"] > 0 and
                        random.random() < config["disconnect_prob"]):
                    client.pool.close_all()
                    delay = random.uniform(*config["reconnect_delay"])
                    time.sleep(delay)

                # 执行测试
                client.run_test(
                    duration=1,  # 每次运行1秒
                    use_busy_wait=(client_id == 3)  # 只有主站4使用忙等待
                )

                # 周期控制
                if config["cycle_time"] and client_id != 3:
                    time.sleep(config["cycle_time"])

        threads = []
        for i, client in enumerate(clients):
            t = threading.Thread(
                target=master_worker,
                args=(i, client),
                name=f"Master-{i + 1}"
            )
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

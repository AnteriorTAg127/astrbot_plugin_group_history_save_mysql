"""ConfigManager 单元测试。"""

import asyncio
import os
import sys
import tempfile

# 添加 data/plugins 父目录到路径（v0.6.0 包化：from astrbot_plugin_group_history_save_mysql.core...）
_plugin_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.dirname(_plugin_root) not in sys.path:
    sys.path.insert(0, os.path.dirname(_plugin_root))

# Mock astrbot 模块
class MockLogger:
    def info(self, *args, **kwargs): pass
    def error(self, *args, **kwargs): pass
    def warning(self, *args, **kwargs): pass

class MockModule:
    logger = MockLogger()

sys.modules['astrbot'] = type(sys)('astrbot')
sys.modules['astrbot.api'] = MockModule()
sys.modules['astrbot.core'] = type(sys)('astrbot.core')
sys.modules['astrbot.core.utils'] = type(sys)('astrbot.core.utils')
sys.modules['astrbot.core.utils.astrbot_path'] = type(sys)('astrbot.core.utils.astrbot_path')

# 设置临时数据目录
temp_dir = tempfile.mkdtemp()

# v0.6.0 core/db_config 经 StarTools.get_data_dir 取规范数据目录（返回 Path）
class MockStarTools:
    @staticmethod
    def get_data_dir(plugin_name=None):
        from pathlib import Path
        return Path(temp_dir)

sys.modules['astrbot.api.star'] = type(sys)('astrbot.api.star')
sys.modules['astrbot.api.star'].StarTools = MockStarTools
sys.modules['astrbot.core.utils.astrbot_path'].get_astrbot_plugin_data_path = lambda: temp_dir

# 现在导入被测模块（v0.6.0 由顶层 db_config 迁移至 core.db_config 包）
from astrbot_plugin_group_history_save_mysql.core.db_config import ConfigManager


async def run_tests():
    results = []
    
    # TC-001: 初始化
    print("TC-001: ConfigManager 初始化...", end=" ")
    mgr = ConfigManager()
    ok = await mgr.initialize()
    db_exists = os.path.exists(mgr.db_path)
    if ok and db_exists:
        print("✅ PASS")
        results.append(("TC-001", True, "初始化成功，数据库文件已创建"))
    else:
        print("❌ FAIL")
        results.append(("TC-001", False, f"ok={ok}, db_exists={db_exists}"))

    # TC-002: 添加群
    print("TC-002: 添加群到白名单...", end=" ")
    ok = await mgr.add_group(123456)
    groups = await mgr.get_groups()
    found = any(g["group_id"] == 123456 for g in groups)
    if ok and found:
        print("✅ PASS")
        results.append(("TC-002", True, "群已添加"))
    else:
        print("❌ FAIL")
        results.append(("TC-002", False, f"ok={ok}, found={found}"))

    # TC-003: 切换群状态
    print("TC-003: 群白名单开关切换...", end=" ")
    new_state = await mgr.toggle_group(123456)
    if new_state == False:  # 从 True 切换到 False
        print("✅ PASS")
        results.append(("TC-003", True, "状态已切换为 False"))
    else:
        print("❌ FAIL")
        results.append(("TC-003", False, f"new_state={new_state}"))

    # 切换回来
    await mgr.toggle_group(123456)

    # TC-004: is_group_enabled 白名单模式
    print("TC-004: is_group_enabled 白名单模式...", end=" ")
    enabled = await mgr.is_group_enabled(123456)
    not_enabled = await mgr.is_group_enabled(999999)
    if enabled and not not_enabled:
        print("✅ PASS")
        results.append(("TC-004", True, "白名单群启用，非白名单群禁用"))
    else:
        print("❌ FAIL")
        results.append(("TC-004", False, f"enabled={enabled}, not_enabled={not_enabled}"))

    # TC-005: is_group_enabled ALL 模式
    print("TC-005: is_group_enabled ALL 模式...", end=" ")
    await mgr.set_setting("all_mode", "true")
    enabled_any = await mgr.is_group_enabled(999999)
    await mgr.set_setting("all_mode", "false")  # 恢复
    if enabled_any:
        print("✅ PASS")
        results.append(("TC-005", True, "ALL 模式下任意群都启用"))
    else:
        print("❌ FAIL")
        results.append(("TC-005", False, f"enabled_any={enabled_any}"))

    # TC-006: 设置读写
    print("TC-006: 设置读写...", end=" ")
    await mgr.set_setting("test_key", "test_value")
    value = await mgr.get_setting("test_key", "default")
    if value == "test_value":
        print("✅ PASS")
        results.append(("TC-006", True, "设置读写正确"))
    else:
        print("❌ FAIL")
        results.append(("TC-006", False, f"value={value}"))

    # TC-007: 移除群
    print("TC-007: 移除群...", end=" ")
    ok = await mgr.remove_group(123456)
    groups = await mgr.get_groups()
    found = any(g["group_id"] == 123456 for g in groups)
    if ok and not found:
        print("✅ PASS")
        results.append(("TC-007", True, "群已移除"))
    else:
        print("❌ FAIL")
        results.append(("TC-007", False, f"ok={ok}, found={found}"))

    # 清理
    await mgr.close()

    # 汇总
    print("\n" + "=" * 50)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"测试结果: {passed}/{total} 通过")
    for tc, ok, msg in results:
        status = "✅" if ok else "❌"
        print(f"  {status} {tc}: {msg}")
    
    return passed == total


if __name__ == "__main__":
    success = asyncio.run(run_tests())
    sys.exit(0 if success else 1)

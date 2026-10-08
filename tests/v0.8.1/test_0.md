# 测试 — v0.8.1 main.py 职责下沉重构（回归验证）

> 验证重点：注册性内容零改动 + 行为/接口不变。

## 1. 加载器真实路径导入验证（必须）

```bash
<astrbot_root>/.venv/Scripts/python.exe -c "import sys; sys.path.insert(0, r'<astrbot_root>'); import data.plugins.astrbot_plugin_group_history_save_mysql.main"
```

结果：**通过**（无 ModuleNotFoundError；`GroupHistoryPlugin` 存在）。

说明：AstrBot 以包形式加载插件，插件目录不在 sys.path 上，本步验证 `main.py` 及其新子包
（`core/bootstrap.py`、`core/commands.py`）的相对导入链在真实加载路径下成立。

## 2. 既有回归套件

| 套件 | 版本 | 结果 | 说明 |
|------|------|------|------|
| tests/v0.6.1/test_v061.py | v0.6.1（最新公共用例） | **38/38 全绿** | 其中 D-3 接线断言由「main.py 内 `saver=self.saver`」更新为「core/bootstrap.py 内」（装配点迁移，行为不变，断言随演进） |
| tests/v0.6.0/test_v060.py A-5 | v0.6.0（main 可导入性） | **通过** | `main` 可导入、`GroupHistoryPlugin` 存在、`extract_image_urls` 不在 main.py（仍留在 core/parsing） |
| tests/v0.4.0/test_profile_main.py | v0.4.0（构造/注入） | **构造与注入用例全过** | `test_constructor_wires_injections`、`test_mysql_mgr_constructed_with_config_defaults` 通过；仅 1 例失败为旧套件版本断言（期望 0.6.0，当前 0.8.0，**重构前即已如此失败**，与本次无关） |
| tests/v0.5.0/smoke_m_main.py | v0.5.0（旧） | 失败项为旧套件失配 | `FakeBackfill` 未升级接受 `saver=`（v0.6.1 起的真实接线，重构前同样失败）+ 版本断言旧值；重构前即已失败，与本次无关 |

## 3. 说明：非本次引入的旧套件失败
- `test_v070`（v0.7.0）：D-2 断言 main.py 内 `"0.7.0"`，但当前 main.py 早已为 `"0.8.0"`
  （重构前 HEAD 即如此）；B-4 仅测 SQLite 查询日志数据层（与 main.py 无关）。
- `smoke_m_main`（v0.5.0）：`FakeBackfill.__init__` 未加 `saver` 参数，属 v0.6.1 接线前的
  历史快照，重构前即失败。

结论：本次重构未引入任何新失败；所有「真正构造/装配/调用 GroupHistoryPlugin」的用例全部通过。

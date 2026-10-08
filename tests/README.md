# tests/ — 版本化测试套件

本目录收录插件各版本的测试代码，按版本分目录。
每个版本目录下的 `test_0.md` 是该版本的测试用例分析与执行报告，`test_vNNN.py` /
`smoke_*.py` 为可执行套件。

## 目录结构

```
tests/
├── v0.1/    test_config.py
├── v0.2/    test_v02.py
├── v0.3/    test_v03.py
├── v0.3.1/  test_v031.py
├── v0.3.2/  test_v032.py
├── v0.4.0/  test_db_mysql_v040.py, test_models_capture.py, test_profile_*.py …
├── v0.5.0/  smoke_a..m_*.py, test_v050.py
├── v0.5.5/  test_v055.py
├── v0.6.0/  test_v060.py
├── v0.6.1/  test_v061.py
├── v0.7.0/  test_v070.py
├── v0.7.1/  test_v071.py            （该版本未落地，套件保留作存档）
├── v0.8.1/  test_0.md               （仅报告，无独立套件）
├── v0.8.2/  _agentd_routes_extract.py
└── v0.9.0/  test_v090.py, smoke_a_sqlite.py
```

## 运行方式

套件按版本独立运行：**各版本目录单跑全绿 = 该版本回归通过**；跨版本合跑需要统一的测试
基建，属于历史 backlog，不保证可合跑。

```bash
# 单个套件（在插件根目录执行）
python -m pytest tests/v0.7.0/test_v070.py -v

# 某版本目录下全部套件
python -m pytest tests/v0.5.0/ -v

# v0.9.0 基线逐文件运行器（每个文件独立子进程）
python tests/v0.9.0/_run_baseline.py
```

## 环境说明

- 套件**离线可跑**：`astrbot.*` / `aiomysql` 全部以 `sys.modules` 桩注入（异步用例使用内置
  `async_test` 装饰器，不依赖 `pytest-asyncio` / `conftest.py`）；SQLite 侧使用真实
  `aiosqlite` 临时库。
- v0.9.0 的 SQLite 备用后端（`core/db_sqlite.py`、`core/sqlite_migrator.py`）位于实验分支
  `feat/v0.9.0-sqlite-backend`，其套件需在该分支代码上运行。
- 各版本套件面向**其自身版本**的代码契约；在更新的代码上运行可能因版本漂移而出现断言
  失败（属预期）。

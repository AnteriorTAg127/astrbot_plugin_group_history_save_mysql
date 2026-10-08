"""消息捕获辅助函数 —— 兼容转手层（v0.8.2 R3）。

原住于本模块的两个「@对象 / 回复目标」提取纯函数（v0.4.0 人物分析 · 存储增强）
已于 v0.8.2 逐行搬入 :mod:`core.parsing`（扶正户口：saver / main 跨域借用的是
通用消息解析能力，与人物分析无关）。函数体与防御式降级语义零改动。

本文件仅保留 re-export，使老导入路径 ``from .capture import ...`` /
``from core.profile.capture import ...`` 不断；新代码请直接
``from .parsing import extract_at_targets, extract_reply_id``。

- :func:`extract_at_targets`：遍历消息链提取所有 ``Comp.At`` 的目标 QQ（去重保序）
- :func:`extract_reply_id`：提取回复目标 message_id（消息链优先，raw_message 回退）

两函数均为纯函数、全程防御式：任何字段缺失 / 类型异常 / 结构不符都安全降级
（返回 ``[]`` 或 ``""``），绝不向上抛异常，绝不阻断消息存储。详见
:mod:`core.parsing` 中的实现与 docstring。
"""

# 经 __all__ 显式声明 re-export 意图（F401 / mypy --no-implicit-reexport 均认可），
# 老导入路径由此转手至 core/parsing.py 的真实实现
from ..parsing import extract_at_targets, extract_reply_id

__all__ = ["extract_at_targets", "extract_reply_id"]

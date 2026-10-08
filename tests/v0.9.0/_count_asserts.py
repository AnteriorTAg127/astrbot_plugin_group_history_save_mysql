"""统计 test_v090.py 各组断言数（离线测试工具）。"""

import ast
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
src = (pathlib.Path(__file__).resolve().parent / "test_v090.py").read_text(
    encoding="utf-8"
)
tree = ast.parse(src)

for cls in ast.walk(tree):
    if not (isinstance(cls, ast.ClassDef) and cls.name.startswith("Test")):
        continue
    n_asserts = 0
    n_tests = 0
    for fn in ast.walk(cls):
        if isinstance(
            fn, ast.FunctionDef | ast.AsyncFunctionDef
        ) and fn.name.startswith("test_"):
            n_tests += 1
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"
                    and node.func.attr.startswith("assert")
                ):
                    n_asserts += 1
    print(f"{cls.name}: {n_tests} tests, {n_asserts} assert-call-sites")

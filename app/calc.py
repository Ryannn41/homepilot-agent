"""安全的数学表达式求值：只允许数字、四则运算、乘方和少数函数，不执行任意代码。"""

import ast
import math
import operator

OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}
FUNCTIONS = {"sqrt": math.sqrt, "abs": abs, "round": round, "log": math.log, "log10": math.log10}
MAX_EXPONENT = 1000  # 防止 9**9**9 这类表达式卡死进程


def calculate(expression: str) -> str:
    expression = expression.replace("×", "*").replace("÷", "/").replace("^", "**")
    try:
        value = _eval(ast.parse(expression, mode="eval").body)
    except ZeroDivisionError:
        raise ValueError("除数不能为 0") from None
    except SyntaxError:
        raise ValueError(f"无法理解的表达式：{expression}") from None
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else f"{value:.10g}"
    return str(value)


def _eval(node: ast.AST) -> int | float:
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPERATORS:
        return OPERATORS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise ValueError("指数太大")
        return OPERATORS[type(node.op)](left, right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and not node.keywords:
        return FUNCTIONS[node.func.id](*(_eval(arg) for arg in node.args))
    raise ValueError(f"不支持的表达式：{ast.unparse(node)}")

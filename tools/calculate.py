# 有理数精确计算
import ast
import operator
import re
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class CalculateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expression: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    decimal_places: int = Field(default=28, strict=True, ge=0, le=100)


CALCULATE_TOOL = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": (
            "精确计算十进制数学表达式，支持数字、括号、+ - * / // % **，幂指数为整数。"
            "涉及金额、比例和算术时使用此工具。exact 是精确值，decimal 是展示小数，"
            "rounded 表示展示值是否舍入。禁止变量、函数调用及执行代码。"
        ),
        "parameters": CalculateArguments.model_json_schema(),
    },
}

# 运算符
OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

# 支持普通十进制，小数和科学计数法
NUMBER = re.compile(
    r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE]([+-]?\d+))?\Z"
)
def calculate(
        expression:str,
        decimal_places:int = 28
) ->dict:
    """
       按有理数精确计算。

       exact：精确结果，例如 3/10、1/3、100。
       decimal：用于展示的小数。
       rounded：展示的小数是否发生了舍入。
       """

    if (
            not isinstance(expression, str)
            or not 1 <= len(expression) <= 1000
    ):
        raise ValueError("表达式长度必须为 1～1000。")

    if (
            type(decimal_places) is not int
            or not 0 <= decimal_places <= 100
    ):
        raise ValueError(
            "decimal_places 必须是 0～100 的整数。"
        )

    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise ValueError("表达式语法错误。") from exc

    if sum(1 for _ in ast.walk(tree)) > 128:
        raise ValueError("表达式过于复杂。")

    def bounded(value):
        """限制中间值规模，防止超大整数运算占用过多资源。"""

        value = Fraction(value)

        if max(
                value.numerator.bit_length(),
                value.denominator.bit_length(),
        ) > 4096:
            raise ValueError("计算结果超过规模限制。")

        return value

    def visit(node):
        # 只允许数字字面量；bool 虽然是 int 的子类，也明确拒绝。
        if (
                isinstance(node, ast.Constant)
                and type(node.value) in (int, float)
        ):
            # 从源代码读取数字，避免 Python float
            # 先把 0.1 等小数变成有误差的二进制浮点数。
            literal = ast.get_source_segment(
                expression.strip(),
                node,
            ).replace("_", "")

            match = NUMBER.fullmatch(literal)

            if not match or len(literal) > 100:
                raise ValueError(
                    "只支持十进制数字，单个数字最多 100 个字符。"
                )

            if (
                    match.group(1)
                    and abs(int(match.group(1))) > 1000
            ):
                raise ValueError("科学计数法指数过大。")

            return bounded(Fraction(literal))

        # 支持正负号，例如 -3、+(1 + 2)。
        if (
                isinstance(node, ast.UnaryOp)
                and type(node.op) in (ast.UAdd, ast.USub)
        ):
            value = visit(node.operand)

            return (
                value
                if isinstance(node.op, ast.UAdd)
                else -value
            )

        if isinstance(node, ast.BinOp):
            left = visit(node.left)
            right = visit(node.right)

            if isinstance(node.op, ast.Pow):
                # 整数幂能够保持有理数精确性；
                # 非整数幂可能产生无理数，因此这里不开放。
                if (
                        right.denominator != 1
                        or abs(right.numerator) > 100
                ):
                    raise ValueError(
                        "幂指数必须是 -100～100 的整数。"
                    )

                power = right.numerator

                # 在计算幂之前估算规模，避免先生成巨大的结果。
                if max(
                        left.numerator.bit_length(),
                        left.denominator.bit_length(),
                ) * abs(power) > 4096:
                    raise ValueError("幂运算超过规模限制。")

                return bounded(left ** power)

            fn = OPS.get(type(node.op))

            if fn is not None:
                return bounded(fn(left, right))

        # 调用函数、属性访问、变量、下标等均不在允许的语法内。
        raise ValueError(
            "仅支持数字、括号及 + - * / // % **。"
        )

    try:
        value = visit(tree.body)
    except ZeroDivisionError as exc:
        raise ValueError(
            "除数不能为零，零也不能取负次幂。"
        ) from exc

    # 精确结果始终保存在 Fraction 中；
    # 只有生成展示小数时才使用 Decimal 和舍入。
    with localcontext() as ctx:
        ctx.prec = (
                max(
                    len(str(value.numerator)),
                    len(str(value.denominator)),
                )
                + decimal_places
                + 10
        )

        decimal = (
                Decimal(value.numerator)
                / Decimal(value.denominator)
        ).quantize(
            Decimal(1).scaleb(-decimal_places),
            rounding=ROUND_HALF_EVEN,
        )

    display = format(decimal, "f")

    # 去掉小数末尾多余的 0，保留整数末尾的 0。
    if "." in display:
        display = display.rstrip("0").rstrip(".")

    if display == "-0":
        display = "0"

    return {
        "exact": str(value),
        "decimal": display,
        "rounded": Fraction(display) != value,
        "decimal_places": decimal_places,
    }

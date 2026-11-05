"""Pure arithmetic: finite Decimal inputs, exact divisions, explicit money boundary."""
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction

FACTORS = {"g": ("kg", Decimal("0.001")), "kg": ("kg", Decimal(1)),
           "ml": ("l", Decimal("0.001")), "l": ("l", Decimal(1)), "pc": ("pc", Decimal(1))}


def normalized(quantity, unit, base_unit):
    if unit not in FACTORS or FACTORS[unit][0] != base_unit:
        raise ValueError("Единица рецептуры несовместима с единицей ингредиента.")
    if quantity <= 0:
        raise ValueError("Количество должно быть больше нуля.")
    value = Fraction(quantity) * Fraction(FACTORS[unit][1])
    if value >= 10 ** 12 or (value * 10 ** 15).denominator != 1:
        raise ValueError("Количество в базовой единице должно быть меньше 1 000 000 000 000 и содержать не более 15 знаков после запятой.")
    return value


def money(value):
    """ROUND_HALF_UP without an approximation before the half-cent boundary."""
    scaled = Fraction(value) * 100
    sign = -1 if scaled < 0 else 1
    n, d = abs(scaled.numerator), scaled.denominator
    cents = (2 * n + d) // (2 * d) * sign
    # Construct from a decimal string: independent of the caller's Decimal context.
    return Decimal(f"{'-' if cents < 0 else ''}{abs(cents) // 100}.{abs(cents) % 100:02d}")


def decimal_display(value):
    with localcontext() as context:
        context.prec = 50
        value = Fraction(value)
        return Decimal(value.numerator) / Decimal(value.denominator)


def unit_price(pack_price, pack_size):
    if pack_size <= 0 or pack_price < 0:
        raise ValueError("Фасовка должна быть положительной, цена — неотрицательной.")
    return Fraction(pack_price) / Fraction(pack_size)


def line_cost(quantity, unit, base_unit, loss, pack_price, pack_size):
    if not 0 <= loss < 100:
        raise ValueError("Потери должны быть от 0% до значения меньше 100%.")
    net = normalized(quantity, unit, base_unit)
    gross = net / (1 - Fraction(loss) / 100)
    return gross * unit_price(pack_price, pack_size)


@dataclass(frozen=True)
class Calculation:
    rows: tuple
    raw: Fraction
    total: Decimal


def calculate(rows):
    costs = tuple(line_cost(**row) for row in rows)
    raw = sum(costs, Fraction(0))
    return Calculation(costs, raw, money(raw))

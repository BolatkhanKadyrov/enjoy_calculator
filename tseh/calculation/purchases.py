"""Exact purchase arithmetic. Only allocated delivery shares become cents."""
from collections import defaultdict
from decimal import Decimal
from fractions import Fraction


def exact_decimal(value):
    """Convert a finite decimal fraction without consulting Decimal's context."""
    value = Fraction(value)
    denominator, twos, fives = value.denominator, 0, 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ValueError("Промежуточная дробь не является конечной десятичной.")
    scale = max(twos, fives)
    number = value.numerator * 2 ** (scale - twos) * 5 ** (scale - fives)
    digits = str(abs(number)).zfill(scale + 1)
    text = digits if not scale else digits[:-scale] + "." + digits[-scale:]
    return Decimal(("-" if number < 0 else "") + text)


def allocate_delivery(total, rows):
    """Return cents by row ID; largest remainder ties use persistent position."""
    total = Fraction(total)
    if total < 0 or (total * 100).denominator != 1:
        raise ValueError("Доставка должна быть неотрицательной суммой с точностью 0,01 ₸.")
    if not total:
        return {row.id: Decimal("0.00") for row in rows}
    if not rows or any(row.pack_count is None or row.pack_count <= 0 for row in rows):
        raise ValueError("Для доставки укажите положительное количество упаковок каждой строки закупки.")
    costs = {row.id: Fraction(row.pack_count) * Fraction(row.pack_price) for row in rows}
    subtotal = sum(costs.values(), Fraction())
    if subtotal <= 0:
        raise ValueError("Для платной доставки стоимость товаров должна быть больше нуля.")
    raw = {row.id: total * 100 * costs[row.id] / subtotal for row in rows}
    cents = {key: value.numerator // value.denominator for key, value in raw.items()}
    remainder = int(total * 100) - sum(cents.values())
    ordered = sorted(rows, key=lambda row: (-(raw[row.id] - cents[row.id]), row.position, str(row.id)))
    for row in ordered[:remainder]:
        cents[row.id] += 1
    return {key: exact_decimal(Fraction(value, 100)) for key, value in cents.items()}


def aggregate_lines(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.ingredient_id].append(row)
    result = {}
    for key, lines in grouped.items():
        if len(lines) > 1 and any(row.pack_count is None for row in lines):
            raise ValueError("Для средневзвешенной цены укажите количество упаковок всех повторных строк позиции.")
        # A single offer without purchase volume remains a price per one pack.
        goods = sum((Fraction(row.pack_price) * Fraction(row.pack_count if row.pack_count is not None else 1) for row in lines), Fraction())
        quantity = sum((Fraction(row.pack_size) * Fraction(row.pack_count if row.pack_count is not None else 1) for row in lines), Fraction())
        delivery = sum((Fraction(row.delivery_share) for row in lines), Fraction())
        result[key] = {"goods_total": exact_decimal(goods), "quantity_total": exact_decimal(quantity),
                       "delivery_total": exact_decimal(delivery), "unit_price": (goods + delivery) / quantity}
    return result

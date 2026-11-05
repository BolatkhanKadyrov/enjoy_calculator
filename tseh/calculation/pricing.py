from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from . import money

MAX_PRICE_CENTS = 99999999999999
ZONE_LABELS = {"green": "Зелёная зона", "orange": "Оранжевая зона", "red": "Красная зона",
               "unconfigured": "Пороги не настроены", "invalid": "Некорректные пороги", "no_price": "Цена после скидки равна нулю"}


def effective_thresholds(dish, settings):
    return (dish.green_override if dish.green_override is not None else settings.green,
            dish.orange_override if dish.orange_override is not None else settings.orange)


def threshold_state(green, orange):
    if (green is not None and green <= 0) or (orange is not None and orange <= 0):
        return "invalid"
    if green is not None and orange is not None and green >= orange:
        return "invalid"
    if green is None or orange is None:
        return "unconfigured"
    return "valid"


def minimum_price(cost, discount, green, orange=None):
    if not 0 <= discount < 100:
        raise ValueError("Скидка должна быть от 0% до значения меньше 100%.")
    if cost < 0:
        raise ValueError("Себестоимость не может быть отрицательной.")
    if green is None or threshold_state(green, orange) == "invalid":
        return None
    factor = 1 - Fraction(discount) / 100
    def passes(cents):
        effective = money(Fraction(cents, 100) * factor)
        return effective > 0 and 100 * Fraction(cost) <= Fraction(green) * Fraction(effective)
    if not passes(MAX_PRICE_CENTS):
        return None
    low, high = 1, MAX_PRICE_CENTS
    while low < high:
        middle = (low + high) // 2
        if passes(middle):
            high = middle
        else:
            low = middle + 1
    return money(Fraction(low, 100))


@dataclass(frozen=True)
class Assessment:
    effective_raw: Fraction
    effective: Decimal
    ratio: object
    zone: str
    green: object
    orange: object
    minimum: object
    minimum_exceeds_limit: bool

    @property
    def label(self):
        return ZONE_LABELS[self.zone]


def assess(cost, price, discount, green=None, orange=None):
    if not 0 <= discount < 100 or price < 0 or cost < 0:
        raise ValueError("Недопустимая цена, себестоимость или скидка.")
    effective_raw = Fraction(price) * (1 - Fraction(discount) / 100)
    effective = money(effective_raw)
    state = threshold_state(green, orange)
    ratio = 100 * Fraction(cost) / Fraction(effective) if effective > 0 else None
    if effective <= 0:
        zone = "no_price"
    elif state != "valid":
        zone = state
    elif 100 * Fraction(cost) <= Fraction(green) * Fraction(effective):
        zone = "green"
    elif 100 * Fraction(cost) <= Fraction(orange) * Fraction(effective):
        zone = "orange"
    else:
        zone = "red"
    minimum = minimum_price(cost, discount, green, orange)
    exceeds = minimum is None and green is not None and state != "invalid"
    return Assessment(effective_raw, effective, ratio, zone, green, orange, minimum, exceeds)

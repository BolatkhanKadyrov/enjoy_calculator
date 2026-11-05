from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from tseh.price_forms import PriceDataForm, PriceDeleteForm, PriceForm


def data(**changes):
    return {"token": "command-token", "supplier": "", "price_date": "2026-11-04",
            "pack_size": "0,000000000001", "pack_price": "12,34", "comment": "Разовая цена",
            **changes}


def test_exact_price_data_keeps_precision_and_excludes_command_flags():
    form = PriceForm(data(apply_now="on"), include_apply=True)
    assert form.is_valid(), form.errors
    result = form.price_data()
    assert result["pack_size"] == Decimal("0.000000000001")
    assert result["pack_price"] == Decimal("12.34")
    assert result["supplier_id"] is None
    assert set(result) == {"supplier_id", "price_date", "pack_size", "pack_price", "comment", "is_one_off"}
    assert form.cleaned_data["apply_now"] is True


def test_zero_requires_explicit_confirmation_even_when_offer_is_not_applied():
    form = PriceForm(data(pack_price="0"), include_apply=True)
    assert not form.is_valid()
    assert "pack_price" in form.errors
    confirmed = PriceForm(data(pack_price="0", confirm_zero="on"))
    assert confirmed.is_valid(), confirmed.errors


def test_invalid_pack_size_and_money_precision_are_not_silently_rounded():
    for changes, field in [({"pack_size": "0"}, "pack_size"),
                           ({"pack_size": "0.0000000000001"}, "pack_size"),
                           ({"pack_price": "2.345"}, "pack_price"),
                           ({"pack_price": "-1"}, "pack_price"),
                           ({"comment": "x" * 2001}, "comment")]:
        form = PriceForm(data(**changes))
        assert not form.is_valid()
        assert field in form.errors


def test_correction_reason_optional_and_create_apply_flag_scoped():
    correction = PriceForm(data(apply_now="on"), include_reason=True)
    assert correction.is_valid(), correction.errors
    assert correction.cleaned_data["reason"] == ""
    assert "apply_now" not in correction.fields
    assert "apply_now" not in correction.cleaned_data
    create = PriceForm(data(reason="forged"), include_apply=True)
    assert create.is_valid()
    assert "reason" not in create.cleaned_data


def test_replacement_uses_own_prefix_and_no_command_token():
    payload = {f"replacement-{key}": value for key, value in data().items() if key != "token"}
    form = PriceDataForm(payload, prefix="replacement", use_required_attribute=False)
    assert form.is_valid(), form.errors
    assert "token" not in form.fields
    assert "required" not in str(form["pack_size"])


def test_noncurrent_delete_does_not_accept_replacement_controls():
    ingredient = SimpleNamespace(current_price_id=uuid4())
    price = SimpleNamespace(pk=uuid4())
    form = PriceDeleteForm({"token": "token", "replacement_mode": "new"}, ingredient=ingredient, price=price)
    assert form.is_valid(), form.errors
    assert list(form.fields) == ["token"]
    assert "replacement_mode" not in form.cleaned_data

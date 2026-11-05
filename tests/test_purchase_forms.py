from decimal import Decimal as D
import uuid

import pytest

from tseh.models import Purchase
from tseh.purchase_forms import (DeliveryForm, PurchaseForm, PurchaseLineDeleteForm,
                                 PurchaseLineForm, PurchaseLineFormSet)
from tests.test_stage2_services import item

pytestmark = pytest.mark.django_db


def row_data(key, **changes):
    return {"ingredient": str(key), "pack_size": "5", "pack_price": "5000", "pack_count": "4", **changes}


def test_comma_and_exact_input_limits(state):
    key = item()
    form = PurchaseLineForm(row_data(key, pack_size="0,000000000001", pack_price="0,01", pack_count="3,125"))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["pack_size"] == D("0.000000000001")
    assert form.cleaned_data["pack_count"] == D("3.125")
    for changes in ({"pack_price": "1.001"}, {"pack_price": ""}, {"pack_size": "0.0000000000001"},
                    {"pack_count": "0"}, {"ingredient": "broken-uuid"}, {"id": "bad"}):
        assert not PurchaseLineForm(row_data(key, **changes)).is_valid()
    assert PurchaseLineForm(row_data(key, pack_count="")).is_valid()


def test_delivery_edit_requires_reason_and_no_silent_money_rounding():
    assert DeliveryForm({"token": "test", "amount": "3000,01", "note": ""}).is_valid()
    assert not DeliveryForm({"token": "test", "amount": "3000,001"}).is_valid()
    assert not DeliveryForm({"token": "test", "amount": "2900", "note": ""}, editing=True).is_valid()
    corrected = DeliveryForm({"token": "test", "amount": "2900", "note": "", "reason": "Опечатка"}, editing=True)
    assert corrected.is_valid()


def test_purchase_delivery_correction_requires_reason():
    purchase = Purchase(delivery_total=D(600))
    data = {"token": "test", "purchase_date": "2026-11-04", "delivery_total": "700", "comment": ""}
    assert not PurchaseForm(data, purchase=purchase).is_valid()
    assert PurchaseForm({**data, "reason": "Доставка по чеку"}, purchase=purchase).is_valid()
    assert PurchaseForm({**data, "delivery_total": "600"}, purchase=purchase).is_valid()


def test_last_line_cannot_reallocate_and_actions_are_explicit():
    form = PurchaseLineDeleteForm({"token": "test", "delivery_action": "reallocate"}, last_line=True)
    assert not form.is_valid()
    assert "replacement_mode" not in form.fields
    assert set(dict(form.fields["delivery_action"].choices)) == {"keep", "delete"}
    assert not PurchaseLineDeleteForm({"token": "test"}, last_line=False).is_valid()


def test_formset_rejects_forged_stable_ids_and_empty_added_rows(state):
    key = item()
    data = {"lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "1",
            "lines-MAX_NUM_FORMS": "100", **{f"lines-0-{k}": v for k, v in row_data(key).items()}}
    assert PurchaseLineFormSet(data, prefix="lines").is_valid()
    assert not PurchaseLineFormSet({**data, "lines-0-id": str(uuid.uuid4())}, prefix="lines").is_valid()
    assert not PurchaseLineFormSet({**data, "lines-TOTAL_FORMS": "2"}, prefix="lines").is_valid()
    assert not PurchaseLineFormSet({**data, "lines-TOTAL_FORMS": "101"}, prefix="lines").is_valid()

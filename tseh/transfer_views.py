"""Manual downloads and a guarded full domain restore; credentials stay in place."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from tseh.services import backups
from tseh.services.reports import build_xlsx
from tseh.views import analysis_discount, read_snapshot

LABELS = {"settings": "Настройки", "suppliers": "Поставщики", "ingredients": "Сырьё и упаковка", "prices": "Цены",
    "price_applications": "Применения цен", "purchases": "Закупки", "purchase_lines": "Строки закупок",
    "unallocated_delivery": "Нераспределённая доставка", "preparations": "Заготовки", "preparation_lines": "Строки заготовок",
    "dishes": "Блюда", "dish_lines": "Строки рецептур", "catalog_drafts": "Черновики", "journal": "Записи журнала"}


class UploadForm(forms.Form):
    backup_file = forms.FileField(label="JSON-копия", widget=forms.FileInput(attrs={"accept": ".json,application/json"}),
        help_text="Полная копия, ранее скачанная из Цеха. Максимальный размер — 25 МиБ.")

    def clean_backup_file(self):
        upload = self.cleaned_data["backup_file"]
        if upload.size > backups.MAX_BACKUP_BYTES:
            raise ValidationError("Файл больше 25 МиБ. Выберите полную JSON-копию допустимого размера.")
        return upload


class RestoreForm(forms.Form):
    review_token = forms.CharField(widget=forms.HiddenInput)
    confirm_download = forms.BooleanField(label="Я скачал текущую копию и подтверждаю замену данных и журнала", required=True)


def attachment(data, kind, *, prefix="tseh"):
    mime = "application/json" if kind == "json" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    response = HttpResponse(data, content_type=mime)
    response["Content-Disposition"] = f'attachment; filename="{prefix}-{timezone.now():%Y%m%dT%H%M%SZ}.{kind}"'
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def export_error(request, exc):
    form = UploadForm({})
    form.is_valid()
    form.errors.clear()
    for message in exc.messages:
        form.add_error(None, message)
    return render(request, "tseh/exchange.html", {"form": form, "active": "exchange",
        "discount": analysis_discount(request)[0]}, status=400)


@login_required
@require_http_methods(["GET", "POST"])
def exchange(request):
    form = UploadForm(request.POST if request.method == "POST" else None, request.FILES if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            upload = form.cleaned_data["backup_file"]
            session = backups.prepare_restore(request.user, upload.read(backups.MAX_BACKUP_BYTES + 1))
            return redirect("restore-review", pk=session.pk)
        except ValidationError as exc:
            for message in exc.messages:
                form.add_error("backup_file", message)
    return render(request, "tseh/exchange.html", {"form": form, "active": "exchange", "discount": analysis_discount(request)[0]},
                  status=400 if form.errors else 200)


@login_required
@require_GET
def export_json(request):
    try:
        return attachment(backups.encode_backup(backups.export_snapshot()), "json")
    except ValidationError as exc:
        return export_error(request, exc)


@login_required
@require_GET
def export_xlsx(request):
    try:
        with read_snapshot():
            data = build_xlsx(analysis_discount(request)[0])
    except ValidationError as exc:
        return export_error(request, exc)
    return attachment(data, "xlsx")


def _review(request, pk, form=None, errors=()):
    try:
        session = backups.get_restore(pk, request.user)
    except ValidationError as exc:
        return render(request, "tseh/restore_unavailable.html", {"errors": exc.messages, "active": "exchange"}, status=400)
    if session.completed_at:
        return render(request, "tseh/restore_complete.html", {"session": session, "active": "exchange"})
    previous, proposed = backups.data_summary(session.previous), backups.data_summary(session.proposed)
    rows = [(LABELS.get(name, name), count, proposed[name]) for name, count in previous.items()]
    return render(request, "tseh/restore_review.html", {"session": session, "rows": rows, "errors": errors,
        "form": form if form is not None else RestoreForm(initial={"review_token": backups.restore_token(session, request.user)}), "active": "exchange"},
        status=400 if errors or (form is not None and form.errors) else 200)


@login_required
@require_http_methods(["GET", "POST"])
def restore_review(request, pk):
    if request.method == "GET":
        return _review(request, pk)
    form = RestoreForm(request.POST)
    if form.is_valid():
        try:
            backups.apply_restore(pk, request.user, confirm_download=form.cleaned_data["confirm_download"],
                                  review_token=form.cleaned_data["review_token"])
            return redirect("restore-review", pk=pk)
        except ValidationError as exc:
            return _review(request, pk, errors=exc.messages)
    return _review(request, pk, form=form)


@login_required
@require_POST
def restore_download(request, pk):
    try:
        document = backups.download_backup(pk, request.user)
        return attachment(backups.encode_backup(document), "json", prefix="tseh-before-restore")
    except ValidationError as exc:
        return _review(request, pk, errors=exc.messages)


@login_required
@require_POST
def restore_refresh(request, pk):
    try:
        backups.refresh_restore(pk, request.user)
        messages.info(request, "Сформирована свежая копия. Скачайте её и заново подтвердите замену.")
        return redirect("restore-review", pk=pk)
    except ValidationError as exc:
        return _review(request, pk, errors=exc.messages)

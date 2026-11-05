from django.core.management.base import BaseCommand

from tseh.auth import cleanup_login_attempts


class Command(BaseCommand):
    help = "Удалить истёкшие счётчики попыток входа старше суток."

    def handle(self, *args, **options):
        self.stdout.write(f"Удалено истёкших счётчиков входа: {cleanup_login_attempts()}")

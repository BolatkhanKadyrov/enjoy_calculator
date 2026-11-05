from django.core.management.base import BaseCommand

from tseh.services.backups import cleanup_restore_sessions


class Command(BaseCommand):
    help = "Удалить истёкшие временные данные подготовки JSON-восстановления (24 часа)."

    def handle(self, *args, **options):
        self.stdout.write(f"Удалено временных подготовок восстановления: {cleanup_restore_sessions()}")

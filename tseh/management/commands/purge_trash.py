from django.core.management.base import BaseCommand

from tseh.services.lifecycle import purge_due_dishes


class Command(BaseCommand):
    help = "Удалить блюда из корзины, срок которых уже наступил. Запускать ежедневно."

    def handle(self, *args, **options):
        count = purge_due_dishes()
        self.stdout.write(f"Окончательно удалено блюд из корзины: {count}.")

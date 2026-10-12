from django.core.management.base import BaseCommand
from tracker.services import check_and_update_disconnected_riders

class Command(BaseCommand):
    help = 'Inspects active rides and updates riders whose last_seen exceeded DISCONNECT_THRESHOLD_SECONDS to DISCONNECTED.'

    def handle(self, *args, **options):
        disconnected = check_and_update_disconnected_riders()
        if disconnected:
            self.stdout.write(self.style.WARNING(f'Marked {len(disconnected)} riders as DISCONNECTED.'))
            for d in disconnected:
                self.stdout.write(f" - Rider ID {d['rider_id']} ({d['name']}) last seen {d['last_seen']}")
        else:
            self.stdout.write(self.style.SUCCESS('All active riders are currently connected.'))

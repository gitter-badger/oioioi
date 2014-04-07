import os
from optparse import make_option

from django.core.management.base import BaseCommand
from django.conf import settings


class Command(BaseCommand):
    help = 'Runs the OIOIOI notifications server'
    requires_model_validation = False
    option_list = BaseCommand.option_list + (
        make_option('-i', '--install', action='store_true',
                    help='install dependencies requires by the server'),
    )

    def handle(self, *args, **options):
        path = os.path.join(os.path.dirname(__file__), '..', '..', 'server')
        os.chdir(path)
        if options['install']:
            os.execlp('env', 'env', 'npm', 'install')
        else:
            os.execlp('env', 'env', 'node', 'ns-main.js',
              '--port', settings.NOTIFICATIONS_SERVER_PORT.__str__(),
              '--url', settings.OIOIOI_URL,
              '--amqp', settings.RABBITMQ_URL)

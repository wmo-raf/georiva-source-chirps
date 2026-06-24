"""The plugin's AppConfig.ready() must register both recipes on the engine."""
from django.apps import apps
from django.test import TestCase

from georiva.processing.registry import recipe_registry


class RegistrationTests(TestCase):
    def test_app_ready_registers_both_chirps_recipes(self):
        # ready() is idempotent, so calling it here exercises the wiring that
        # runs at process startup.
        apps.get_app_config("georiva_source_chirps").ready()

        types = recipe_registry.all_types()
        self.assertIn("chirps-climatology", types)
        self.assertIn("chirps-anomaly", types)

from django.apps import AppConfig


class GeorivaSourceChirpsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "georiva_source_chirps"
    verbose_name = "GeoRiva CHIRPS"

    def ready(self):
        # Import the derivation recipes so they register on the engine at
        # startup — required in *every* process (web + georiva-processing
        # worker), or units drop with "Unknown recipe". See docs/adr/0007.
        from georiva_source_chirps.recipes import anomaly, climatology  # noqa: F401

"""CHIRPS derived-product declarations (ADR-0008).

CHIRPSDataFeed.get_derived_products() declares, per selected resolution, the
products that turn raw CHIRPS into served + derived collections. This slice
covers the promotion product that publishes the raw rainfall.
"""
from django.test import TestCase

from georiva.core.derived_products import InputRef, OutputRef
from georiva.core.models import Catalog, Collection
from georiva.sources.derivation_invocation import collection_routes_to_staging
from georiva.sources.models import DerivedProduct

from georiva_source_chirps.constants import (
    anomaly_slug,
    climatology_slug,
    relative_anomaly_slug,
    source_slug,
)
from georiva_source_chirps.models import (
    CHIRPSDataFeed,
    CHIRPSDataFeedCollectionLink,
)


def _transient_feed(*selected_keys):
    feed = CHIRPSDataFeed(name="CHIRPS")
    feed._wizard_selected_keys = list(selected_keys)
    return feed


class SlugSchemeTests(TestCase):
    """The canonical CHIRPS collection-slug scheme — the single source the
    declarations (and, via injection, the recipes) build from."""

    def test_slug_helpers_follow_the_chirps_resolution_scheme(self):
        self.assertEqual(source_slug("monthly"), "chirps-monthly")
        self.assertEqual(climatology_slug("monthly"), "chirps-monthly-climatology")
        self.assertEqual(anomaly_slug("dekadal"), "chirps-dekadal-anomaly")
        self.assertEqual(
            relative_anomaly_slug("pentadal"), "chirps-pentadal-relative-anomaly"
        )


class PromotionProductTests(TestCase):
    def test_declares_one_promotion_product_per_selected_resolution(self):
        feed = _transient_feed("chirps-monthly")

        products = feed.get_derived_products()

        self.assertEqual(len(products), 1)
        product = products[0]
        self.assertEqual(product.key, "chirps-monthly-promotion")
        self.assertEqual(product.recipe_type, "promotion")
        self.assertEqual(product.trigger_mode, "event")
        self.assertEqual(
            product.inputs,
            (InputRef(role="source", collection="chirps-monthly", tier="staging"),),
        )
        self.assertEqual(
            product.outputs,
            (OutputRef(role="served", collection="chirps-monthly"),),
        )

    def test_one_product_per_resolution_across_multiple_selections(self):
        feed = _transient_feed("chirps-monthly", "chirps-dekadal")

        keys = {p.key for p in feed.get_derived_products()}

        self.assertEqual(keys, {"chirps-monthly-promotion", "chirps-dekadal-promotion"})

    def test_saved_feed_declares_the_same_products_as_the_transient_stash(self):
        catalog = Catalog.objects.create(
            name="CHIRPS", slug="chirps", file_format="geotiff"
        )
        feed = CHIRPSDataFeed.objects.create(name="CHIRPS", catalog=catalog)
        collection = Collection.objects.create(
            catalog=catalog, slug="chirps-monthly", name="CHIRPS Monthly"
        )
        CHIRPSDataFeedCollectionLink.objects.create(
            data_feed=feed, collection=collection,
            definition_key="chirps-monthly", period="monthly",
        )

        saved = {(p.key, p.recipe_type) for p in feed.get_derived_products()}
        transient = {
            (p.key, p.recipe_type)
            for p in _transient_feed("chirps-monthly").get_derived_products()
        }

        self.assertEqual(saved, transient)
        self.assertEqual(saved, {("chirps-monthly-promotion", "promotion")})


class RawRoutesToStagingTests(TestCase):
    """The promotion product declares its raw input at the staging tier, which is
    what makes the raw CHIRPS collection auto-route to staging (ADR-0008) so
    there is something to promote."""

    def setUp(self):
        self.catalog = Catalog.objects.create(
            name="CHIRPS", slug="chirps", file_format="geotiff"
        )
        self.feed = CHIRPSDataFeed.objects.create(name="CHIRPS", catalog=self.catalog)
        collection = Collection.objects.create(
            catalog=self.catalog, slug="chirps-monthly", name="CHIRPS Monthly"
        )
        CHIRPSDataFeedCollectionLink.objects.create(
            data_feed=self.feed, collection=collection,
            definition_key="chirps-monthly", period="monthly",
        )
        DerivedProduct.objects.create(
            data_feed=self.feed, definition_key="chirps-monthly-promotion",
            recipe_type="promotion", config={}, is_enabled=True,
        )

    def test_raw_collection_routes_to_staging_when_promotion_is_enabled(self):
        self.assertTrue(collection_routes_to_staging(self.feed, "chirps-monthly"))

    def test_a_collection_no_product_consumes_does_not_route_to_staging(self):
        self.assertFalse(collection_routes_to_staging(self.feed, "chirps-dekadal"))

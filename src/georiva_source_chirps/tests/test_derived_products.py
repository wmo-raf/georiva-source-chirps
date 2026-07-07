"""CHIRPS derived-product declarations (ADR-0008).

CHIRPSDataFeed.get_derived_products() declares, per selected resolution, the
products that turn raw CHIRPS into served + derived collections. This slice
covers the promotion product that publishes the raw rainfall.
"""
from django.test import TestCase

from georiva.core.derived_products import InputRef
from georiva.core.models import Catalog, Collection
from georiva.sources.derivation_invocation import collection_routes_to_staging
from georiva.sources.models import DerivedProduct, DerivedProductInput

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


def _promotion_products(feed):
    return [p for p in feed.get_derived_products() if p.recipe_type == "promotion"]


class PromotionProductTests(TestCase):
    def test_declares_one_promotion_product_per_selected_resolution(self):
        feed = _transient_feed("chirps-monthly")

        products = _promotion_products(feed)

        self.assertEqual(len(products), 1)
        product = products[0]
        self.assertEqual(product.key, "chirps-monthly-promotion")
        self.assertEqual(product.recipe_type, "promotion")
        self.assertEqual(product.trigger_mode, "event")
        self.assertEqual(
            product.inputs,
            (InputRef(role="source", collection="chirps-monthly", tier="staging"),),
        )
        out = product.outputs[0]
        self.assertEqual((out.role, out.collection), ("served", "chirps-monthly"))
        self.assertEqual(out.title, "CHIRPS monthly rainfall")
        self.assertTrue(out.description)
        self.assertEqual(out.visibility, "public")

    def test_one_product_per_resolution_across_multiple_selections(self):
        feed = _transient_feed("chirps-monthly", "chirps-dekadal")

        keys = {p.key for p in _promotion_products(feed)}

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
        self.assertEqual(saved, {
            ("chirps-monthly-promotion", "promotion"),
            ("chirps-monthly-climatology", "chirps-climatology"),
            ("chirps-monthly-anomaly", "chirps-anomaly"),
        })


class ClimatologyProductTests(TestCase):
    def _climatology(self, feed):
        return next(
            p for p in feed.get_derived_products()
            if p.recipe_type == "chirps-climatology"
        )

    def test_declares_a_manual_climatology_product_per_resolution(self):
        product = self._climatology(_transient_feed("chirps-monthly"))

        self.assertEqual(product.key, "chirps-monthly-climatology")
        self.assertEqual(product.recipe_type, "chirps-climatology")
        self.assertEqual(product.trigger_mode, "manual")
        self.assertEqual(
            product.inputs,
            (InputRef(role="value", collection="chirps-monthly", tier="staging"),),
        )
        # Output slug drops the baseline years: one climatology collection per
        # resolution (ADR-0008). It is declared internal — a derivation
        # intermediate (the anomaly's baseline), never served.
        out = product.outputs[0]
        self.assertEqual(
            (out.role, out.collection),
            ("climatology", "chirps-monthly-climatology"),
        )
        self.assertEqual(out.title, "CHIRPS monthly climatology")
        self.assertTrue(out.description)
        self.assertEqual(out.visibility, "internal")

    def test_config_schema_exposes_baseline_and_min_count_with_defaults(self):
        from georiva_source_chirps.constants import CHIRPS_BASELINE

        product = self._climatology(_transient_feed("chirps-monthly"))
        schema = {f.key: f for f in product.config_schema}

        self.assertEqual(set(schema), {"baseline_start", "baseline_end", "min_count"})
        self.assertEqual(schema["baseline_start"].default, CHIRPS_BASELINE[0])
        self.assertEqual(schema["baseline_end"].default, CHIRPS_BASELINE[1])
        self.assertEqual(schema["baseline_start"].type, "int")

    def test_config_validates_and_coerces_operator_values(self):
        product = self._climatology(_transient_feed("chirps-monthly"))

        cleaned = product.validate_config({"baseline_start": "1981", "min_count": "10"})

        self.assertEqual(cleaned["baseline_start"], 1981)
        self.assertEqual(cleaned["min_count"], 10)


class AnomalyProductTests(TestCase):
    def _anomaly(self, feed):
        return next(
            p for p in feed.get_derived_products()
            if p.recipe_type == "chirps-anomaly"
        )

    def test_declares_an_event_anomaly_product_per_resolution(self):
        product = self._anomaly(_transient_feed("chirps-monthly"))

        self.assertEqual(product.key, "chirps-monthly-anomaly")
        self.assertEqual(product.recipe_type, "chirps-anomaly")
        self.assertEqual(product.trigger_mode, "event")
        self.assertEqual(product.config_schema, ())

    def test_consumes_the_raw_value_and_the_published_climatology_baseline(self):
        product = self._anomaly(_transient_feed("chirps-monthly"))

        self.assertEqual(
            product.inputs,
            (
                InputRef(role="value", collection="chirps-monthly", tier="staging"),
                InputRef(role="baseline", collection="chirps-monthly-climatology",
                         tier="published"),
            ),
        )

    def test_emits_absolute_and_relative_anomaly_collections_without_baseline_years(self):
        product = self._anomaly(_transient_feed("chirps-monthly"))

        anomaly, relative = product.outputs
        self.assertEqual(
            (anomaly.role, anomaly.collection),
            ("anomaly", "chirps-monthly-anomaly"),
        )
        self.assertEqual(anomaly.title, "CHIRPS monthly anomaly")
        self.assertEqual(anomaly.visibility, "public")
        self.assertEqual(
            (relative.role, relative.collection),
            ("relative-anomaly", "chirps-monthly-relative-anomaly"),
        )
        self.assertEqual(relative.title, "CHIRPS monthly relative anomaly")
        # Both anomaly outputs carry human descriptions for the catalog.
        self.assertTrue(anomaly.description)
        self.assertTrue(relative.description)


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
        product = DerivedProduct.objects.create(
            data_feed=self.feed, definition_key="chirps-monthly-promotion",
            recipe_type="promotion", config={}, is_enabled=True,
        )
        # Routing is driven by the pinned staging-input binding (ADR-0010 §4).
        DerivedProductInput.objects.create(
            product=product, role="source", tier="staging",
            source_key="chirps-monthly", collection=collection,
        )

    def test_raw_collection_routes_to_staging_when_promotion_is_enabled(self):
        self.assertTrue(collection_routes_to_staging(self.feed, "chirps-monthly"))

    def test_a_collection_no_product_consumes_does_not_route_to_staging(self):
        self.assertFalse(collection_routes_to_staging(self.feed, "chirps-dekadal"))


class WizardPathRoutingRegressionTests(TestCase):
    """End-to-end guard for the failure this ADR-0010 slice fixes: a CHIRPS feed
    provisioned through the *real* setup service (not hand-created collections)
    must route its raw collection to staging and dispatch an arriving staged
    input to the promotion. Before the unified slug scheme the service produced a
    catalog-prefixed slug ('chirps-chirps-monthly') that never matched the
    declaration key ('chirps-monthly'), so the whole product chain sat inert —
    invisible to every test that hand-created the collection with the right slug.
    """

    def _monthly_definition(self):
        return next(
            d for d in CHIRPSDataFeed.get_collection_definitions()
            if d.key == "chirps-monthly"
        )

    def _provision(self):
        from georiva.sources.setup_service import SourceSetupService

        catalog = Catalog.objects.create(
            name="CHIRPS", slug="chirps", file_format="geotiff"
        )
        service = SourceSetupService()
        feed, collections = service.provision(
            CHIRPSDataFeed,
            catalog=catalog,
            feed_name="CHIRPS Africa",
            selected_definitions=[(self._monthly_definition(), {})],
        )
        promotion = next(
            p for p in feed.get_derived_products()
            if p.key == "chirps-monthly-promotion"
        )
        service.provision_derived_products(feed, [(promotion, {}, True)])
        return feed, collections[0]

    def test_provisioned_raw_collection_uses_the_declaration_key_as_its_slug(self):
        _feed, collection = self._provision()
        # The service-derived slug — not a hand-written literal — is the key the
        # promotion's InputRef/OutputRef reference.
        self.assertEqual(collection.slug, "chirps-monthly")

    def test_provisioned_feed_routes_its_raw_collection_to_staging(self):
        feed, collection = self._provision()
        self.assertTrue(collection_routes_to_staging(feed, collection.slug))

    def test_arriving_staged_input_dispatches_to_the_promotion(self):
        from unittest.mock import patch

        from georiva.sources.derivation_invocation import dispatch_for_input

        feed, collection = self._provision()
        # An arriving StagingItem in the raw collection. The trigger carries the
        # collection_id (ADR-0010 §3/§4) that dispatch matches binding rows on.
        # Patch the engine so we assert the routing decision, not a full run.
        trigger = {
            "staging_item_id": 4242,
            "collection_id": collection.pk,
            "collection_slug": collection.slug,
        }
        with patch("georiva.processing.engine.run", return_value=[]) as run:
            dispatch_for_input(trigger)

        self.assertTrue(run.called, "no product matched the arriving staged input")
        matched_recipe = run.call_args.args[0]
        self.assertEqual(matched_recipe.type, "promotion")

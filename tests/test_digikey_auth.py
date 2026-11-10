"""Focused OAuth tests, runnable without an InvenTree installation."""

import importlib
import multiprocessing
import os
import tempfile
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import django
from cryptography.fernet import Fernet
from django.conf import settings

if not settings.configured:
    settings.configure(
        DATABASES={
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
        },
        INSTALLED_APPS=[],
        SECRET_KEY="oauth-test-only",
    )
    django.setup()

from django.db import connection, connections, models  # noqa: E402

from inventree_supplier_panel.digikey_auth import (  # noqa: E402
    CREDENTIAL_SETTING,
    Credentials,
    DigikeyAuth,
    DigikeyAuthError,
)


class Config(models.Model):
    active = models.BooleanField(default=True)

    class Meta:
        app_label = "oauth_tests"


class Setting(models.Model):
    plugin = models.ForeignKey(Config, on_delete=models.CASCADE)
    key = models.CharField(max_length=100)
    value = models.TextField(default="")

    class Meta:
        app_label = "oauth_tests"
        unique_together = [("plugin", "key")]


PLUGIN_MODELS = types.ModuleType("plugin.models")
PLUGIN_MODELS.PluginConfig = Config
PLUGIN_MODELS.PluginSetting = Setting


class Plugin:
    NAME = "SupplierCart"
    COUNTRY_CODES = {"USD": "US"}
    DEFAULT_COUNTRY_CODE = "US"

    def plugin_config(self):
        return Config.objects.get(pk=1)

    def get_setting(self, key):
        return (
            Setting.objects.filter(plugin_id=1, key=key)
            .values_list("value", flat=True)
            .first()
            or ""
        )


def refresh_worker(database, key, calls, results):
    """Run a refresh in an independent worker with its own DB connection."""
    connections.close_all()
    connection.settings_dict["NAME"] = database

    def post(*args, **kwargs):
        calls.put("refresh")
        time.sleep(0.15)
        return Mock(
            status_code=200,
            json=lambda: {
                "access_token": "worker-access",
                "refresh_token": "worker-refresh",
                "expires_in": 1800,
            },
        )

    with (
        patch.dict(
            os.environ, {"DIGIKEY_CREDENTIAL_KEY": key, "DIGIKEY_CLIENT_SECRET": ""}
        ),
        patch.dict("sys.modules", {"plugin.models": PLUGIN_MODELS}),
        patch(
            "inventree_supplier_panel.digikey_auth.Wrappers.post_request",
            side_effect=post,
        ),
    ):
        try:
            results.put(DigikeyAuth(Plugin()).access_token())
        except DigikeyAuthError as error:
            results.put(str(error))
        finally:
            connections.close_all()


class OAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.database = os.path.join(cls.temp.name, "oauth.sqlite3")
        connections.close_all()
        connection.settings_dict["NAME"] = cls.database
        with connection.schema_editor() as editor:
            editor.create_model(Config)
            editor.create_model(Setting)
        Config.objects.create(pk=1)

    @classmethod
    def tearDownClass(cls):
        connections.close_all()
        cls.temp.cleanup()

    def setUp(self):
        Setting.objects.all().delete()
        self.key = Fernet.generate_key().decode()
        self.environment = patch.dict(
            os.environ,
            {
                "DIGIKEY_CREDENTIAL_KEY": self.key,
                "DIGIKEY_CLIENT_SECRET": "",
            },
        )
        self.environment.start()
        self.models = patch.dict("sys.modules", {"plugin.models": PLUGIN_MODELS})
        self.models.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.models.stop)
        self.plugin = Plugin()
        self.auth = DigikeyAuth(self.plugin)
        self.set_setting("DIGIKEY_CLIENT_ID", "client-id")
        self.auth.save(
            self.plugin.plugin_config(),
            Credentials(
                "old-access", "old-refresh", time.time() + 1800, "client-secret"
            ),
        )
        self.post = patch("inventree_supplier_panel.digikey_auth.Wrappers.post_request")
        self.request = self.post.start()
        self.addCleanup(self.post.stop)
        self.request.return_value = self.response()

    def set_setting(self, key, value):
        Setting.objects.update_or_create(
            plugin_id=1, key=key, defaults={"value": value}
        )

    def response(self, **overrides):
        data = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 1800,
            "token_type": "Bearer",
        }
        data.update(overrides)
        return Mock(status_code=200, json=lambda: data)

    def stored(self):
        return Setting.objects.get(key=CREDENTIAL_SETTING).value

    def expire(self, expires_at=0, access_token="old-access"):
        self.auth.save(
            self.plugin.plugin_config(),
            Credentials(access_token, "old-refresh", expires_at, "client-secret"),
        )

    def test_valid_token_reused(self):
        self.assertEqual(self.auth.access_token(), "old-access")
        self.request.assert_not_called()

    def test_expiry_threshold_and_missing_token(self):
        for offset, access, refresh in [
            (61, "old-access", False),
            (60, "old-access", True),
            (59, "old-access", True),
            (1800, "", True),
            (-1, "old-access", True),
        ]:
            with (
                self.subTest(offset=offset, access=access),
                patch(
                    "inventree_supplier_panel.digikey_auth.time.time", return_value=1000
                ),
            ):
                self.expire(1000 + offset, access)
                self.request.reset_mock()
                self.assertEqual(
                    self.auth.access_token(), "new-access" if refresh else "old-access"
                )
                self.assertEqual(self.request.call_count, int(refresh))

    def test_refresh_rotates_tokens_and_saves_expiry(self):
        self.expire()
        with patch(
            "inventree_supplier_panel.digikey_auth.time.time", return_value=1000
        ):
            self.assertEqual(self.auth.access_token(), "new-access")
        stored = self.auth.load(self.plugin.plugin_config())
        self.assertEqual(stored.refresh_token, "new-refresh")
        self.assertEqual(stored.expires_at, 2800)
        self.expire_with_credentials(stored)
        self.auth.access_token()
        self.assertEqual(self.request.call_args.args[1]["refresh_token"], "new-refresh")

    def expire_with_credentials(self, credentials):
        credentials.expires_at = 0
        self.auth.save(self.plugin.plugin_config(), credentials)

    def test_initial_authorization_saves_same_bundle(self):
        with patch(
            "inventree_supplier_panel.digikey_auth.time.time", return_value=1000
        ):
            self.auth.authorize("authorization-code", "https://example.test/callback")
        saved = self.auth.load(self.plugin.plugin_config())
        self.assertEqual(
            (saved.access_token, saved.refresh_token, saved.expires_at),
            ("new-access", "new-refresh", 2800),
        )
        self.assertEqual(
            self.request.call_args.args[1]["grant_type"], "authorization_code"
        )
        self.assertEqual(
            self.request.call_args.args[1]["redirect_uri"],
            "https://example.test/callback",
        )

    def test_missing_authorization_code_does_not_request(self):
        with self.assertRaisesRegex(DigikeyAuthError, "code is missing"):
            self.auth.authorize(None, "https://example.test/callback")
        self.request.assert_not_called()

    def test_bundle_is_encrypted_at_rest(self):
        for secret in ("old-access", "old-refresh", "client-secret"):
            self.assertNotIn(secret, self.stored())
        self.assertEqual(
            self.auth.load(self.plugin.plugin_config()).client_secret, "client-secret"
        )

    def test_migrates_plaintext_and_clears_legacy_settings(self):
        Setting.objects.filter(key=CREDENTIAL_SETTING).delete()
        for key, value in {
            "DIGIKEY_TOKEN": "legacy-access",
            "DIGIKEY_REFRESH_TOKEN": "legacy-refresh",
            "DIGIKEY_CLIENT_SECRET": "legacy-secret",
        }.items():
            self.set_setting(key, value)
        self.assertEqual(self.auth.access_token(), "new-access")
        self.assertEqual(
            self.request.call_args.args[1]["refresh_token"], "legacy-refresh"
        )
        self.assertEqual(
            self.request.call_args.args[1]["client_secret"], "legacy-secret"
        )
        self.assertEqual(self.plugin.get_setting("DIGIKEY_REFRESH_TOKEN"), "")
        self.assertEqual(self.plugin.get_setting("DIGIKEY_TOKEN"), "")
        self.assertEqual(self.plugin.get_setting("DIGIKEY_CLIENT_SECRET"), "")
        self.assertEqual(
            self.auth.load(self.plugin.plugin_config()).client_secret, "legacy-secret"
        )

    def test_new_secret_setting_replaces_encrypted_secret(self):
        self.set_setting("DIGIKEY_CLIENT_SECRET", "replacement-secret")
        self.auth.access_token()
        self.assertEqual(self.plugin.get_setting("DIGIKEY_CLIENT_SECRET"), "")
        self.assertEqual(
            self.auth.load(self.plugin.plugin_config()).client_secret,
            "replacement-secret",
        )

    def test_environment_secret_takes_precedence(self):
        self.expire()
        with patch.dict(os.environ, {"DIGIKEY_CLIENT_SECRET": "environment-secret"}):
            self.auth.access_token()
        self.assertEqual(
            self.request.call_args.args[1]["client_secret"], "environment-secret"
        )
        self.assertNotIn(
            "environment-secret",
            vars(self.auth.load(self.plugin.plugin_config())).values(),
        )

    def test_malformed_responses_preserve_credentials(self):
        self.expire()
        original = self.stored()
        for overrides in [
            {"access_token": None},
            {"access_token": ""},
            {"refresh_token": None},
            {"refresh_token": 42},
            {"expires_in": None},
            {"expires_in": 0},
            {"expires_in": -1},
            {"expires_in": True},
            {"expires_in": "1800"},
            {"expires_in": float("inf")},
            {"expires_in": float("nan")},
            {"expires_in": 10**400},
            {"token_type": "Basic"},
            {"token_type": None},
        ]:
            with self.subTest(overrides=overrides):
                self.request.return_value = self.response(**overrides)
                with self.assertRaises(DigikeyAuthError):
                    self.auth.access_token()
                self.assertEqual(self.stored(), original)
        for body in [[], None, "not-an-object"]:
            self.request.return_value = Mock(status_code=200, json=lambda: body)
            with self.assertRaises(DigikeyAuthError):
                self.auth.access_token()
            self.assertEqual(self.stored(), original)
        self.request.return_value = Mock(status_code=200)
        self.request.return_value.json.side_effect = ValueError("private response")
        with self.assertRaisesRegex(DigikeyAuthError, "invalid token response"):
            self.auth.access_token()
        self.assertEqual(self.stored(), original)

    def test_http_errors_and_revocation_are_sanitized(self):
        self.expire()
        original = self.stored()
        for status, body, expected in [
            (
                400,
                {"error": "invalid_grant", "error_description": "private-token"},
                "Create a new",
            ),
            (401, {}, "Create a new"),
            (500, {"access_token": "private-token"}, "request failed"),
            (
                400,
                {"StatusCode": 400, "ErrorDetails": "private-token"},
                "request failed",
            ),
        ]:
            with self.subTest(status=status, body=body):
                self.request.return_value = Mock(status_code=status, json=lambda: body)
                with self.assertRaisesRegex(DigikeyAuthError, expected) as caught:
                    self.auth.access_token()
                self.assertNotIn("private-token", str(caught.exception))
                self.assertEqual(self.stored(), original)

    def test_network_error_is_safe_and_does_not_retry(self):
        self.expire()
        original = self.stored()
        self.request.side_effect = ConnectionError("private-token")
        with self.assertRaisesRegex(DigikeyAuthError, "unreachable") as caught:
            self.auth.access_token()
        self.assertNotIn("private-token", str(caught.exception))
        self.assertEqual(self.stored(), original)
        self.request.assert_called_once()

    def test_failed_authorization_preserves_credentials(self):
        original = self.stored()
        self.request.return_value = self.response(refresh_token=None)
        with self.assertRaises(DigikeyAuthError):
            self.auth.authorize("code", "https://example.test/callback")
        self.assertEqual(self.stored(), original)

    def test_missing_refresh_token_requests_reauthorization(self):
        self.auth.save(self.plugin.plugin_config(), Credentials())
        with self.assertRaisesRegex(DigikeyAuthError, "Create a new"):
            self.auth.access_token()
        self.request.assert_not_called()

    def test_missing_or_changed_key_fails_closed(self):
        original = self.stored()
        for key in ["", "not-a-fernet-key", Fernet.generate_key().decode()]:
            with (
                self.subTest(key=key),
                patch.dict(os.environ, {"DIGIKEY_CREDENTIAL_KEY": key}),
            ):
                with self.assertRaises(DigikeyAuthError):
                    DigikeyAuth(self.plugin).access_token()
        self.assertEqual(self.stored(), original)
        self.request.assert_not_called()

    def test_save_failure_does_not_report_success(self):
        self.expire()
        original = self.stored()
        from django.db import DatabaseError

        with patch.object(
            self.auth, "save", side_effect=DatabaseError("private-token")
        ):
            with self.assertRaisesRegex(DigikeyAuthError, "could not be saved"):
                self.auth.access_token()
        self.assertEqual(self.stored(), original)

    def test_concurrent_connections_refresh_once(self):
        self.expire()
        original_response = self.response()

        def delayed(*args, **kwargs):
            time.sleep(0.1)
            return original_response

        def worker(_):
            try:
                return DigikeyAuth(Plugin()).access_token()
            finally:
                connections.close_all()

        self.request.side_effect = delayed
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(worker, range(4)))
        self.assertEqual(results, ["new-access"] * 4)
        self.request.assert_called_once()

    def test_independent_processes_refresh_once(self):
        self.expire()
        context = multiprocessing.get_context("spawn")
        calls, results = context.Queue(), context.Queue()
        workers = [
            context.Process(
                target=refresh_worker, args=(self.database, self.key, calls, results)
            )
            for _ in range(3)
        ]
        for worker in workers:
            worker.start()
        try:
            self.assertEqual(
                [results.get(timeout=20) for _ in workers], ["worker-access"] * 3
            )
            for worker in workers:
                worker.join(timeout=20)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(calls.get(timeout=2), "refresh")
            import queue

            with self.assertRaises(queue.Empty):
                calls.get(timeout=0.2)
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=5)
            calls.close()
            results.close()

    def test_secret_values_are_not_in_credentials_repr(self):
        stored = self.auth.load(self.plugin.plugin_config())
        for secret in ("old-access", "old-refresh", "client-secret"):
            self.assertNotIn(secret, repr(stored))

    def test_failed_migration_rolls_back_plaintext_changes(self):
        Setting.objects.filter(key=CREDENTIAL_SETTING).delete()
        self.set_setting("DIGIKEY_REFRESH_TOKEN", "legacy-refresh")
        self.set_setting("DIGIKEY_CLIENT_SECRET", "legacy-secret")
        self.request.return_value = self.response(refresh_token=None)
        with self.assertRaises(DigikeyAuthError):
            self.auth.access_token()
        self.assertFalse(Setting.objects.filter(key=CREDENTIAL_SETTING).exists())
        self.assertEqual(
            self.plugin.get_setting("DIGIKEY_REFRESH_TOKEN"), "legacy-refresh"
        )
        self.assertEqual(
            self.plugin.get_setting("DIGIKEY_CLIENT_SECRET"), "legacy-secret"
        )

    def digikey(self):
        common = types.ModuleType("common.models")
        common.InvenTreeSetting = Mock()
        common.InvenTreeSetting.get_setting.return_value = "USD"
        with patch.dict("sys.modules", {"common.models": common}):
            return importlib.import_module("inventree_supplier_panel.digikey")

    def test_search_uses_decrypted_token_without_refreshing(self):
        module = self.digikey()
        with patch.object(
            module.Wrappers,
            "get_request",
            return_value=Mock(
                json=lambda: {"status": 400, "title": "not found", "detail": ""}
            ),
        ) as get:
            result = module.Digikey.get_digikey_partdata_v4(self.plugin, "part", None)
        self.assertEqual(result["error_status"], "not found")
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"], "Bearer old-access"
        )
        self.request.assert_not_called()

    def test_cart_create_and_update_use_decrypted_tokens(self):
        module = self.digikey()
        order = Mock(reference="PO-1", metadata={})
        order.lines.all.return_value = []
        with patch.object(
            module.Wrappers, "get_request", return_value=Mock(content=b"true")
        ) as get:
            self.request.return_value = Mock(json=lambda: "list-id")
            cart = module.Digikey.create_digikey_cart(self.plugin, order)
        self.assertEqual(cart, {"ID": "list-id", "error_status": "OK"})
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"], "Bearer old-access"
        )
        self.assertEqual(
            self.request.call_args.kwargs["headers"]["Authorization"],
            "Bearer old-access",
        )
        with patch.object(
            module.Wrappers,
            "get_request",
            return_value=Mock(json=lambda: {"PartsList": []}),
        ) as get:
            cart = module.Digikey.update_digikey_cart(self.plugin, order, "list-id")
        self.assertEqual(cart["error_status"], "OK")
        self.assertNotIn("old-access", str(cart))
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"], "Bearer old-access"
        )
        self.assertEqual(
            self.request.call_args.args[3]["Authorization"], "Bearer old-access"
        )

    def test_auth_errors_stop_product_and_cart_requests(self):
        module = self.digikey()
        self.auth.save(self.plugin.plugin_config(), Credentials())
        order = Mock(reference="PO-1", metadata={})
        with patch.object(module.Wrappers, "get_request") as get:
            for result in [
                module.Digikey.get_digikey_partdata_v4(self.plugin, "part", None),
                module.Digikey.create_digikey_cart(self.plugin, order),
                module.Digikey.update_digikey_cart(self.plugin, order, "list-id"),
            ]:
                self.assertIn("Create a new", result["error_status"])
            get.assert_not_called()
        self.request.assert_not_called()

    def supplier_panel(self):
        definitions = {
            "common.models": {"InvenTreeSetting": Mock()},
            "company.models": {
                name: Mock()
                for name in (
                    "Company",
                    "ManufacturerPart",
                    "SupplierPart",
                    "SupplierPriceBreak",
                )
            },
            "order.models": {"PurchaseOrder": Mock()},
            "part.models": {"Part": Mock()},
            "users.permissions": {"check_user_role": Mock()},
            "plugin": {"InvenTreePlugin": type("InvenTreePlugin", (), {})},
            "plugin.mixins": {
                name: type(name, (), {})
                for name in ("SettingsMixin", "UrlsMixin", "UserInterfaceMixin")
            },
        }
        modules = {}
        for name, attributes in definitions.items():
            modules[name] = types.ModuleType(name)
            vars(modules[name]).update(attributes)
        with patch.dict("sys.modules", modules):
            module = importlib.import_module("inventree_supplier_panel.supplier_panel")
        module.InvenTreeSetting.get_setting.return_value = "https://example.test/"
        self.plugin.base_url = "plugin/suppliercart/"
        return module

    def test_callback_persists_expiry_and_never_returns_tokens(self):
        module = self.supplier_panel()
        request = Mock(GET={"code": "code"})
        request.user.is_authenticated = True
        request.user.is_staff = True
        response = module.SupplierCartPanel.receive_authcode(self.plugin, request)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"successfully received", response.content)
        for value in (b"new-access", b"new-refresh", b"client-secret"):
            self.assertNotIn(value, response.content)
        self.assertGreater(
            self.auth.load(self.plugin.plugin_config()).expires_at, time.time()
        )
        self.assertEqual(
            self.request.call_args.args[1]["redirect_uri"],
            "https://example.test/plugin/suppliercart/digikeytoken/",
        )

    def test_callback_rejects_non_admins(self):
        module = self.supplier_panel()
        for authenticated, staff in [(False, False), (True, False)]:
            request = Mock(GET={"code": "code"})
            request.user.is_authenticated = authenticated
            request.user.is_staff = staff
            response = module.SupplierCartPanel.receive_authcode(self.plugin, request)
            self.assertEqual(response.status_code, 403)
        self.request.assert_not_called()

    def test_callback_errors_do_not_echo_oauth_responses(self):
        module = self.supplier_panel()
        request = Mock(GET={"code": "code"})
        request.user.is_authenticated = True
        request.user.is_staff = True
        self.request.return_value = Mock(
            status_code=400,
            json=lambda: {
                "error": "invalid_grant",
                "error_description": "private-token",
            },
        )
        response = module.SupplierCartPanel.receive_authcode(self.plugin, request)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Create a new", response.content)
        self.assertNotIn(b"private-token", response.content)

    def test_admin_context_contains_no_credentials(self):
        module = self.supplier_panel()
        context = module.SupplierCartPanel.get_admin_context(self.plugin)
        for value in ("old-access", "old-refresh", "client-secret", self.stored()):
            self.assertNotIn(value, context.values())
        for key in ("DIGIKEY_TOKEN", "DIGIKEY_REFRESH_TOKEN", CREDENTIAL_SETTING):
            self.assertTrue(module.SupplierCartPanel.SETTINGS[key]["protected"])
            self.assertTrue(module.SupplierCartPanel.SETTINGS[key]["hidden"])
        self.assertTrue(
            module.SupplierCartPanel.SETTINGS["DIGIKEY_CLIENT_SECRET"]["protected"]
        )

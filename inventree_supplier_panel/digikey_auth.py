"""Server-side DigiKey OAuth credentials and refresh lifecycle."""

import json
import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import requests
from cryptography.fernet import Fernet, InvalidToken
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import F

from .request_wrappers import Wrappers

TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
CREDENTIAL_SETTING = "DIGIKEY_CREDENTIALS"
EXPIRY_MARGIN = 60
REAUTHORIZE = "DigiKey authorization is invalid or revoked. Create a new DigiKey token in plugin settings."


class DigikeyAuthError(Exception):
    """An actionable message safe to return to the frontend."""


@dataclass
class Credentials:
    access_token: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    expires_at: float = 0
    client_secret: str = field(default="", repr=False)

    def usable(self):
        return bool(self.access_token) and self.expires_at > time.time() + EXPIRY_MARGIN


class DigikeyAuth:
    def __init__(self, plugin):
        self.plugin = plugin
        key = os.environ.get("DIGIKEY_CREDENTIAL_KEY", "")
        try:
            self.cipher = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeError):
            raise DigikeyAuthError(
                "Configure a valid DIGIKEY_CREDENTIAL_KEY on every server worker before using DigiKey."
            ) from None

    @contextmanager
    def locked(self):
        from plugin.models import PluginConfig

        try:
            config = self.plugin.plugin_config()
            if config is None:
                raise DigikeyAuthError("DigiKey plugin configuration is missing.")
            with transaction.atomic():
                # A write locks the shared row even on SQLite, where
                # select_for_update alone provides no serialization.
                if not PluginConfig.objects.filter(pk=config.pk).update(
                    active=F("active")
                ):
                    raise DigikeyAuthError("DigiKey plugin configuration is missing.")
                yield config
        except (DatabaseError, ValidationError):
            raise DigikeyAuthError(
                "DigiKey credentials could not be saved or locked. Check the server database and try again."
            ) from None

    def load(self, config):
        from plugin.models import PluginSetting

        value = (
            PluginSetting.objects.filter(plugin=config, key=CREDENTIAL_SETTING)
            .values_list("value", flat=True)
            .first()
        )
        if not value:
            return Credentials()
        try:
            data = json.loads(self.cipher.decrypt(value.encode("ascii")))
            credentials = Credentials(**data)
            if (
                not all(
                    isinstance(value, str)
                    for value in (
                        credentials.access_token,
                        credentials.refresh_token,
                        credentials.client_secret,
                    )
                )
                or isinstance(credentials.expires_at, bool)
                or not isinstance(credentials.expires_at, (int, float))
                or not math.isfinite(credentials.expires_at)
            ):
                raise ValueError
            return credentials
        except (InvalidToken, ValueError, TypeError, UnicodeError, OverflowError):
            raise DigikeyAuthError(
                "DigiKey credentials cannot be decrypted. Restore the original DIGIKEY_CREDENTIAL_KEY."
            ) from None

    def save(self, config, credentials):
        from plugin.models import PluginSetting

        value = self.cipher.encrypt(json.dumps(vars(credentials)).encode()).decode(
            "ascii"
        )
        PluginSetting.objects.update_or_create(
            plugin=config, key=CREDENTIAL_SETTING, defaults={"value": value}
        )

    def migrate(self, config):
        from plugin.models import PluginSetting

        credentials = self.load(config)
        legacy = dict(
            PluginSetting.objects.filter(
                plugin=config,
                key__in=(
                    "DIGIKEY_TOKEN",
                    "DIGIKEY_REFRESH_TOKEN",
                    "DIGIKEY_CLIENT_SECRET",
                ),
            ).values_list("key", "value")
        )
        if any(legacy.values()):
            if not credentials.refresh_token:
                credentials.access_token = legacy.get("DIGIKEY_TOKEN") or ""
                credentials.refresh_token = legacy.get("DIGIKEY_REFRESH_TOKEN") or ""
                # Legacy tokens have no trustworthy expiry.
                credentials.expires_at = 0
            credentials.client_secret = (
                legacy.get("DIGIKEY_CLIENT_SECRET") or credentials.client_secret
            )
            self.save(config, credentials)
            PluginSetting.objects.filter(plugin=config, key__in=legacy).update(value="")
        return credentials

    def request_tokens(self, credentials, grant):
        client_id = self.plugin.get_setting("DIGIKEY_CLIENT_ID")
        client_secret = (
            os.environ.get("DIGIKEY_CLIENT_SECRET") or credentials.client_secret
        )
        if not client_id or not client_secret:
            raise DigikeyAuthError(
                "Configure the DigiKey client ID and client secret before authorizing."
            )
        started = time.time()
        try:
            response = Wrappers.post_request(
                self.plugin,
                {"client_id": client_id, "client_secret": client_secret, **grant},
                TOKEN_URL,
                headers={},
            )
        except (ConnectionError, requests.exceptions.RequestException):
            raise DigikeyAuthError(
                "DigiKey token endpoint is unreachable. Check the connection and try again."
            ) from None
        try:
            data = response.json()
        except ValueError:
            raise DigikeyAuthError(
                "DigiKey returned an invalid token response. Try again."
            ) from None
        if not isinstance(data, dict):
            raise DigikeyAuthError(
                "DigiKey returned an invalid token response. Try again."
            )
        if (
            data.get("error") in ("invalid_grant", "access_denied")
            or response.status_code == 401
        ):
            raise DigikeyAuthError(REAUTHORIZE)
        if response.status_code != 200 or data.get("error") or data.get("StatusCode"):
            raise DigikeyAuthError(
                "DigiKey token request failed. Check client credentials; if authorization was revoked, create a new DigiKey token."
            )
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = data.get("expires_in")
        try:
            valid = (
                isinstance(access_token, str)
                and bool(access_token.strip())
                and isinstance(refresh_token, str)
                and bool(refresh_token.strip())
                and not isinstance(expires_in, bool)
                and isinstance(expires_in, (int, float))
                and math.isfinite(expires_in)
                and expires_in > 0
                and isinstance(data.get("token_type", "Bearer"), str)
                and data.get("token_type", "Bearer").lower() == "bearer"
            )
        except OverflowError:
            valid = False
        if not valid:
            raise DigikeyAuthError(
                "DigiKey returned an invalid token response. Stored credentials were not replaced."
            )
        return Credentials(
            access_token, refresh_token, started + expires_in, credentials.client_secret
        )

    def access_token(self):
        # Always reload under the database lock, never from a process-local cache.
        with self.locked() as config:
            credentials = self.migrate(config)
            if credentials.usable():
                return credentials.access_token
            if not credentials.refresh_token:
                raise DigikeyAuthError(REAUTHORIZE)
            replacement = self.request_tokens(
                credentials,
                {
                    "refresh_token": credentials.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            self.save(config, replacement)
            return replacement.access_token

    def authorize(self, code, redirect_uri):
        if not isinstance(code, str) or not code.strip():
            raise DigikeyAuthError(
                "DigiKey authorization code is missing. Create a new DigiKey token."
            )
        with self.locked() as config:
            credentials = self.migrate(config)
            replacement = self.request_tokens(
                credentials,
                {
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            self.save(config, replacement)

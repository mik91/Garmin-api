"""Calls to Garmin's official Personal API (read-only), using Garmin's own client library.

The API key comes from GARMIN_PERSONAL_API_KEY (environment or .env) or Garmin's default
key file, ~/.config/garmin/connect-personal-api-key. Keys are created in Garmin Connect
under account settings > Personal API. Bearer tokens are cached by the library.
"""
from typing import Any, Callable

from garmin_personal_api import (
    GarminPersonalApiClient,
    GarminPersonalApiError,
    HttpError,
    PersonalApiAuthenticator,
    resolve_api_key,
)

from garmin_ai import config  # noqa: F401  (loads .env so the key variable is visible)


class PersonalApiUnavailable(Exception):
    """A user-facing reason the Personal API could not be used."""


def call(operation: Callable[[GarminPersonalApiClient], Any]) -> Any:
    """Run one read operation, retrying once with a fresh token if Garmin rejects a cached one."""
    try:
        key = resolve_api_key()
        with PersonalApiAuthenticator() as auth:
            for force_refresh in (False, True):
                token = auth.obtain_token(key, force_refresh=force_refresh)
                with GarminPersonalApiClient(token.access_token) as client:
                    try:
                        return operation(client)
                    except HttpError as error:
                        if error.status_code != 401 or force_refresh:
                            raise
    except HttpError as error:
        if error.status_code in (400, 401, 403):
            raise PersonalApiUnavailable(
                f"Garmin rejected the Personal API key ({error.status_code}). It may have expired "
                "or been deleted; create a new one in Garmin Connect."
            ) from error
        raise PersonalApiUnavailable(f"Garmin Personal API error {error.status_code}.") from error
    except GarminPersonalApiError as error:
        raise PersonalApiUnavailable(str(error)) from error

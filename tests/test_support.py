"""Ephemeral credentials and non-routable addresses for isolated tests."""

from contextlib import contextmanager
from secrets import token_urlsafe
from unittest.mock import patch


# The account password policy needs a letter and a number; a random token alone can lack a digit.
TEST_PASSWORD = f"fixture-{token_urlsafe(24)}-7"
TEST_OIDC_CLIENT_SECRET = token_urlsafe(24)
TEST_OIDC_SESSION_SECRET = token_urlsafe(32)
TEST_CAMERA_PASSWORD = token_urlsafe(24)
TEST_URL_TOKEN = token_urlsafe(18)


def setup_code() -> str:
    """The one-time code first-time setup needs (#542), as the installer reads it from the NVR's log: here, from where the NVR keeps it
    for the database and secrets folder the test has set up."""
    from app import setup_code as code

    return code.expected() or ""

# RFC 5737 TEST-NET-2 addresses are documentation-only and never routed.
TEST_CAMERA_IP = "198.51.100.25"
TEST_CAMERA_IP_A = "198.51.100.10"
TEST_CAMERA_IP_B = "198.51.100.11"
TEST_CAMERA_IP_C = "198.51.100.12"
TEST_CAMERA_IP_D = "198.51.100.20"
TEST_CAMERA_IP_E = "198.51.100.21"


@contextmanager
def patch_camera_probe(**kwargs):
    """Replace the stream probe everywhere cameras are tested before saving."""
    with patch("app.api.cameras.probe_stream", **kwargs) as probe, patch("app.api.onboarding.probe_stream", new=probe):
        yield probe


def principal(user_id: int, username: str, role: str):
    """The signed-in user as the API sees one, for tests that fake authentication."""
    from app.principal import Principal
    return Principal(id=user_id, username=username, role=role)


def as_given(url, allow_private=False):
    """A destination exactly as written, for tests whose stand-in receivers listen on loopback, which real destinations may never be: the
    stand-in for ``webhooks.pin_target_url`` as the old check's stand-in was ``lambda url, allow_private: url`` (#531)."""
    from urllib.parse import urlsplit

    from app.webhooks import Pinned

    return Pinned(url=url, host=urlsplit(url).netloc, sni=None)

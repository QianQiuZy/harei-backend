import aiohttp
import pytest

from blivedm.models import send_gift_v2
from app.services import bili_captain_listener


SEND_GIFT_V2_FIXTURE = (
    "CMDEBxIM5rWL6K+V55So5oi3GgBCCAgAKAAyAFgASgoIABAAGgAqADAAUi8IARIM"
    "5Y+j5rC06buE6LGGGAMgAChkMKwCOABCAEoAUIC8lLQGYgCSAQCaAgIKAA=="
)


def test_send_gift_v2_decodes_as_gift_message() -> None:
    # Given a current Bilibili SEND_GIFT_V2 protobuf event.
    command: send_gift_v2.SendGiftV2Command = {"pb": SEND_GIFT_V2_FIXTURE}

    # When the synchronized protobuf model decodes the event.
    message = send_gift_v2.SendGiftV2Message.from_command(command)

    # Then downstream gift handling receives the normalized event fields.
    assert message.gift_name == "口水黄豆"
    assert message.num == 3
    assert message.uid == 123456


@pytest.mark.asyncio
async def test_bili_session_keeps_default_tls_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an instrumented connector factory.
    connector_ssl_options: list[bool] = []
    connector_factory = aiohttp.TCPConnector

    def record_connector_options(*, ssl: bool = True) -> aiohttp.TCPConnector:
        connector_ssl_options.append(ssl)
        return connector_factory(ssl=ssl)

    monkeypatch.setattr(aiohttp, "TCPConnector", record_connector_options)

    # When the cookie-bearing Bilibili session is initialized.
    bili_captain_listener.init_session()
    session = bili_captain_listener.aiohttp_session
    assert session is not None
    try:
        # Then TLS certificate verification is not disabled.
        assert False not in connector_ssl_options
    finally:
        await session.close()
        bili_captain_listener.aiohttp_session = None

"""Exercise the shared SMTP submission window without delivering external email."""

import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from redis import Redis
from redis.exceptions import ConnectionError

from app.core import redis as redis_clients
from app.services import gift_mail, member_security


def test_noreply_window_across_senders_and_cc(tmp_path, monkeypatch):
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("redis-server is not installed")
    socket = tmp_path / "mail.sock"
    proc = subprocess.Popen(
        [
            binary,
            "--port",
            "0",
            "--unixsocket",
            str(socket),
            "--save",
            "",
            "--appendonly",
            "no",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    peer = Redis(unix_socket_path=str(socket), db=2)
    monkeypatch.setattr(redis_clients, "_sync_rate_client", None)
    monkeypatch.setattr(
        redis_clients,
        "get_settings",
        lambda: SimpleNamespace(
            redis_url="redis://unavailable.invalid/0",
            gift_mail_rate_redis_url=SecretStr(f"unix://{socket}?db=5"),
        ),
    )
    try:
        for _ in range(100):
            if socket.exists():
                break
            time.sleep(0.02)
        original = redis_clients.get_sync_rate_limit_redis()
        assert original.connection_pool.connection_kwargs["db"] == 2
        assert original.ping() and peer.ping()
        settings = SimpleNamespace(
            gift_email_from=gift_mail.NOREPLY,
            gift_smtp_host="smtp.example.test",
            gift_smtp_port=465,
            gift_smtp_ca_file="",
            gift_smtp_pass=SimpleNamespace(get_secret_value=lambda: ""),
            gift_roster_recipient="shipping@example.test",
        )
        lock = threading.Lock()
        entries = []

        def capture(_message, *, from_addr, to_addrs):
            with lock:
                entries.append((time.monotonic(), len(to_addrs)))

        smtp = Mock()
        smtp.send_message.side_effect = capture
        context = Mock()
        context.__enter__ = Mock(return_value=smtp)
        context.__exit__ = Mock(return_value=False)
        monkeypatch.setattr(gift_mail.smtplib, "SMTP_SSL", lambda *a, **k: context)
        monkeypatch.setattr(member_security, "get_settings", lambda: settings)

        def send(n):
            if n == 0:
                gift_mail.send_gift_message(
                    settings, EmailMessage(), [f"cc{i}@example.test" for i in range(24)]
                )
            elif n % 3 == 0:
                gift_mail.send_roster("203112", b"test", settings=settings)
            elif n % 3 == 1:
                member_security.smtp_send(f"otp{n}@example.test", "test", "code", True)
            else:
                # The cookie alert may use a different envelope address with the noreply login.
                gift_mail.send_smtp_message(
                    smtp,
                    EmailMessage(),
                    "alert@example.test",
                    ["to@example.test"],
                    login="NoReply@Harei.cn",
                )

        clients = iter([original, peer] * 25)
        monkeypatch.setattr(
            gift_mail, "get_sync_rate_limit_redis", lambda: next(clients)
        )
        with ThreadPoolExecutor(max_workers=25) as pool:
            list(pool.map(send, range(23)))
        times = [when for when, count in sorted(entries) for _ in range(count)]
        assert len(times) == 46
        assert all(times[i + 20] - times[i] >= 1 for i in range(len(times) - 20))
        assert peer.zcard(gift_mail.MAIL_WINDOW_KEY) > 0
    finally:
        if redis_clients._sync_rate_client:
            redis_clients._sync_rate_client.close()
        peer.close()
        proc.terminate()
        proc.wait(timeout=5)


def test_noreply_redis_failure_prevents_submission(monkeypatch):
    client = Mock()
    client.eval.side_effect = ConnectionError("offline")
    monkeypatch.setattr(gift_mail, "get_sync_rate_limit_redis", lambda: client)
    smtp = Mock()
    with pytest.raises(ConnectionError):
        gift_mail.send_smtp_message(
            smtp, EmailMessage(), gift_mail.NOREPLY, ["to@example.test"]
        )
    smtp.send_message.assert_not_called()
    gift_mail.send_smtp_message(
        smtp, EmailMessage(), "other@example.test", ["to@example.test"]
    )
    smtp.send_message.assert_called_once()

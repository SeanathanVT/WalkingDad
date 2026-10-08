import errno
import os
import socket

import pytest

import run


def _ipv6_available():
    try:
        socket.create_server(("::1", 0), family=socket.AF_INET6).close()
        return True
    except OSError:
        return False


needs_ipv6 = pytest.mark.skipif(not _ipv6_available(), reason="IPv6 loopback unavailable")


def _free_port(host="127.0.0.1", family=socket.AF_INET):
    with socket.create_server((host, 0), family=family) as s:
        return s.getsockname()[1]


def _config(monkeypatch, host, port):
    monkeypatch.setattr(run, "HOST", host)
    monkeypatch.setattr(run, "PORT", port)


def test_exits_when_port_taken(monkeypatch):
    with socket.create_server(("127.0.0.1", 0)) as s:
        _config(monkeypatch, "127.0.0.1", s.getsockname()[1])
        with pytest.raises(SystemExit, match="another WalkingDad"):
            run.check_port()


def test_passes_when_free(monkeypatch):
    _config(monkeypatch, "127.0.0.1", _free_port())
    run.check_port()


@needs_ipv6
@pytest.mark.parametrize("host", ["::1", "[::1]"])
def test_ipv6_literal_passes_when_free(monkeypatch, host):
    _config(monkeypatch, host, _free_port("::1", socket.AF_INET6))
    run.check_port()


@needs_ipv6
def test_ipv6_literal_exits_when_taken(monkeypatch):
    with socket.create_server(("::1", 0), family=socket.AF_INET6) as s:
        _config(monkeypatch, "::1", s.getsockname()[1])
        with pytest.raises(SystemExit, match="another WalkingDad"):
            run.check_port()


@needs_ipv6
def test_wildcard_probes_every_address(monkeypatch):
    # "*" resolves to both 0.0.0.0 and ::; a listener on only the IPv6 one
    # must still be caught, since Waitress binds both.
    with socket.create_server(("::", 0), family=socket.AF_INET6) as s:
        port = s.getsockname()[1]
        _config(monkeypatch, "*", port)
        assert {f for f, _ in run._listen_addrs()} == {socket.AF_INET, socket.AF_INET6}
        with pytest.raises(SystemExit, match="another WalkingDad"):
            run.check_port()


def test_duplicate_addresses_probed_once(monkeypatch):
    v6 = socket.AF_INET6
    fake = [(v6, 1, 6, "", ("fe80::1%en0", 5001, 0, 4)),
            (v6, 1, 6, "", ("fe80::1%en1", 5001, 0, 5)),
            (socket.AF_INET, 1, 6, "", ("127.0.0.1", 5001))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: fake)
    _config(monkeypatch, "anything", 5001)
    assert run._listen_addrs() == [(v6, fake[0][4]), (socket.AF_INET, fake[2][4])]


def test_unresolvable_host(monkeypatch):
    def fail(*a, **k):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    _config(monkeypatch, "no-such-host.invalid", 5001)
    with pytest.raises(SystemExit, match="doesn't resolve"):
        run.check_port()


@pytest.mark.parametrize("port", [0, 70000, -1, "5001", True, 5001.0])
def test_rejects_invalid_port(monkeypatch, port):
    _config(monkeypatch, "127.0.0.1", port)
    with pytest.raises(SystemExit, match="1 to 65535"):
        run.check_port()


@pytest.mark.parametrize(("err", "hint"), [
    (errno.EACCES, "doesn't allow this port"),
    (errno.EADDRNOTAVAIL, 'Check "host" and "port"'),
])
def test_other_bind_errors_get_their_own_hint(monkeypatch, err, hint):
    _config(monkeypatch, "127.0.0.1", _free_port())

    def refuse(*a, **k):
        raise OSError(err, os.strerror(err))

    monkeypatch.setattr(run.socket, "create_server", refuse)
    with pytest.raises(SystemExit, match=hint):
        run.check_port()

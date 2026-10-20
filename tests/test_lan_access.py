import pytest
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.security import CrossSiteGuard, HostGuard, TokenGuard


async def test_lan_admin_can_keep_gateway_listeners_on_loopback(tmp_path):
    settings = Settings(
        _env_file=None, master_secret=SecretStr('test-master'),
        db_path=tmp_path / 'proxies.db', bind_host='0.0.0.0',  # noqa: S104
        gateway_bind_host='127.0.0.1', gateway_http_port=0, gateway_socks_port=0,
        allow_remote_bind=True, admin_token=SecretStr('test-token'),
        admin_allowed_hosts=['proxy.lan'],
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        status = app.state.runtime.gateway
        assert status.http.bound.startswith('127.0.0.1:')
        assert status.socks5.bound.startswith('127.0.0.1:')


@pytest.mark.parametrize(('host', 'origin', 'token', 'accepted'), [
    ('proxy.lan', 'http://proxy.lan:5001', 'test-token', True),
    ('proxy.lan', 'http://proxy.lan:5001', '', False),
    ('proxy.lan', 'http://evil.invalid', 'test-token', False),
    ('evil.invalid', 'http://proxy.lan:5001', 'test-token', False),
    ('proxy.lan', 'null', 'test-token', False),
])
async def test_websocket_obeys_admin_guards(host, origin, token, accepted):
    messages = []

    async def endpoint(scope, receive, send):
        await send({'type': 'websocket.accept'})

    async def receive():
        return {'type': 'websocket.connect'}

    async def send(message):
        messages.append(message)

    allowed = frozenset({'proxy.lan'})
    app = HostGuard(CrossSiteGuard(TokenGuard(endpoint, token='test-token'), allowed), allowed)
    await app({
        'type': 'websocket', 'path': '/ws/events',
        'headers': [(b'host', host.encode()), (b'origin', origin.encode()),
                    (b'cookie', ('proxy_rotator_admin=' + token).encode())],
    }, receive, send)
    assert messages[0]['type'] == ('websocket.accept' if accepted else 'websocket.close')

"""Gateway responses are bounded before they are materialized in memory."""
import httpx
import pytest

from services.scene import provider
from services.scene.versioning import SceneError

REAL_HTTPX_CLIENT = httpx.Client


class CountingStream(httpx.SyncByteStream):
    def __init__(self, chunks, *, fail_after=None):
        self.chunks = chunks
        self.fail_after = fail_after
        self.read_count = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            if self.fail_after == self.read_count:
                raise httpx.ReadError('connection dropped')
            yield chunk

    def close(self):
        pass


def _gateway(monkeypatch, handler):
    def mock_client(**kwargs):
        return REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(provider.httpx, 'Client', mock_client)


def test_model_listing_stops_reading_after_decoded_byte_limit(monkeypatch):
    stream = CountingStream([b'x' * (512 * 1024)] * 10)
    _gateway(monkeypatch, lambda request: httpx.Response(200, stream=stream))
    with pytest.raises(SceneError) as raised:
        provider.list_models('https://fixture.invalid/v1', 'test-key')
    assert raised.value.code == 'MODEL_CHECK_FAILED'
    assert stream.read_count == 3


def test_paid_response_stops_reading_and_requires_charge_ack_on_retry(monkeypatch):
    stream = CountingStream([b'x' * (1024 * 1024)] * 50)
    _gateway(monkeypatch, lambda request: httpx.Response(200, stream=stream))
    with pytest.raises(SceneError) as raised:
        provider._post('https://fixture.invalid/v1', 'test-key', 'chat/completions', {})
    assert raised.value.code == 'MODEL_RESPONSE_TOO_LARGE'
    assert stream.read_count == 26


def test_paid_stream_disconnect_is_unknown_and_error_body_is_not_read(monkeypatch):
    broken = CountingStream([b'{', b'"ok":true}'], fail_after=2)
    _gateway(monkeypatch, lambda request: httpx.Response(200, stream=broken))
    with pytest.raises(provider.ProviderOutcomeUnknown):
        provider._post('https://fixture.invalid/v1', 'test-key', 'chat/completions', {})
    assert broken.read_count == 2

    _gateway(monkeypatch, lambda request: httpx.Response(200,
        headers={'content-encoding': 'gzip'}, content=b'not gzip'))
    with pytest.raises(provider.ProviderOutcomeUnknown):
        provider._post('https://fixture.invalid/v1', 'test-key', 'chat/completions', {})

    forbidden = CountingStream([b'should not be read'])
    _gateway(monkeypatch, lambda request: httpx.Response(401, stream=forbidden))
    with pytest.raises(SceneError) as raised:
        provider._post('https://fixture.invalid/v1', 'test-key', 'chat/completions', {})
    assert raised.value.code == 'MODEL_AUTH_FAILED'
    assert forbidden.read_count == 0


def test_gateway_success_keeps_request_id_and_usage(monkeypatch):
    _gateway(monkeypatch, lambda request: httpx.Response(200,
        json={'data': [{'id': 'fixture-model'}]}))
    assert provider.list_models('https://fixture.invalid/v1', 'test-key') == ['fixture-model']
    _gateway(monkeypatch, lambda request: httpx.Response(200,
        headers={'x-request-id': 'request-123'},
        json={'choices': [{'message': {'content': '{"answer":1}'}}], 'usage': {'total_tokens': 3}}))
    result, request_id, usage = provider.chat_json('https://fixture.invalid/v1', 'test-key',
        'fixture-model', 'system', 'user')
    assert result == {'answer': 1}
    assert request_id == 'request-123'
    assert usage == {'total_tokens': 3}

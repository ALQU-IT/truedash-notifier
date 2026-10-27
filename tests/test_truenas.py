"""truenas.Client login against a local fake middleware socket."""
import asyncio
import json
import ssl

import pytest
import websockets
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import datetime

import truenas


def _tls(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nas")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    return ctx


def run_against(tmp_path, handler_answers, username):
    """handler_answers: method -> result dict ({"result": ...} or {"error": ...})."""
    calls = []

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            calls.append(msg["method"])
            answer = handler_answers.get(msg["method"], {"result": []})
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], **answer}))

    async def main():
        async with websockets.serve(handler, "127.0.0.1", 0, ssl=_tls(tmp_path)) as server:
            port = server.sockets[0].getsockname()[1]
            async with truenas.connect("127.0.0.1", port, "key", verify=False, username=username) as tn:
                return await tn.pools()

    return asyncio.run(main()), calls


def test_login_ex_used_with_username(tmp_path):
    result, calls = run_against(tmp_path, {
        "auth.login_ex": {"result": {"response_type": "SUCCESS"}},
        "pool.query": {"result": [{"name": "tank"}]},
    }, "truedash")
    assert calls == ["auth.login_ex", "pool.query"]
    assert result == [{"name": "tank"}]


def test_old_server_falls_back(tmp_path):
    _, calls = run_against(tmp_path, {
        "auth.login_ex": {"error": {"code": -32601, "message": "Method not found"}},
        "auth.login_with_api_key": {"result": True},
    }, "truedash")
    assert calls == ["auth.login_ex", "auth.login_with_api_key", "pool.query"]


def test_refusal_does_not_fall_back(tmp_path):
    with pytest.raises(truenas.TrueNASError):
        run_against(tmp_path, {"auth.login_ex": {"result": {"response_type": "AUTH_ERR"}}}, "truedash")


def test_no_username_uses_old_login(tmp_path):
    _, calls = run_against(tmp_path, {"auth.login_with_api_key": {"result": True}}, None)
    assert calls == ["auth.login_with_api_key", "pool.query"]


def test_local_host_never_verifies():
    assert truenas.Client(truenas.LOCAL_HOST, 443, "k", verify=True)._verify is False
    assert truenas.Client("nas.lan", 443, "k", verify=True)._verify is True

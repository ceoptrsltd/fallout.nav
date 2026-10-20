import io
import time

from core_helpers import login, make_opts, running_app

from fnav import paths
from fnav.core import auth as auth_mod
from fnav.core import passwd
from fnav.core import secretsfile as sf


def test_hash_and_verify():
    h = sf.hash_password("secret-123", n=2**10)
    assert h.startswith("scrypt$1024$8$1$")
    assert sf.verify_password("secret-123", h)
    assert not sf.verify_password("secret-124", h)
    assert not sf.verify_password("x", "garbage")
    assert not sf.verify_password("x", None)
    assert not sf.verify_password("x", "scrypt$abc$8$1$AA==$AA==")


def test_secrets_file_roundtrip(tmp_path):
    p = tmp_path / "secrets.env"
    p.write_text("# comment\nOTHER=1\nexport FNAV_SESSION_SECRET=abc\n", encoding="utf-8")
    sf.update_secrets({sf.KEY_HASH: "scrypt$1$2$3$a$b"}, p)
    data = sf.read_secrets(p)
    assert data == {"OTHER": "1", sf.KEY_SECRET: "abc", sf.KEY_HASH: "scrypt$1$2$3$a$b"}
    text = p.read_text(encoding="utf-8")
    assert "# comment" in text and "FNAV_PASSWORD_HASH='scrypt$1$2$3$a$b'" in text


def test_session_tokens(tmp_path):
    a = auth_mod.AuthManager(secrets_path=tmp_path / "s.env")
    a.load(create_default_password="pw-123456")
    tok = a.issue_token()
    assert a.check_token(tok)
    assert not a.check_token(tok + "x")
    assert not a.check_token(tok, now=time.time() + auth_mod.SESSION_TTL_S + 10)
    assert not a.check_token("v1.1.2")
    # смена пароля делает старые токены недействительными
    a.password_hash = sf.hash_password("other-pass", n=2**10)
    assert not a.check_token(tok)


def test_passwd_cli(tmp_path, monkeypatch):
    p = tmp_path / "secrets.env"
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"my new pass\n")))
    assert passwd.main(["--secrets", str(p)]) == 0
    data = sf.read_secrets(p)
    assert sf.verify_password("my new pass", data[sf.KEY_HASH])
    secret = data[sf.KEY_SECRET]
    assert secret
    # повторный запуск: секрет сессий сохраняется
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"another-pass")))
    assert passwd.main(["--secrets", str(p)]) == 0
    data2 = sf.read_secrets(p)
    assert data2[sf.KEY_SECRET] == secret
    assert sf.verify_password("another-pass", data2[sf.KEY_HASH])
    # короткий пароль
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"123")))
    assert passwd.main(["--secrets", str(p)]) == 2


async def test_login_flow(data_dir):
    async with running_app() as c:
        assert (data_dir / "secrets.env").exists()  # mock создал dev-пароль
        r = await c.get("/api/session")
        assert r.json()["authenticated"] is False
        assert (await c.get("/api/settings")).status_code == 401
        assert (await c.get("/api/system/status")).status_code == 401
        r = await c.get("/admin/index.html")
        assert r.status_code == 302 and r.headers["location"].startswith("/admin/login.html?next=")
        # страница логина и её статика — без входа (файла может не быть → 404, но не 401/302)
        assert (await c.get("/admin/login.html")).status_code in (200, 404)
        assert (await c.get("/admin/app.css")).status_code in (200, 404)

        r = await c.post("/api/login", json={"password": "wrong"})
        assert r.status_code == 401 and r.json()["detail"]
        await login(c)
        assert "fnav_session" in c.cookies
        assert (await c.get("/api/session")).json()["authenticated"] is True
        assert (await c.get("/api/settings")).status_code == 200

        r = await c.post("/api/logout")
        assert r.status_code == 200
        c.cookies.clear()
        assert (await c.get("/api/settings")).status_code == 401


async def test_bruteforce_lockout(data_dir, monkeypatch):
    async with running_app() as c:
        for _ in range(auth_mod.MAX_FAILURES):
            assert (await c.post("/api/login", json={"password": "bad"})).status_code == 401
        r = await c.post("/api/login", json={"password": "fnav-dev"})
        assert r.status_code == 429 and "Retry-After" in r.headers
        # по истечении паузы — снова можно
        a = c.app.state.core.auth
        a._attempts["10.0.0.5"].locked_until = 0.0
        await login(c)


async def test_localhost_exemptions(data_dir):
    async with running_app(client_addr=("127.0.0.1", 40000)) as c:
        assert (await c.get("/api/settings")).status_code == 200
        assert (await c.patch("/api/settings", json={})).status_code == 401
        assert (await c.get("/api/recordings")).status_code == 401
        assert (await c.get("/display/index.html")).status_code in (200, 404)
    # trust_localhost=False — те же запросы требуют входа
    async with running_app(make_opts(trust_localhost=False), client_addr=("127.0.0.1", 40000)) as c:
        assert (await c.get("/api/settings")).status_code == 401


async def test_change_password(data_dir):
    async with running_app() as c:
        await login(c)
        r = await c.post("/api/system/password", json={"old": "nope", "new": "abcdef"})
        assert r.status_code == 400
        r = await c.post("/api/system/password", json={"old": "fnav-dev", "new": "abc"})
        assert r.status_code == 400
        r = await c.post("/api/system/password", json={"old": "fnav-dev", "new": "new-password"})
        assert r.status_code == 200
        # текущая сессия переоформлена — работает
        assert (await c.get("/api/session")).json()["authenticated"] is True
        c.cookies.clear()
        assert (await c.post("/api/login", json={"password": "fnav-dev"})).status_code == 401
        await login(c, "new-password")
    assert sf.verify_password("new-password", sf.read_secrets(paths.secrets_file())[sf.KEY_HASH])


async def test_no_password_non_mock(data_dir):
    async with running_app(make_opts(mock=False, hub_factory=lambda s: _mock_hub(s))) as c:
        r = await c.post("/api/login", json={"password": "fnav-dev"})
        assert r.status_code == 503


def _mock_hub(s):
    from fnav.mock.sensors import MockSensorHub

    return MockSensorHub(s)

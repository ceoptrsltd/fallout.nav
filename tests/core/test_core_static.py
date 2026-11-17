"""Статика админки: заголовки кэша, метка версии web/admin/version.json (tools/web-version.sh)."""

from core_helpers import running_app

from fnav import paths


def _web(tmp_path):
    web = tmp_path / "web"
    (web / "admin" / "js").mkdir(parents=True)
    (web / "display").mkdir()
    (web / "admin" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (web / "admin" / "js" / "shell.js").write_text("export {};", encoding="utf-8")
    (web / "admin" / "version.json").write_text('{"version": "abc123"}', encoding="utf-8")
    return web


async def test_static_version_json_no_store(data_dir, tmp_path, monkeypatch):
    web = _web(tmp_path)
    monkeypatch.setattr(paths, "web_dir", lambda: web)
    async with running_app() as c:
        # без входа: открытая админка проверяет метку и на странице, и после истечения сессии
        r = await c.get("/admin/version.json")
        assert r.status_code == 200
        assert r.json() == {"version": "abc123"}
        assert r.headers["cache-control"] == "no-store"
        # остальная статика — с перепроверкой (ETag), как раньше
        r = await c.get("/admin/js/shell.js")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
        # повторный запрос с ETag: 304, метка всё равно не кэшируется
        etag = (await c.get("/admin/version.json")).headers["etag"]
        r = await c.get("/admin/version.json", headers={"If-None-Match": etag})
        assert r.status_code == 304 and r.headers["cache-control"] == "no-store"


async def test_static_version_json_missing(data_dir, tmp_path, monkeypatch):
    web = _web(tmp_path)
    (web / "admin" / "version.json").unlink()
    monkeypatch.setattr(paths, "web_dir", lambda: web)
    async with running_app() as c:
        # нет метки (dev, mock) → 404, админка просто не проверяет версию
        assert (await c.get("/admin/version.json")).status_code == 404


async def test_static_gzip(data_dir, tmp_path, monkeypatch):
    """Текст и шрифты — сжатыми, если браузер принимает gzip; ETag тот же, 304 работает, мелочь и картинки как есть."""
    web = _web(tmp_path)
    big = "export const x = 1; // текст\n" * 400
    (web / "admin" / "js" / "big.js").write_bytes(big.encode())
    (web / "admin" / "pic.png").write_bytes(b"\x89PNG" + bytes(4000))
    monkeypatch.setattr(paths, "web_dir", lambda: web)
    async with running_app() as c:
        plain = await c.get("/admin/js/big.js", headers={"Accept-Encoding": "identity"})
        assert plain.status_code == 200 and "content-encoding" not in plain.headers
        assert plain.headers["vary"] == "Accept-Encoding"
        assert plain.text == big
        gz = await c.get("/admin/js/big.js", headers={"Accept-Encoding": "gzip, deflate, br"})
        assert gz.status_code == 200 and gz.headers["content-encoding"] == "gzip"
        assert gz.text == big  # httpx распаковал
        assert int(gz.headers["content-length"]) < len(big.encode()) // 5
        assert gz.headers["etag"] == plain.headers["etag"] and gz.headers["cache-control"] == "no-cache"
        assert gz.headers["content-type"] == plain.headers["content-type"]
        assert gz.headers["vary"] == "Accept-Encoding"
        # перепроверка: 304 без тела
        r = await c.get("/admin/js/big.js", headers={"Accept-Encoding": "gzip", "If-None-Match": gz.headers["etag"]})
        assert r.status_code == 304 and not r.content
        # файл изменился — сжатая копия новая
        new = "export const y = 2;\n" * 500
        (web / "admin" / "js" / "big.js").write_bytes(new.encode())
        assert (await c.get("/admin/js/big.js", headers={"Accept-Encoding": "gzip"})).text == new
        # мелкий файл и картинка — как есть
        r = await c.get("/admin/js/shell.js", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200 and "content-encoding" not in r.headers
        r = await c.get("/admin/pic.png", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200 and "content-encoding" not in r.headers and len(r.content) == 4004
        # метка версии: no-store остаётся
        assert (await c.get("/admin/version.json", headers={"Accept-Encoding": "gzip"})).headers["cache-control"] == "no-store"

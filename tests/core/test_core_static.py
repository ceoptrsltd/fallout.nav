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

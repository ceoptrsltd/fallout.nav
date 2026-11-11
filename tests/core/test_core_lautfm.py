"""laut.fm — второй каталог радио (fnav/core/lautfm.py) и его подмешивание в RadioDirectory."""

from __future__ import annotations

import httpx

from fnav.core import lautfm as lf
from fnav.core.radio import RadioDirectory
from fnav.models import RadioStation


def _st(name, genres, pos, **kw):
    return {"name": name, "display_name": name.title(), "stream_url": f"https://{name}.stream.laut.fm/{name}",
            "page_url": f"https://laut.fm/{name}", "genres": genres, "position": pos,
            "images": {"station_120x120": f"https://assets.laut.fm/{name}?t=_120x120"}, **kw}


JAZZ = [
    _st("late", ["Jazz"], 900),
    _st("jazzloft", ["Jazz", "Lounge"], 106),
    _st("third", ["Pop", "Rock", "Jazz"], 5),  # джаз — третий жанр: станция не про него
    _st("outside", ["Jazz"], 1500),  # вне первой тысячи чарта
    _st("schlagerjazz", ["Jazz", "Schlager"], 20),  # немецкоязычный жанр
    _st("deutschjazz", ["Jazz"], 30),  # немецкое название
    _st("truckjazz", ["Jazz"], 40, description="Bekannt aus ETS2 und ATS."),  # радио симулятора
    _st("moderated", ["Jazz"], 45, format="Live moderiert"),  # с ведущими
    _st("off", ["Jazz"], 50, active=False),
    _st("nopos", ["Jazz"], None),
]


def _laut_handler(calls, *, down=False):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host != "api.laut.fm":
            return None  # not ours
        calls.append(req.url.path)
        if down:
            return httpx.Response(503)
        if req.url.path == "/stations/genre/Jazz":
            return httpx.Response(200, json=JAZZ)
        if req.url.path.startswith("/stations/genre/"):
            return httpx.Response(200, json=[])
        if req.url.path == "/search/stations":
            items = [{"station": _st("jazzloft", ["Jazz"], 106)}, {"station": _st("deutschjazz", ["Jazz"], 30)}]
            return httpx.Response(200, json={"results": [{"categories": [["name", "jazz"]], "items": items},
                                                         {"categories": [["genre", "jazz"]],
                                                          "items": [{"station": _st("other", ["Jazz"], 7)}]}]})
        return httpx.Response(404)

    return handler


RB = [{"stationuuid": f"u{i}", "name": f"RB {i}", "url": f"http://rb{i}.example/s"} for i in range(6)]
RB_LAUT = {"stationuuid": "u-loft", "name": "Jazzloft", "url": "https://jazzloft.stream.laut.fm/jazzloft"}


def _directory(*, rb_up=True, laut_down=False, rb=None):
    laut_calls: list[str] = []
    laut_h = _laut_handler(laut_calls, down=laut_down)

    def handler(req: httpx.Request) -> httpx.Response:
        r = laut_h(req)
        if r is not None:
            return r
        if not rb_up:
            return httpx.Response(503)
        if req.url.path == "/json/servers":
            return httpx.Response(200, json=[{"name": "de1.api.radio-browser.info"}])
        if req.url.path == "/json/stations/search":
            return httpx.Response(200, json=RB if rb is None else rb)
        return httpx.Response(404)

    t = httpx.MockTransport(handler)
    return RadioDirectory(transport=t, retry_pause_s=0, laut=lf.LautFm(transport=t)), laut_calls


async def test_genre_picks_chart_stations_where_the_genre_leads_without_german_or_talk():
    d, _ = _directory(rb_up=False)
    got = await d.laut.by_tag("jazz")
    assert [s.name for s in got] == ["Jazzloft · laut.fm", "Late · laut.fm"]  # по чарту
    s = got[0]
    assert s.uuid == "" and s.url == "https://jazzloft.stream.laut.fm/jazzloft" and s.tags == "jazz,lounge"
    assert s.favicon.endswith("_120x120") and s.homepage == "https://laut.fm/jazzloft"
    assert await d.laut.by_tag("news") == []  # таких плиток laut.fm не даёт
    assert await d.laut.by_tag("pop") == [] and await d.laut.by_tag("pop", fallback=True) == []  # только запас
    await d.close()


def test_mix_every_third_without_repeating_what_radio_browser_already_has():
    main = [RadioStation(name=f"m{i}", url=f"http://m{i}/s") for i in range(5)]
    main.append(RadioStation(name="Jazzloft", url="https://jazzloft.stream.laut.fm/jazzloft"))
    extra = [RadioStation(name=f"{n} · laut.fm", url=f"https://{n}.stream.laut.fm/{n}") for n in ("jazzloft", "a", "b")]
    assert [s.name for s in lf.mix(main, extra)] == ["m0", "m1", "a · laut.fm", "m2", "m3", "b · laut.fm", "m4",
                                                     "Jazzloft"]
    assert lf.mix([], extra) == []


async def test_radio_browser_down_genres_and_random_play_from_laut_fm():
    """radio-browser не отвечает: плитки и случайный режим — из laut.fm; плитки, которых он не знает (новости), —
    «каталог недоступен», как раньше."""
    d, _ = _directory(rb_up=False)
    got = await d.by_tag("jazz")
    assert got and all(s.name.endswith(" · laut.fm") for s in got)
    assert await d.by_tag("news") is None
    assert [s.name for s in await d.by_name("jazz")] == ["Jazzloft · laut.fm"]  # категория name, без немецкого
    await d.close()


async def test_radio_browser_up_laut_fm_is_mixed_in_strong_genres_only():
    d, _ = _directory(rb=[*RB, RB_LAUT])
    got = await d.by_tag("jazz")
    names = [s.name for s in got]
    assert names[2] == "Late · laut.fm" and names.count("Jazzloft") == 1 and "Jazzloft · laut.fm" not in names
    assert [s.uuid for s in await d.by_tag("pop")] == [f"u{i}" for i in range(6)] + ["u-loft"]  # не подмешиваем
    # поиск по имени: radio-browser, за ним laut.fm — без станций, которые тот уже вернул
    assert [s.uuid for s in await d.by_name("jazz")] == [f"u{i}" for i in range(6)] + ["u-loft"]
    await d.close()
    d, _ = _directory()
    assert [s.name for s in await d.by_name("jazz")] == [f"RB {i}" for i in range(6)] + ["Jazzloft · laut.fm"]
    await d.close()


async def test_laut_fm_down_changes_nothing_and_is_not_asked_again_at_once():
    d, laut_calls = _directory(laut_down=True)
    assert [s.uuid for s in await d.by_tag("jazz")] == [f"u{i}" for i in range(6)]
    n = len(laut_calls)
    assert [s.uuid for s in await d.by_tag("blues")] == [f"u{i}" for i in range(6)]
    assert len(laut_calls) == n  # FAIL_S не спрашиваем
    await d.close()


async def test_genre_lists_are_cached():
    d, laut_calls = _directory()
    await d.by_tag("jazz")
    n = len(laut_calls)
    await d.by_tag("jazz")
    assert len(laut_calls) == n
    await d.close()

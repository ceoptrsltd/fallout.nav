"""laut.fm — второй каталог радио: открытый API без ключа (https://api.laut.fm), ~17 000 станций, свои серверы.

docs/contracts/radio.md. Зачем: radio-browser.info — единственный каталог, и когда он не отвечает, в машине не
переключить станцию ни клавишами, ни из админки. laut.fm подмешивается в жанры (каждая третья станция — отсюда, в
сильных у него и безъязыковых жанрах: джаз, блюз, классика, лаунж, чилаут, эмбиент, 70–90-е, диско, танцевальная…),
в случайный режим и в поиск по имени; не отвечает radio-browser — жанры и случайный режим играют отсюда.

Отбор: сервис немецкий — только станции, у которых жанр плитки стоит первым или вторым, из первой тысячи их чарта
(``position``), без немецкоязычных и разговорных жанров и названий. Название — «Jazzloft · laut.fm» (по нему видно,
откуда станция, и её находит поиск), uuid пустой: адреса потоков laut.fm постоянные, radio-browser о них не спрашиваем.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Any
from urllib.parse import quote

import httpx

from fnav.models import RadioStation

log = logging.getLogger("fnav.core.radio")

API = "https://api.laut.fm"
SUFFIX = " · laut.fm"
TIMEOUT_S = 6.0  # быстрее radio-browser (0,1–0,4 с): ждать дольше — задерживать жанр, который тот уже вернул
LIST_LIMIT = 120  # станций жанра за запрос (~300 КБ); его список идёт по активности, а не по чарту
CHART_MAX = 1000  # только первая тысяча их чарта
CACHE_S = 3 * 3600.0
FAIL_S = 60.0  # не ответил — столько не спрашиваем (жанры не ждут его тайм-аута каждый раз)
TAG_MAX = 40
SEARCH_LIMIT = 20

# тег плитки → жанры laut.fm; mix — подмешивать при живом radio-browser (сильные у laut.fm и без языка), иначе только
# запас. Нет здесь — новости, детское, шансон, хип-хоп (немецкий рэп): у laut.fm это по-немецки
TAGS: dict[str, tuple[tuple[str, ...], bool]] = {
    "jazz": (("Jazz", "Smooth Jazz", "Swing"), True),
    "blues": (("Blues", "Bluesrock"), True),
    "classical": (("Klassik",), True),
    "lounge": (("Lounge", "Easy Listening"), True),
    "relax": (("Chillout", "Chill"), True),
    "meditation": (("Ambient",), True),
    "sleep": (("Ambient", "Chillout"), True),
    "80s": (("80er",), True),
    "90s": (("90er",), True),
    "oldies": (("70er", "60er", "Oldies"), True),
    "dance": (("Dance", "Disco", "Eurodance"), True),
    "electronic": (("Electronic", "House", "Techno"), True),
    "lofi": (("Chill", "Instrumental"), False),
    "workout": (("EDM", "Hardstyle"), False),
    "party": (("Party-Hits", "Dance"), False),
    "romantic": (("Lovesongs", "Soul"), False),
    "pop": (("Pop",), False),
    "rock": (("Rock", "Classic Rock"), False),
    "hits": (("Charts", "Hits"), False),
    "folk": (("Folk",), False),
}
MIX_GENRES = sorted({g for gs, mix in TAGS.values() if mix for g in gs})
# немецкоязычное и речь — не берём
SKIP_GENRES = {g.lower() for g in (
    "Schlager", "Pop-Schlager", "Schlager-Oldies", "Deutsch", "Deutsch-Rap", "Deutscher Hip-Hop", "Deutsch-Pop",
    "Deutsch-Rock", "Volksmusik", "NDW", "Discofox", "Mallorca", "Karneval", "Liedermacher", "Kinderlieder",
    "Weihnachtslieder", "Mittelalter", "Hörspiel", "Talk", "News", "Podcast", "Comedy", "Aktuell", "Radio",
    "Querbeet", "Heimat", "Oberkrainer", "Party", "Gaming", "Live", "Liveshows", "Hip-Hop", "Rap", "Trap")}
# «german» — Germanyrock и т.п.; «simulator/sim…live» — радио игровых сообществ (живые немецкие ведущие)
SKIP_NAME = re.compile(r"deutsch|german|schlager|heimat|volks|bayer|sächs|saechs|schwäb|kinder|hörspiel|"
                       r"karneval|fasching|mallorca|ballermann|weihnacht|rentner|simulat|sim\s*live|truck",
                       re.IGNORECASE)
# в описании: радио для симуляторов (ETS2/ATS), «moderiert» — с ведущими (говорят по-немецки)
SKIP_TEXT = re.compile(r"\bETS ?2\b|\bATS\b|simulator|moderiert|moderation", re.IGNORECASE)


def _skipped(o: dict) -> bool:
    """Немецкоязычное, речь, радио игровых сообществ, выключенные станции."""
    if o.get("active") is False or any(str(g).lower() in SKIP_GENRES for g in o.get("genres") or []):
        return True
    return bool(SKIP_NAME.search(f"{o.get('name', '')} {o.get('display_name', '')}")
                or SKIP_TEXT.search(f"{o.get('format') or ''} {o.get('description') or ''}"))


def station_from(o: Any) -> RadioStation | None:
    """Станция laut.fm → RadioStation; без адреса потока — None."""
    if not isinstance(o, dict):
        return None
    url = str(o.get("stream_url") or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE) or len(url) > 2048:
        return None
    genres = [str(g) for g in (o.get("genres") or []) if g]
    images = o.get("images") if isinstance(o.get("images"), dict) else {}
    name = str(o.get("display_name") or o.get("name") or "laut.fm").strip()
    return RadioStation(uuid="", name=name + SUFFIX, url=url, homepage=str(o.get("page_url") or ""),
                        favicon=str(images.get("station_120x120") or images.get("station_80x80") or ""),
                        tags=",".join(g.lower() for g in genres), codec="MP3")


def _wanted(o: dict, genres: tuple[str, ...]) -> bool:
    gs = [str(g).lower() for g in (o.get("genres") or [])]
    if not gs or not any(g.lower() in gs[:2] for g in genres):
        return False
    pos = o.get("position")
    return isinstance(pos, int) and pos <= CHART_MAX and not _skipped(o)


def stream_id(url: str) -> str | None:
    """Имя станции laut.fm по адресу потока (radio-browser тоже знает часть их станций — не повторять)."""
    m = re.search(r"//(?:([\w.-]+)\.)?stream\.laut\.fm/([\w.-]+)", url, re.IGNORECASE)
    return m.group(2).lower() if m else None


class LautFm:
    """Клиент api.laut.fm с кэшем списков жанров. None из методов — laut.fm не ответил (или недавно не отвечал)."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, user_agent: str = "fallout.nav") -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S), transport=transport,
                                         headers={"User-Agent": user_agent}, follow_redirects=True)
        self._lists: dict[str, tuple[float, list[dict]]] = {}
        self._down_until = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str) -> Any:
        if time.monotonic() < self._down_until:
            return None
        try:
            r = await self._client.get(API + path)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.info("radio: laut.fm %s failed: %s", path.split("?")[0], e)
            self._down_until = time.monotonic() + FAIL_S
            return None

    async def _genre(self, genre: str) -> list[dict] | None:
        hit = self._lists.get(genre)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        d = await self._get(f"/stations/genre/{quote(genre, safe='')}?limit={LIST_LIMIT}")
        if not isinstance(d, list):
            return hit[1] if hit else None  # устаревший список лучше, чем никакого
        lst = [o for o in d if isinstance(o, dict)]
        self._lists[genre] = (time.monotonic(), lst)
        return lst

    async def by_tag(self, tag: str, fallback: bool = False) -> list[RadioStation] | None:
        """Станции плитки (по чарту laut.fm). [] — плитки здесь нет (или подмешивать её не надо: fallback=False)."""
        spec = TAGS.get(tag)
        if spec is None or not (spec[1] or fallback):
            return []
        genres = spec[0]
        lists = await asyncio.gather(*(self._genre(g) for g in genres))
        if all(x is None for x in lists):
            return None
        seen: set[str] = set()
        picked: list[tuple[int, RadioStation]] = []
        for lst in lists:
            for o in lst or []:
                if not _wanted(o, genres) or o.get("name") in seen:
                    continue
                st = station_from(o)
                if st is not None:
                    seen.add(o.get("name"))
                    picked.append((o["position"], st))
        picked.sort(key=lambda t: t[0])
        return [st for _, st in picked[:TAG_MAX]]

    async def random(self, n: int) -> list[RadioStation] | None:
        """n случайных станций сильных жанров (по 3 случайных жанра за раз — кэш наполняется понемногу)."""
        genres = random.sample(MIX_GENRES, min(3, len(MIX_GENRES)))
        lists = await asyncio.gather(*(self._genre(g) for g in genres))
        if all(x is None for x in lists):
            return None
        pool: dict[str, RadioStation] = {}
        for g, lst in zip(genres, lists, strict=True):
            for o in lst or []:
                if _wanted(o, (g,)) and o.get("name") not in pool:
                    st = station_from(o)
                    if st is not None:
                        pool[o["name"]] = st
        out = list(pool.values())
        random.shuffle(out)
        return out[:n]

    async def by_name(self, name: str) -> list[RadioStation] | None:
        """Поиск по имени станции (категория name их поиска), без немецкоязычного."""
        d = await self._get(f"/search/stations?query={quote(name, safe='')}&limit={SEARCH_LIMIT}")
        if not isinstance(d, dict):
            return None
        out: list[RadioStation] = []
        for cat in d.get("results") or []:
            cats = [c[0] for c in cat.get("categories") or [] if isinstance(c, list) and c]
            if "name" not in cats:
                continue
            for it in cat.get("items") or []:
                o = it.get("station") if isinstance(it, dict) else None
                if not isinstance(o, dict) or _skipped(o):
                    continue
                st = station_from(o)
                if st is not None:
                    out.append(st)
        return out


def mix(main: list[RadioStation], extra: list[RadioStation], every: int = 3) -> list[RadioStation]:
    """Каждая every-я станция — из extra (пока они есть), без станций laut.fm, уже пришедших из main."""
    have = {stream_id(s.url) for s in main} - {None}
    extra = [s for s in extra if stream_id(s.url) not in have]
    out: list[RadioStation] = []
    ei = 0
    for s in main:
        if (len(out) + 1) % every == 0 and ei < len(extra):
            out.append(extra[ei])
            ei += 1
        out.append(s)
    return out

"""HTTP-клиент к API Интерсвязи (api.is74.ru + cams.is74.ru).

Контракт эндпоинтов описан в CLAUDE.md (реверс-инжиниринг из HAR браузера).
"""
import re

import httpx

AUTH_BASE = "https://api.is74.ru"
CAMS_BASE = "https://cams.is74.ru"
CDN_BASE = "https://cdn.cams.is74.ru"

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify_name(name: str) -> str:
    """Ключ потока — только латиница/цифры/дефисы (безопасно для RTSP-путей)."""
    s = name.lower()
    s = "".join(_TRANSLIT.get(ch, ch) for ch in s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


async def send_push_code(phone: str, unique_device_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{AUTH_BASE}/mobile/auth/send-push-code",
            json={"phone": phone, "uniqueDeviceId": unique_device_id},
        )
        resp.raise_for_status()
        return resp.json()


async def confirm(phone: str, confirm_code: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{AUTH_BASE}/mobile/auth/confirm",
            json={"confirmCode": confirm_code, "phone": phone},
        )
        resp.raise_for_status()
        return resp.json()


async def get_token(auth_id: str, unique_device_id: str, user_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{AUTH_BASE}/mobile/auth/get-token",
            json={
                "authId": auth_id,
                "uniqueDeviceId": unique_device_id,
                "userId": user_id,
            },
        )
        resp.raise_for_status()
        return resp.json()


def _auth_headers(token: str) -> dict:
    # JSON API (cams.is74.ru, simple-address.is74.ru) авторизуется стандартным заголовком
    # Authorization: Bearer <token>. Это НЕ то же самое, что query-параметр
    # token=bearer-<token>, который встречается в готовых CDN-ссылках (snapshot/HLS/MSE) —
    # те приходят уже собранными в ответе limited-info, собирать их самим не нужно.
    return {"Authorization": f"Bearer {token}"}


async def limited_info(camera_ids: list[int], token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{CAMS_BASE}/api/limited-info",
            json={"CAMERA_IDS": camera_ids},
            headers=_auth_headers(token),
        )
        resp.raise_for_status()
        return resp.json()


async def get_group(token: str, group_id: str | None = None) -> list[dict]:
    """Папки/камеры личного кабинета — то же дерево, что в пикере "По имени" на old-stream.

    group_id=None -> корневой список групп аккаунта (GET .../get-group/?selfCams=true):
    [{"OBJECT":"GROUP","ID":"own","NAME":"Свои камеры"}, {"OBJECT":"GROUP","ID":<int>,"NAME":"..."}, ...]
    — "own" плюс любые группы, которые пользователь сам сохранил в старом личном кабинете.

    group_id="own" или числовой id -> список камер этой группы (или вложенных GROUP —
    в публичном дереве "Улицы Онлайн", id 16, это города, а не сразу камеры):
    [{"OBJECT":"CAMERA","ID":...,"NAME":"..."}, ...]

    В обоих случаях достаточно ID/NAME — если это камера, для остального (UUID, ссылки)
    идём в limited-info; отдельного эндпоинта "список моих камер" в API не было, но это —
    он и есть, просто под именем "группы".
    """
    async with httpx.AsyncClient() as client:
        if group_id is None:
            resp = await client.get(
                f"{CAMS_BASE}/api/get-group/",
                params={"selfCams": "true"},
                headers=_auth_headers(token),
            )
        else:
            resp = await client.get(
                f"{CAMS_BASE}/api/get-group/{group_id}",
                headers=_auth_headers(token),
            )
        resp.raise_for_status()
        return resp.json()


# Порядок важен: первое совпадение по ключевому слову в NAME (регистронезависимо) побеждает.
# Значения — id символов в спрайте app/static/icons.svg (<use href="...#id">), рендерятся
# напрямую в шаблонах без промежуточного маппинга.
_ICON_KEYWORDS = [
    ("домофон", "ic-door"),
    ("калитка", "ic-gate"),
    ("лифт", "ic-elevator"),
    ("парков", "ic-parking"),
    ("площадк", "ic-playground"),
]
_ICON_DEFAULT = "ic-camera"


def camera_icon(name: str) -> str:
    """id иконки в спрайте по ключевым словам в названии камеры (домофон/лифт/площадка/...)."""
    lowered = (name or "").lower()
    for keyword, icon in _ICON_KEYWORDS:
        if keyword in lowered:
            return icon
    return _ICON_DEFAULT


def build_stream_list(cameras: dict, base_url: str, order: list[int] | None = None) -> list[dict]:
    """Приводит ответ limited-info к плоскому списку для /streams и /api/streams.

    hls_url/snapshot_url указывают на собственные /media/* прокси-роуты этого сервиса
    (см. main.py), а не на cdn.cams.is74.ru напрямую — токен Интерсвязи наружу не уходит.
    base_url — например str(request.base_url), с завершающим "/".
    order — порядок вывода, обычно state["camera_ids"] (пользователь настраивает его
    перетаскиванием на /settings). Без order — сортировка по числовому ID, как раньше.
    """
    if order is not None:
        cam_ids = [str(cid) for cid in order if str(cid) in cameras]
    else:
        cam_ids = sorted(cameras.keys(), key=int)

    streams = []
    for cam_id in cam_ids:
        cam = cameras[cam_id]
        uuid = cam["UUID"]
        name = cam.get("NAME") or f"cam{cam_id}"
        streams.append({
            "id": cam_id,
            "uuid": uuid,
            "name": name,
            "icon": camera_icon(name),
            "address": cam.get("ADDRESS"),
            "slug": f"is74-{cam_id}-{slugify_name(name)}",
            "hls_url": f"{base_url}media/{cam_id}/multivariant.m3u8?uuid={uuid}",
            "snapshot_url": f"{base_url}media/{cam_id}/snapshot?uuid={uuid}",
        })
    return streams


def _browser_headers() -> dict:
    # cdn.cams.is74.ru в некоторых случаях смотрит на Origin/Referer (см. CLAUDE.md) —
    # эмулируем запрос из SPA old-stream.is74.ru.
    return {"Origin": "https://old-stream.is74.ru", "Referer": "https://old-stream.is74.ru/"}


async def fetch_cdn_text(path: str, params: dict, token: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{CDN_BASE}{path}",
            params={**params, "token": f"bearer-{token}"},
            headers=_browser_headers(),
        )
        resp.raise_for_status()
        return resp.text


async def fetch_cdn_bytes(path: str, params: dict, token: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{CDN_BASE}{path}",
            params={**params, "token": f"bearer-{token}"},
            headers=_browser_headers(),
        )
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "image/jpeg")


def rewrite_master_playlist(body: str, proxy_variant_url: str) -> str:
    """Заменяет строку с реальной ts.m3u8-ссылкой (с токеном) на наш прокси-URL.

    Формат ответа CDN — одна строка ссылки после каждого #EXT-X-STREAM-INF (см. CLAUDE.md).
    """
    out = []
    for line in body.splitlines():
        if line and not line.startswith("#"):
            out.append(proxy_variant_url)
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def rewrite_segment_urls(body: str) -> str:
    """Абсолютные пути сегментов (`/33/hls/media/...`) делает полными ссылками на CDN.

    Сами сегменты уже подписаны отдельной схемой (i=/ik=), не токеном аккаунта — их можно
    отдавать плееру/ffmpeg напрямую, прокси не нужен (см. CLAUDE.md, "Список камер").
    """
    out = []
    for line in body.splitlines():
        if line.startswith("/"):
            out.append(CDN_BASE + line)
        else:
            out.append(line)
    return "\n".join(out) + "\n"

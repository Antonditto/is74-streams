"""is74-streams: веб-сервис входа в Интерсвязь и выдачи ссылок на потоки камер.

go2rtc в этот сервис не входит — он отдельный add-on и должен сам забирать
актуальные ссылки из GET /api/streams.
"""
import asyncio
import base64
import json
import os
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import is74_client, storage

_APP_DIR = Path(__file__).parent


class ProxyAwareMiddleware:
    """Учитывает заголовки reverse-proxy (Traefik) и HA Ingress до роутинга, чтобы
    request.base_url/root_path ниже по стеку уже отражали реальный публичный адрес,
    а не внутренний host:port контейнера. Без этих заголовков (прямой доступ по
    опубликованному порту) поведение не меняется.

    Доверяем заголовкам безусловно, без allow-list доверенных прокси — сервис живёт
    за собственным Traefik в приватном homelab, не публичный multi-tenant edge
    (см. CLAUDE.md).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])

        # HA Supervisor Ingress: срезает этот префикс перед тем, как запрос долетает
        # до аддона, но ссылки, которые мы генерируем сами, должны его учитывать.
        ingress_path = headers.get(b"x-ingress-path")
        if ingress_path:
            scope["root_path"] = ingress_path.decode("latin-1")

        # Traefik сам подставляет эти заголовки на обычном роутинге, без доп. конфигурации.
        proto = headers.get(b"x-forwarded-proto")
        if proto:
            scope["scheme"] = proto.decode("latin-1").split(",")[0].strip()

        fwd_host = headers.get(b"x-forwarded-host")
        if fwd_host:
            host_val = fwd_host.split(b",")[0].strip()
            scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"host"] + [(b"host", host_val)]

        await self.app(scope, receive, send)


app = FastAPI(title="is74-streams")
app.add_middleware(ProxyAwareMiddleware)
app.mount("/static", StaticFiles(directory=str(_APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(_APP_DIR / "templates"))


def _ctx(request: Request, **extra) -> dict:
    """Контекст для TemplateResponse: base_path — префикс для root-relative ссылок
    в шаблонах (см. ProxyAwareMiddleware, обычно "" вне Ingress)."""
    return {"base_path": request.scope.get("root_path", ""), **extra}


def _redirect(request: Request, path: str, status_code: int = 307) -> RedirectResponse:
    """RedirectResponse с учётом root_path — иначе под HA Ingress Location уходит мимо
    префикса /api/hassio_ingress/<token>/ и браузер попадает не в аддон, а куда-то в HA
    (см. ProxyAwareMiddleware)."""
    base_path = request.scope.get("root_path", "")
    return RedirectResponse(f"{base_path}{path}", status_code=status_code)


def _internal_base_url(request: Request) -> str:
    """base_url для /api/streams (go2rtc/Frigate — на том же хосте, что и этот сервис).

    Если задана IS74_INTERNAL_BASE_URL — используем её вместо публичного base_url
    (важно для HA add-on: наружные ссылки могут идти через Ingress-префикс, а
    go2rtc/Frigate стоят рядом и он им не нужен). Без переменной — тот же публичный
    base_url, что и раньше.
    """
    override = os.environ.get("IS74_INTERNAL_BASE_URL", "").strip()
    if override:
        return override if override.endswith("/") else override + "/"
    return str(request.base_url)


def _describe_error(exc: Exception) -> str:
    """API отдаёт ошибки валидации как [{"field","message"}, ...] с кодом 422."""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            errors = exc.response.json()
            if isinstance(errors, list):
                return "; ".join(e.get("message", str(e)) for e in errors)
        except ValueError:
            pass
    return str(exc)

# Состояние незавершённого логина (send-push-code -> confirm). Сервис
# однопользовательский, поэтому простого процесса-глобального словаря достаточно.
_pending_login: dict = {}


@app.get("/")
async def index(request: Request):
    state = storage.load_state()
    if not state.get("token"):
        return _redirect(request, "/login")
    if not state.get("camera_ids"):
        return _redirect(request, "/settings")
    return _redirect(request, "/streams")


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", _ctx(request, error=None))


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, phone: str = Form(...)):
    state = storage.load_state()
    unique_device_id = state.get("unique_device_id") or uuid.uuid4().hex

    try:
        await is74_client.send_push_code(phone, unique_device_id)
    except Exception as exc:
        return templates.TemplateResponse(
            request, "login.html", _ctx(request, error=f"Не удалось отправить код: {_describe_error(exc)}")
        )

    _pending_login["phone"] = phone
    _pending_login["unique_device_id"] = unique_device_id
    return _redirect(request, "/confirm", status_code=303)


@app.get("/confirm", response_class=HTMLResponse)
async def confirm_form(request: Request):
    if "phone" not in _pending_login:
        return _redirect(request, "/login")
    return templates.TemplateResponse(
        request, "confirm.html", _ctx(request, phone=_pending_login["phone"], error=None)
    )


@app.post("/confirm", response_class=HTMLResponse)
async def confirm_submit(request: Request, code: str = Form(...)):
    phone = _pending_login.get("phone")
    unique_device_id = _pending_login.get("unique_device_id")
    if not phone or not unique_device_id:
        return _redirect(request, "/login")

    try:
        confirm_resp = await is74_client.confirm(phone, code)
        # authId приходит уже в ответе send-push-code и повторяется в ответе confirm
        # (это один и тот же authId). userId — не плоское поле, а addresses[0].USER_ID:
        # {"authId","message","addresses":[{"USER_ID":"...","ADDRESS":"..."}]}.
        # Если у номера несколько адресов/лицевых счетов — берём первый.
        auth_id = confirm_resp.get("authId")
        addresses = confirm_resp.get("addresses") or []
        if not auth_id or not addresses:
            raise ValueError(f"не нашли authId/addresses в ответе confirm: {confirm_resp}")
        user_id = addresses[0]["USER_ID"]

        token_resp = await is74_client.get_token(auth_id, unique_device_id, str(user_id))
    except Exception as exc:
        return templates.TemplateResponse(
            request, "confirm.html", _ctx(request, phone=phone, error=f"Ошибка подтверждения: {_describe_error(exc)}")
        )

    state = storage.load_state()
    state.update({
        "token": token_resp["TOKEN"],
        "access_end": token_resp.get("ACCESS_END"),
        "phone": phone,
        "unique_device_id": unique_device_id,
        "address": addresses[0].get("ADDRESS"),
    })
    storage.save_state(state)
    _pending_login.clear()
    return _redirect(request, "/settings", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_form(request: Request):
    state = storage.load_state()
    if not state.get("token"):
        return _redirect(request, "/login")
    camera_ids = state.get("camera_ids", [])

    error = None
    cameras: list[dict] = []
    if camera_ids:
        try:
            cameras = await _fetch_streams(str(request.base_url))
        except Exception as exc:
            # Не блокируем страницу настроек — ручное редактирование списка всё ещё должно
            # работать, даже если превью подтянуть не удалось (сеть недоступна и т.п.).
            error = f"Не удалось загрузить превью камер: {_describe_error(exc)}"

    cameras_json = json.dumps(
        [{"id": c["id"], "name": c["name"], "icon": c["icon"], "snapshot_url": c["snapshot_url"]} for c in cameras],
        ensure_ascii=False,
    )
    return templates.TemplateResponse(
        request,
        "settings.html",
        _ctx(
            request,
            camera_ids=", ".join(str(c) for c in camera_ids),
            cameras_json=cameras_json,
            error=error,
        ),
    )


@app.post("/settings", response_class=HTMLResponse)
async def settings_submit(request: Request, camera_ids: str = Form(""), logout: str = Form(None)):
    if logout:
        storage.clear_state()
        return _redirect(request, "/login", status_code=303)

    state = storage.load_state()
    if not state.get("token"):
        return _redirect(request, "/login")

    try:
        # принимаем и "71069, 71070", и вставленный как есть из localStorage массив "[71069,71070]"
        cleaned = camera_ids.strip().strip("[]")
        parsed_ids = [int(x.strip()) for x in cleaned.split(",") if x.strip()]
    except ValueError:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _ctx(request, camera_ids=camera_ids, cameras_json="[]", error="ID камер — числа через запятую"),
        )

    state["camera_ids"] = parsed_ids
    storage.save_state(state)
    return _redirect(request, "/streams", status_code=303)


@app.get("/api/my-groups")
async def my_groups_api():
    """Корневые группы аккаунта: "Свои камеры" (own) + группы, привязанные к аккаунту Интерсвязью."""
    state = storage.load_state()
    token = state.get("token")
    if not token:
        return JSONResponse({"error": "нет токена"}, status_code=401)
    try:
        result = await is74_client.get_group(token)
    except Exception as exc:
        return JSONResponse({"error": _describe_error(exc)}, status_code=502)
    return result


@app.get("/api/group-cameras")
async def group_cameras_api(group_id: str):
    state = storage.load_state()
    token = state.get("token")
    if not token:
        return JSONResponse({"error": "нет токена"}, status_code=401)
    try:
        result = await is74_client.get_group(token, group_id=group_id)
    except Exception as exc:
        return JSONResponse({"error": _describe_error(exc)}, status_code=502)
    return result


async def _fetch_snapshot_with_retry(uuid: str, token: str, attempts: int = 3) -> tuple[bytes, str]:
    """До 3 попыток с небольшой паузой — сами снэпшоты иногда отваливаются транзиентно
    (камера в спящем режиме, разовый таймаут CDN), не значит, что превью нет вообще."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return await is74_client.fetch_cdn_bytes("/snapshot", {"uuid": uuid}, token)
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                await asyncio.sleep(0.4 * (attempt + 1))
    raise last_exc


@app.get("/api/camera-preview")
async def camera_preview_api(ids: str, request: Request):
    """Превью (имя + снэпшот) для камер, которых ещё нет в state.camera_ids.

    Нужен отдельно от /media/*/snapshot — та отдаёт снэпшот только для уже сохранённых
    камер (защита от превращения прокси в открытый релей токена, см. _require_known_camera).
    Пока камера не сохранена, отдаём снэпшот как data:-URI прямо в JSON — без него на
    /settings до нажатия "Сохранить" превью нечем показать, только заглушку. Каждый снэпшот
    качается с повтором (см. _fetch_snapshot_with_retry) — если он всё равно не подтянулся,
    фронтенд показывает кнопку "повторить" прямо на карточке (см. settings.html).
    """
    state = storage.load_state()
    token = state.get("token")
    if not token:
        return JSONResponse({"error": "нет токена"}, status_code=401)
    try:
        parsed_ids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        return JSONResponse({"error": "ids — числа через запятую"}, status_code=400)
    if not parsed_ids:
        return []

    try:
        cameras = await is74_client.limited_info(parsed_ids, token)
        streams = is74_client.build_stream_list(cameras, str(request.base_url), order=parsed_ids)
        snapshots = await asyncio.gather(
            *(_fetch_snapshot_with_retry(s["uuid"], token) for s in streams),
            return_exceptions=True,
        )
    except Exception as exc:
        return JSONResponse({"error": _describe_error(exc)}, status_code=502)

    result = []
    for s, snap in zip(streams, snapshots):
        data_uri = None
        if not isinstance(snap, Exception):
            content, content_type = snap
            data_uri = f"data:{content_type};base64,{base64.b64encode(content).decode()}"
        result.append({"id": s["id"], "name": s["name"], "icon": s["icon"], "snapshot_data_uri": data_uri})
    return result


async def _fetch_streams(base_url: str) -> list[dict]:
    state = storage.load_state()
    token = state.get("token")
    camera_ids = state.get("camera_ids") or []
    if not token or not camera_ids:
        return []
    cameras = await is74_client.limited_info(camera_ids, token)
    return is74_client.build_stream_list(cameras, base_url, order=camera_ids)


@app.get("/streams", response_class=HTMLResponse)
async def streams_page(request: Request):
    state = storage.load_state()
    if not state.get("token"):
        return _redirect(request, "/login")
    if not state.get("camera_ids"):
        return _redirect(request, "/settings")

    try:
        streams = await _fetch_streams(str(request.base_url))
        error = None
    except Exception as exc:
        streams, error = [], str(exc)

    return templates.TemplateResponse(
        request, "streams.html", _ctx(request, streams=streams, error=error)
    )


@app.get("/api/streams")
async def streams_api(request: Request):
    try:
        streams = await _fetch_streams(_internal_base_url(request))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return streams


def _require_known_camera(camera_id: int) -> str | None:
    """Токен, если camera_id есть в сохранённом списке — иначе None (403 в вызывающем коде).

    Страховка от того, чтобы прокси-роуты не превращались в открытый релей токена аккаунта
    на произвольные UUID (см. обсуждение в CLAUDE.md).
    """
    state = storage.load_state()
    token = state.get("token")
    if not token or camera_id not in (state.get("camera_ids") or []):
        return None
    return token


@app.get("/media/{camera_id}/multivariant.m3u8")
async def media_master(camera_id: int, uuid: str, request: Request):
    token = _require_known_camera(camera_id)
    if not token:
        return PlainTextResponse("нет доступа", status_code=403)
    try:
        body = await is74_client.fetch_cdn_text(
            "/hls/playlists/multivariant.m3u8", {"uuid": uuid}, token
        )
    except Exception as exc:
        return PlainTextResponse(_describe_error(exc), status_code=502)
    variant_url = f"{request.base_url}media/{camera_id}/ts.m3u8?uuid={uuid}&quality=main"
    return PlainTextResponse(
        is74_client.rewrite_master_playlist(body, variant_url),
        media_type="application/vnd.apple.mpegurl",
    )


@app.get("/media/{camera_id}/ts.m3u8")
async def media_variant(camera_id: int, uuid: str, quality: str = "main"):
    token = _require_known_camera(camera_id)
    if not token:
        return PlainTextResponse("нет доступа", status_code=403)
    if quality not in ("main", "sub"):
        return PlainTextResponse("неверный quality", status_code=400)
    try:
        body = await is74_client.fetch_cdn_text(
            "/hls/playlists/ts.m3u8", {"uuid": uuid, "quality": quality}, token
        )
    except Exception as exc:
        return PlainTextResponse(_describe_error(exc), status_code=502)
    return PlainTextResponse(
        is74_client.rewrite_segment_urls(body), media_type="application/vnd.apple.mpegurl"
    )


@app.get("/media/{camera_id}/snapshot")
async def media_snapshot(camera_id: int, uuid: str, lossy: int = 0):
    token = _require_known_camera(camera_id)
    if not token:
        return PlainTextResponse("нет доступа", status_code=403)
    params = {"uuid": uuid, "lossy": 1} if lossy else {"uuid": uuid}
    try:
        content, content_type = await is74_client.fetch_cdn_bytes("/snapshot", params, token)
    except Exception as exc:
        return PlainTextResponse(_describe_error(exc), status_code=502)
    return Response(content=content, media_type=content_type)

"""Встроенный обратный прокси для авто-входа на ресурсы.

Поддерживаемые типы авторизации ресурса (auth_type):
  - basic : подставляется заголовок Authorization: Basic ... (надёжно)
  - form  : best-effort воспроизведение формы логина и проксирование сессии
  - link  : без авто-входа (ресурс открывается прямой ссылкой, не через прокси)

Тело ответа читается целиком (не потоково) — этого достаточно для веб-интерфейсов
админ-сервисов и упрощает переписывание ссылок. Для крупных загрузок прокси не
предназначен.
"""
import base64
from urllib.parse import urljoin, urlsplit

import httpx
from starlette.responses import Response

from . import database

# Заголовки, которые нельзя пробрасывать (hop-by-hop) либо которые перепишет httpx.
_DROP_REQUEST = {
    "host", "connection", "keep-alive", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade", "content-length", "accept-encoding",
}
_DROP_RESPONSE = {
    "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
    "content-encoding", "content-length",
}

# Простое хранилище сессионных cookie для form-логина (на время жизни процесса).
_form_cookies: dict[int, httpx.Cookies] = {}


def _base(resource) -> str:
    url = resource["url"]
    return url if url.endswith("/") else url + "/"


async def _ensure_form_login(resource, client: httpx.AsyncClient) -> None:
    """Выполняет вход по форме, если для ресурса ещё нет сессии. Best-effort."""
    rid = resource["id"]
    if rid in _form_cookies:
        client.cookies = _form_cookies[rid]
        return
    username, secret = database.resource_credentials(resource)
    login_url = resource["login_url"] or _base(resource)
    ufield = resource["username_field"] or "username"
    pfield = resource["password_field"] or "password"
    try:
        await client.post(login_url, data={ufield: username, pfield: secret})
        _form_cookies[rid] = client.cookies
    except Exception:
        # Логин не удался — продолжаем без cookie (страница покажет форму входа).
        pass


def _rewrite_location(location: str, resource, prefix: str) -> str:
    """Переписывает абсолютные редиректы на наш прокси-префикс."""
    base = _base(resource)
    bp = urlsplit(base)
    lp = urlsplit(location)
    if lp.scheme and lp.netloc:
        if (lp.scheme, lp.netloc) == (bp.scheme, bp.netloc):
            sub = lp.path
            if sub.startswith(bp.path):
                sub = sub[len(bp.path):]
            return f"{prefix}/{sub.lstrip('/')}" + (f"?{lp.query}" if lp.query else "")
        return location  # внешний редирект оставляем как есть
    return location


async def proxy_request(request, resource, path: str) -> Response:
    auth_type = resource["auth_type"]
    target = urljoin(_base(resource), path.lstrip("/"))
    if request.url.query:
        target = f"{target}?{request.url.query}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST}

    if auth_type == "basic":
        username, secret = database.resource_credentials(resource)
        token = base64.b64encode(f"{username}:{secret}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"

    verify = bool(resource["verify_tls"])
    body = await request.body()

    async with httpx.AsyncClient(verify=verify, follow_redirects=False, timeout=30) as client:
        if auth_type == "form":
            await _ensure_form_login(resource, client)
        try:
            upstream = await client.request(
                request.method, target, headers=headers, content=body or None
            )
        except Exception as exc:
            return Response(
                f"Не удалось обратиться к ресурсу: {type(exc).__name__}: {exc}",
                status_code=502, media_type="text/plain; charset=utf-8",
            )

    prefix = f"/go/{resource['id']}"
    out_headers = {}
    for k, v in upstream.headers.items():
        kl = k.lower()
        if kl in _DROP_RESPONSE:
            continue
        if kl == "location":
            v = _rewrite_location(v, resource, prefix)
        out_headers[k] = v

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=out_headers,
    )

"""Точка входа FastAPI: маршруты UI, аутентификация, прокси, планировщик."""
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, PlainTextResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import config, database, security, scheduler, jobs, tracker, proxy

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Update Tracker", docs_url=None, redoc_url=None, openapi_url=None)


# ----------------------------------------------------------- middleware -----
class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        public = path == "/login" or path == "/healthz" or path.startswith("/static")
        if not public and not request.session.get("uid"):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)


app.add_middleware(AuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.effective_secret_key(),
    session_cookie="ut_session",
    max_age=config.SESSION_MAX_AGE,
    same_site="lax",
    https_only=os.environ.get("SESSION_COOKIE_SECURE", "true").lower() != "false",
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --------------------------------------------------------------- helpers ----
def ensure_csrf(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


def check_csrf(request: Request, form) -> bool:
    return bool(form.get("csrf")) and form.get("csrf") == request.session.get("csrf")


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flashes", []).append({"msg": message, "kind": kind})


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    flashes = request.session.pop("flashes", [])
    user = database.get_user(request.session["uid"]) if request.session.get("uid") else None
    base_ctx = {
        "request": request,
        "csrf": ensure_csrf(request),
        "flashes": flashes,
        "user": user,
        "path": request.url.path,
        "next_broadcast": scheduler.next_broadcast(),
    }
    base_ctx.update(ctx)
    return templates.TemplateResponse(name, base_ctx)


def client_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", request.client.host if request.client else "")


# --------------------------------------------------------------- startup ----
@app.on_event("startup")
def on_startup():
    database.init_db()
    # Первичное создание администратора.
    if database.count_users() == 0:
        pwd = config.ADMIN_PASSWORD or secrets.token_urlsafe(12)
        database.create_user(config.ADMIN_LOGIN, security.hash_password(pwd))
        if not config.ADMIN_PASSWORD:
            print(f"[setup] Создан администратор '{config.ADMIN_LOGIN}' с паролем: {pwd}")
    # Сид-настройки на первый запуск.
    defaults = {
        "translator_provider": "google",
        "timezone": config.TIMEZONE,
        "schedule_day": config.DEFAULT_SCHEDULE_DAY,
        "schedule_hour": str(config.DEFAULT_SCHEDULE_HOUR),
        "schedule_minute": str(config.DEFAULT_SCHEDULE_MINUTE),
        "poll_interval": str(config.DEFAULT_POLL_INTERVAL),
    }
    for k, v in defaults.items():
        if not database.get_setting(k, ""):
            database.set_setting(k, v)
    for k, seed in {
        "telegram_token": config.SEED_TELEGRAM_TOKEN,
        "telegram_chat_id": config.SEED_TELEGRAM_CHAT_ID,
        "deepl_api_key": config.SEED_DEEPL_API_KEY,
        "github_token": config.SEED_GITHUB_TOKEN,
    }.items():
        if seed and not database.get_setting(k, ""):
            database.set_setting(k, seed)
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown()


@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")


# ----------------------------------------------------------------- auth -----
@app.get("/login")
def login_page(request: Request):
    if request.session.get("uid"):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    ip = client_ip(request)
    if security.is_locked(ip):
        flash(request, "Слишком много попыток. Подождите несколько минут.", "error")
        return RedirectResponse("/login", status_code=303)

    login = (form.get("login") or "").strip()
    password = form.get("password") or ""
    user = database.get_user_by_login(login)
    if user and security.verify_password(user["password_hash"], password):
        security.reset_attempts(ip)
        request.session["uid"] = user["id"]
        database.log_action("login", login, ip)
        return RedirectResponse("/", status_code=303)

    security.register_failure(ip)
    database.log_action("login_failed", login, ip)
    flash(request, "Неверный логин или пароль.", "error")
    return RedirectResponse("/login", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------ dashboard -----
@app.get("/")
def dashboard(request: Request):
    resources = database.list_resources()
    projects = database.list_projects()
    updated = sum(1 for e in database.list_unnotified_events())
    return render(request, "dashboard.html", resources=resources,
                  projects=projects, pending_updates=updated)


# ------------------------------------------------------------- resources ----
@app.get("/resources")
def resources_page(request: Request):
    return render(request, "resources.html", resources=database.list_resources())


@app.post("/resources")
async def resources_add(request: Request):
    form = await request.form()
    if not check_csrf(request, form):
        flash(request, "Сессия истекла, повторите.", "error")
        return RedirectResponse("/resources", status_code=303)
    if not (form.get("name") and form.get("url")):
        flash(request, "Укажите название и адрес ресурса.", "error")
        return RedirectResponse("/resources", status_code=303)
    database.create_resource({
        "name": form.get("name"), "description": form.get("description"),
        "url": form.get("url"), "icon": form.get("icon"),
        "auth_type": form.get("auth_type", "link"),
        "username": form.get("username"), "secret": form.get("secret"),
        "login_url": form.get("login_url"),
        "username_field": form.get("username_field"),
        "password_field": form.get("password_field"),
        "verify_tls": form.get("verify_tls") == "on",
        "sort_order": form.get("sort_order") or 0,
    })
    database.log_action("resource_add", form.get("name"), client_ip(request))
    flash(request, "Ресурс добавлен.")
    return RedirectResponse("/resources", status_code=303)


@app.post("/resources/{rid}/update")
async def resources_update(request: Request, rid: int):
    form = await request.form()
    if not check_csrf(request, form):
        return RedirectResponse("/resources", status_code=303)
    database.update_resource(rid, {
        "name": form.get("name"), "description": form.get("description"),
        "url": form.get("url"), "icon": form.get("icon"),
        "auth_type": form.get("auth_type", "link"),
        "username": form.get("username"), "secret": form.get("secret"),
        "login_url": form.get("login_url"),
        "username_field": form.get("username_field"),
        "password_field": form.get("password_field"),
        "verify_tls": form.get("verify_tls") == "on",
        "sort_order": form.get("sort_order") or 0,
    })
    flash(request, "Ресурс обновлён.")
    return RedirectResponse("/resources", status_code=303)


@app.post("/resources/{rid}/delete")
async def resources_delete(request: Request, rid: int):
    form = await request.form()
    if check_csrf(request, form):
        database.delete_resource(rid)
        flash(request, "Ресурс удалён.")
    return RedirectResponse("/resources", status_code=303)


# -------------------------------------------------------------- projects ----
@app.get("/projects")
def projects_page(request: Request):
    return render(request, "projects.html", projects=database.list_projects())


@app.post("/projects")
async def projects_add(request: Request):
    form = await request.form()
    if not check_csrf(request, form):
        return RedirectResponse("/projects", status_code=303)
    image_ref = (form.get("image_ref") or "").strip()
    if not image_ref:
        flash(request, "Укажите ссылку на образ.", "error")
        return RedirectResponse("/projects", status_code=303)
    parsed = tracker.parse_image_ref(image_ref)
    pid = database.create_project({
        "name": form.get("name") or parsed["repository"],
        "description": form.get("description"),
        "image_ref": parsed["normalized"],
        "registry": parsed["registry"], "namespace": parsed["namespace"],
        "repository": parsed["repository"], "tag": parsed["tag"],
        "track_strategy": "latest_digest",
        "enabled": form.get("enabled", "on") == "on",
        "repo_url": form.get("repo_url"),
    })
    # Базовый замер сразу после добавления (ошибки игнорируем).
    try:
        jobs.poll_project(database.get_project(pid))
    except Exception:
        pass
    database.log_action("project_add", parsed["normalized"], client_ip(request))
    flash(request, f"Проект добавлен: {parsed['normalized']}")
    return RedirectResponse("/projects", status_code=303)


@app.post("/projects/{pid}/update")
async def projects_update(request: Request, pid: int):
    form = await request.form()
    if not check_csrf(request, form):
        return RedirectResponse("/projects", status_code=303)
    image_ref = (form.get("image_ref") or "").strip()
    parsed = tracker.parse_image_ref(image_ref)
    database.update_project(pid, {
        "name": form.get("name") or parsed["repository"],
        "description": form.get("description"),
        "image_ref": parsed["normalized"],
        "registry": parsed["registry"], "namespace": parsed["namespace"],
        "repository": parsed["repository"], "tag": parsed["tag"],
        "track_strategy": "latest_digest",
        "enabled": form.get("enabled") == "on",
        "repo_url": form.get("repo_url"),
    })
    flash(request, "Проект обновлён.")
    return RedirectResponse("/projects", status_code=303)


@app.post("/projects/{pid}/delete")
async def projects_delete(request: Request, pid: int):
    form = await request.form()
    if check_csrf(request, form):
        database.delete_project(pid)
        flash(request, "Проект удалён.")
    return RedirectResponse("/projects", status_code=303)


@app.post("/projects/{pid}/check")
async def projects_check(request: Request, pid: int):
    form = await request.form()
    if not check_csrf(request, form):
        return RedirectResponse("/projects", status_code=303)
    project = database.get_project(pid)
    if project:
        res = jobs.poll_project(project)
        flash(request, f"{project['name']}: {res['status']} — {res.get('detail','')}")
    return RedirectResponse("/projects", status_code=303)


# --------------------------------------------------------------- settings ---
@app.get("/settings")
def settings_page(request: Request):
    s = database.all_settings()
    return render(request, "settings.html", s=s,
                  has_telegram=bool(database.get_setting("telegram_token")),
                  has_deepl=bool(database.get_setting("deepl_api_key")),
                  has_libre=bool(database.get_setting("libretranslate_api_key")),
                  has_github=bool(database.get_setting("github_token")))


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    if not check_csrf(request, form):
        return RedirectResponse("/settings", status_code=303)
    # Секреты обновляем только если поле заполнено (иначе сохраняем прежнее).
    for key in ("telegram_token", "deepl_api_key", "github_token", "libretranslate_api_key"):
        if form.get(key):
            database.set_setting(key, form.get(key))
    database.set_setting("telegram_chat_id", form.get("telegram_chat_id") or "")
    database.set_setting("translator_provider", form.get("translator_provider") or "google")
    database.set_setting("libretranslate_url", form.get("libretranslate_url") or "")
    database.set_setting("timezone", form.get("timezone") or config.TIMEZONE)
    database.set_setting("schedule_day", form.get("schedule_day") or "sun")
    database.set_setting("schedule_hour", form.get("schedule_hour") or "12")
    database.set_setting("schedule_minute", form.get("schedule_minute") or "0")
    database.set_setting("poll_interval", form.get("poll_interval") or "60")
    scheduler.reschedule()
    database.log_action("settings_save", "", client_ip(request))
    flash(request, "Настройки сохранены.")
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/detect-chatid")
async def settings_detect_chatid(request: Request):
    from . import notifier
    form = await request.form()
    if not check_csrf(request, form):
        return RedirectResponse("/settings", status_code=303)
    token = form.get("telegram_token") or database.get_setting("telegram_token", "")
    chat_id, detail = notifier.detect_chat_id(token)
    if chat_id:
        database.set_setting("telegram_chat_id", chat_id)
        flash(request, f"chat_id определён: {chat_id} ({detail})")
    else:
        flash(request, f"Не удалось определить chat_id: {detail}", "error")
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/test-telegram")
async def settings_test_telegram(request: Request):
    from . import notifier
    form = await request.form()
    if not check_csrf(request, form):
        return RedirectResponse("/settings", status_code=303)
    token = database.get_setting("telegram_token", "")
    chat_id = database.get_setting("telegram_chat_id", "")
    ok, detail = notifier.send_message(
        token, chat_id, "🛰 Тестовое сообщение от Update Tracker. Связь есть!"
    )
    flash(request, f"Telegram: {detail}", "ok" if ok else "error")
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/change-password")
async def settings_change_password(request: Request):
    form = await request.form()
    if not check_csrf(request, form):
        return RedirectResponse("/settings", status_code=303)
    user = database.get_user(request.session["uid"])
    current = form.get("current_password") or ""
    new = form.get("new_password") or ""
    if not security.verify_password(user["password_hash"], current):
        flash(request, "Текущий пароль неверен.", "error")
    elif len(new) < 8:
        flash(request, "Новый пароль должен быть не короче 8 символов.", "error")
    else:
        database.update_password(user["id"], security.hash_password(new))
        database.log_action("password_change", user["login"], client_ip(request))
        flash(request, "Пароль изменён.")
    return RedirectResponse("/settings", status_code=303)


# --------------------------------------------------------------- history ----
@app.get("/history")
def history_page(request: Request):
    return render(request, "history.html",
                  events=database.recent_events(100),
                  audit=database.recent_audit(50))


@app.post("/run-now")
async def run_now(request: Request):
    form = await request.form()
    if not check_csrf(request, form):
        return RedirectResponse("/", status_code=303)
    result = jobs.run_broadcast(force=True)
    if result["sent"]:
        flash(request, f"Дайджест отправлен. Событий: {result.get('events', 0)}.")
    else:
        flash(request, f"Не отправлено: {result.get('detail') or result.get('reason')}", "error")
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


# ----------------------------------------------------------------- proxy ----
@app.api_route("/go/{rid}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
@app.api_route("/go/{rid}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
async def go_proxy(request: Request, rid: int, path: str = ""):
    resource = database.get_resource(rid)
    if resource is None:
        return PlainTextResponse("Ресурс не найден", status_code=404)
    if resource["auth_type"] == "link":
        # Прямая ссылка — без проксирования.
        return RedirectResponse(resource["url"], status_code=302)
    return await proxy.proxy_request(request, resource, path)

"""Уведомления в Telegram (личный чат) и сборка текста дайджеста."""
import html
import datetime as dt
import httpx

from . import database

TG_API = "https://api.telegram.org"
TG_LIMIT = 4096


def _api(token: str, method: str) -> str:
    return f"{TG_API}/bot{token}/{method}"


def _split(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Бьёт длинный текст на части по границам строк, не превышая лимит Telegram."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                chunks.append(current)
            # Очень длинная одиночная строка — режем жёстко.
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def send_message(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    """Отправляет сообщение (с авторазбиением). Возвращает (ok, detail)."""
    if not token or not chat_id:
        return False, "Не заданы токен бота или chat_id"
    try:
        with httpx.Client() as client:
            for part in _split(text):
                r = client.post(
                    _api(token, "sendMessage"),
                    data={
                        "chat_id": chat_id,
                        "text": part,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": "true",
                    },
                    timeout=30,
                )
                if r.status_code != 200:
                    return False, f"HTTP {r.status_code}: {r.text[:200]}"
        return True, "Отправлено"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def detect_chat_id(token: str) -> tuple[str | None, str]:
    """Определяет chat_id последнего написавшего боту (через getUpdates)."""
    if not token:
        return None, "Не задан токен бота"
    try:
        with httpx.Client() as client:
            r = client.get(_api(token, "getUpdates"), timeout=20)
            r.raise_for_status()
            updates = r.json().get("result", [])
        for upd in reversed(updates):
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") or {}
            if chat.get("id") is not None:
                return str(chat["id"]), f"Найден чат с {chat.get('first_name', '')}".strip()
        return None, "Сначала напишите боту /start, затем повторите"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def _date_ru() -> str:
    return dt.datetime.now().strftime("%d.%m.%Y")


def build_digest(events: list, project_names: dict, checked_count: int) -> str:
    """Собирает HTML-сообщение для Telegram по списку событий обновлений."""
    if not events:
        return (
            f"🛰 <b>Обновления за неделю</b> · {_date_ru()}\n\n"
            f"Новых обновлений не обнаружено.\n"
            f"Проверено проектов: {checked_count}."
        )

    lines = [f"🛰 <b>Обновления за неделю</b> · {_date_ru()}", ""]
    for e in events:
        name = _esc(project_names.get(e["project_id"], "проект"))
        old_v = e["old_version"] or (e["old_digest"] or "")[:19]
        new_v = e["new_version"] or (e["new_digest"] or "")[:19]
        lines.append(f"📦 <b>{name}</b>")
        if old_v and new_v:
            lines.append(f"   <code>{_esc(old_v)} → {_esc(new_v)}</code>")
        elif new_v:
            lines.append(f"   новая версия: <code>{_esc(new_v)}</code>")
        notes = e["notes_ru"] or e["notes_original"]
        if notes:
            trimmed = notes.strip()
            if len(trimmed) > 700:
                trimmed = trimmed[:700].rstrip() + "…"
            lines.append(_esc(trimmed))
        if e["release_url"]:
            lines.append(f'<a href="{_esc(e["release_url"])}">Подробнее о релизе</a>')
        lines.append("")
    return "\n".join(lines).strip()


def project_names_map() -> dict:
    return {p["id"]: p["name"] for p in database.list_projects()}

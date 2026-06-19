"""Основная логика заданий: опрос проектов и формирование/рассылка дайджеста."""
import datetime as dt

from . import database, tracker, releasenotes, translator, notifier


def poll_project(project) -> dict:
    """Опрашивает один проект; при изменении создаёт событие обновления."""
    res = tracker.check_project(project)
    database.touch_project_checked(project["id"])

    if res.get("error"):
        return {"id": project["id"], "name": project["name"], "status": "error",
                "detail": res["error"]}

    if res.get("baseline"):
        # Первый замер — фиксируем состояние без уведомления.
        database.set_project_state(project["id"], res["new_digest"], res["new_version"])
        return {"id": project["id"], "name": project["name"], "status": "baseline",
                "detail": res["new_version"] or res["new_digest"][:19]}

    if res["changed"]:
        notes = releasenotes.get_release_notes(project, res["new_version"])
        database.create_event({
            "project_id": project["id"],
            "old_version": res["old_version"],
            "new_version": res["new_version"],
            "old_digest": res["old_digest"],
            "new_digest": res["new_digest"],
            "notes_original": notes["body"],
            "notes_ru": None,
            "release_url": notes["url"],
        })
        database.set_project_state(project["id"], res["new_digest"], res["new_version"])
        return {"id": project["id"], "name": project["name"], "status": "updated",
                "detail": f'{res["old_version"]} → {res["new_version"]}'}

    return {"id": project["id"], "name": project["name"], "status": "uptodate",
            "detail": res["new_version"] or (res["new_digest"] or "")[:19]}


def run_poll() -> list[dict]:
    """Опрашивает все включённые проекты."""
    results = []
    for project in database.list_projects(enabled_only=True):
        results.append(poll_project(project))
    database.log_action("poll", f"проверено: {len(results)}")
    return results


def run_broadcast(force: bool = False) -> dict:
    """Собирает дайджест из необработанных событий, переводит и отправляет в Telegram.

    Идемпотентность: при автозапуске не отправляет повторно в один и тот же день.
    """
    today = dt.date.today().isoformat()
    last = database.get_setting("last_broadcast_date", "")
    if not force and last == today:
        return {"sent": False, "reason": "уже отправлено сегодня"}

    # Перед рассылкой — свежий опрос, чтобы данные были актуальны.
    run_poll()

    events = database.list_unnotified_events()
    checked = len(database.list_projects(enabled_only=True))

    # Перевод заметок на русский (на этапе рассылки, чтобы экономить вызовы API).
    for e in events:
        if e["notes_original"] and not e["notes_ru"]:
            ru, ok = translator.translate_to_russian(e["notes_original"])
            if ok:
                database.update_event_notes(e["id"], ru)
    events = database.list_unnotified_events()  # перечитываем с переводами

    token = database.get_setting("telegram_token", "")
    chat_id = database.get_setting("telegram_chat_id", "")
    text = notifier.build_digest(events, notifier.project_names_map(), checked)

    ok, detail = notifier.send_message(token, chat_id, text)
    if ok:
        database.mark_events_notified([e["id"] for e in events])
        database.set_setting("last_broadcast_date", today)
        database.log_action("broadcast", f"событий: {len(events)}")
    else:
        database.log_action("broadcast_error", detail)

    return {"sent": ok, "events": len(events), "detail": detail}

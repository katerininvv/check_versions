"""Основная логика заданий: опрос проектов и отправка уведомлений об обновлениях.

Модель: несколько раз в день проверяем проекты. Если с прошлой проверки появились
новые обновления — присылаем сообщение в Telegram. Если обновлений нет — молчим.
"""
from . import database, tracker, releasenotes, translator, notifier


def poll_project(project) -> dict:
    """Опрашивает один проект; при изменении создаёт событие обновления."""
    res = tracker.check_project(project)
    database.touch_project_checked(project["id"])

    if res.get("error"):
        return {"id": project["id"], "name": project["name"], "status": "error",
                "detail": res["error"]}

    if res.get("baseline"):
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
    """Опрашивает все включённые проекты (без отправки уведомлений)."""
    results = [poll_project(p) for p in database.list_projects(enabled_only=True)]
    database.log_action("poll", f"проверено: {len(results)}")
    return results


def run_check_and_notify(force: bool = False) -> dict:
    """Проверяет проекты и присылает уведомление, ЕСЛИ есть новые обновления.

    Если обновлений нет — ничего не отправляет (при force=True отправит сообщение
    «обновлений нет» — удобно для ручной проверки связи).
    """
    run_poll()

    events = database.list_unnotified_events()
    checked = len(database.list_projects(enabled_only=True))

    if not events and not force:
        # Тихий режим: нет обновлений — не шлём ничего.
        database.log_action("check", "обновлений нет")
        return {"sent": False, "events": 0, "reason": "обновлений нет"}

    # Перевод заметок на русский (только при наличии новых событий).
    for e in events:
        if e["notes_original"] and not e["notes_ru"]:
            ru, ok = translator.translate_to_russian(e["notes_original"])
            if ok:
                database.update_event_notes(e["id"], ru)
    events = database.list_unnotified_events()

    token = database.get_setting("telegram_token", "")
    chat_id = database.get_setting("telegram_chat_id", "")
    text = notifier.build_digest(events, notifier.project_names_map(), checked)

    ok, detail = notifier.send_message(token, chat_id, text)
    if ok:
        database.mark_events_notified([e["id"] for e in events])
        database.log_action("notify", f"обновлений: {len(events)}")
    else:
        database.log_action("notify_error", detail)

    return {"sent": ok, "events": len(events), "detail": detail}

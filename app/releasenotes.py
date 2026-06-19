"""Получение описаний релизов (release notes) — best-effort из GitHub.

Docker Hub редко содержит внятные заметки о релизе, поэтому описание берётся
из связанного репозитория проекта. Репозиторий определяется по полю repo_url
проекта, либо эвристически как github.com/{namespace}/{repository}.
"""
import re
import httpx

from . import database


def _parse_github(repo_url: str | None, namespace: str, repository: str):
    """Возвращает (owner, repo) или None."""
    if repo_url:
        m = re.search(r"github\.com[/:]+([^/]+)/([^/#?]+)", repo_url)
        if m:
            return m.group(1), m.group(2).removesuffix(".git")
    # Эвристика: namespace/repository (для wg-easy/wg-easy совпадает с GitHub)
    if namespace and namespace != "library":
        return namespace, repository
    return None


def get_release_notes(project, version: str | None) -> dict:
    """Возвращает {title, body, url} или пустые поля, если ничего не найдено."""
    empty = {"title": None, "body": None, "url": None}
    gh = _parse_github(project["repo_url"], project["namespace"], project["repository"])
    if not gh:
        return empty
    owner, repo = gh

    token = database.get_setting("github_token", "")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "update-tracker"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    candidates = []
    if version:
        v = version.lstrip("v")
        candidates += [f"tags/v{v}", f"tags/{v}", f"tags/{version}"]
    candidates.append("latest")

    try:
        with httpx.Client(headers=headers, follow_redirects=True) as client:
            for path in candidates:
                r = client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/releases/{path}",
                    timeout=20,
                )
                if r.status_code == 200:
                    data = r.json()
                    return {
                        "title": data.get("name") or data.get("tag_name"),
                        "body": (data.get("body") or "").strip() or None,
                        "url": data.get("html_url"),
                    }
    except Exception:
        return empty
    return empty

"""Отслеживание обновлений образов в Docker Hub.

Логика: для тега `:latest` запрашивается digest манифеста через Registry API.
Смена digest = новая продуктивная версия. Дополнительно через Hub API делается
best-effort сопоставление digest -> семантический тег версии (для отображения
"было vN -> стало vN+1").
"""
import re
import httpx

DOCKER_REGISTRY = "registry-1.docker.io"
DOCKER_AUTH = "https://auth.docker.io/token"
DOCKER_HUB_API = "https://hub.docker.com/v2"

_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

# Версионно-выглядящие теги: 1.2.3, v15, 15, 2024-01, и т.п. (не latest/stable)
_VERSION_RE = re.compile(r"^v?\d+(?:[.\-]\d+)*")


def parse_image_ref(ref: str) -> dict:
    """Разбирает строку образа в компоненты.

    Примеры:
      nginx                       -> docker.io/library/nginx:latest
      wg-easy/wg-easy:15          -> docker.io/wg-easy/wg-easy:15
      ghcr.io/wg-easy/wg-easy:15  -> ghcr.io/wg-easy/wg-easy:15
    """
    ref = ref.strip()
    # Отбрасываем digest, если указан (@sha256:...)
    if "@" in ref:
        ref = ref.split("@", 1)[0]

    registry = "docker.io"
    remainder = ref
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        registry = first
        remainder = ref.split("/", 1)[1]

    # Тег
    tag = "latest"
    if ":" in remainder:
        remainder, tag = remainder.rsplit(":", 1)

    parts = remainder.split("/")
    if registry == "docker.io" and len(parts) == 1:
        namespace, repository = "library", parts[0]
    else:
        namespace = "/".join(parts[:-1]) if len(parts) > 1 else parts[0]
        repository = parts[-1]
        if len(parts) == 1:
            namespace = ""

    return {
        "registry": registry,
        "namespace": namespace,
        "repository": repository,
        "tag": tag,
        "normalized": _normalized(registry, namespace, repository, tag),
    }


def _normalized(registry, namespace, repository, tag) -> str:
    repo = f"{namespace}/{repository}" if namespace else repository
    return f"{registry}/{repo}:{tag}"


def _docker_token(client: httpx.Client, repo_path: str) -> str:
    r = client.get(
        DOCKER_AUTH,
        params={"service": "registry.docker.io", "scope": f"repository:{repo_path}:pull"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("token", "")


def get_latest_digest(namespace: str, repository: str, tag: str = "latest") -> str:
    """Возвращает digest манифеста для указанного тега в Docker Hub."""
    repo_path = f"{namespace}/{repository}" if namespace else repository
    with httpx.Client(follow_redirects=True) as client:
        token = _docker_token(client, repo_path)
        url = f"https://{DOCKER_REGISTRY}/v2/{repo_path}/manifests/{tag}"
        headers = {"Accept": _ACCEPT}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = client.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        digest = r.headers.get("Docker-Content-Digest", "")
        return digest


def find_version_for_digest(namespace: str, repository: str, target_digest: str) -> str | None:
    """Best-effort: ищет семантический тег версии, указывающий на тот же образ,
    что и :latest. Использует Hub API. Возвращает имя тега или None.
    """
    repo_path = f"{namespace}/{repository}" if namespace else repository
    try:
        with httpx.Client() as client:
            r = client.get(
                f"{DOCKER_HUB_API}/repositories/{repo_path}/tags",
                params={"page_size": 100, "ordering": "last_updated"},
                timeout=20,
            )
            r.raise_for_status()
            results = r.json().get("results", [])
    except Exception:
        return None

    latest_updated = None
    candidates = []
    for t in results:
        name = t.get("name", "")
        digest = t.get("digest") or ""
        updated = t.get("last_updated")
        if name == "latest":
            latest_updated = updated
            if digest and digest == target_digest:
                pass  # latest сам совпал, ищем именованную версию ниже
            continue
        if not _VERSION_RE.match(name):
            continue
        # 1) Прямое совпадение digest
        if digest and target_digest and digest == target_digest:
            return name
        candidates.append((name, updated))

    # 2) Фолбэк: версия, обновлённая одновременно с latest
    if latest_updated and candidates:
        same_time = [n for n, u in candidates if u == latest_updated]
        if same_time:
            return _best_version(same_time)
    # 3) Фолбэк: самый "старший" из версионных тегов
    if candidates:
        return _best_version([n for n, _ in candidates])
    return None


def _best_version(names: list[str]) -> str:
    """Выбирает наиболее вероятную 'основную' версию из списка тегов."""
    def key(n):
        nums = re.findall(r"\d+", n)
        return tuple(int(x) for x in nums) if nums else (0,)
    return sorted(names, key=key, reverse=True)[0]


def check_project(project) -> dict:
    """Проверяет проект на обновление.

    Возвращает словарь:
      {changed: bool, old_digest, new_digest, old_version, new_version, error}
    """
    result = {
        "changed": False, "old_digest": project["last_digest"],
        "new_digest": None, "old_version": project["last_version"],
        "new_version": None, "error": None,
    }
    try:
        digest = get_latest_digest(
            project["namespace"], project["repository"], project["tag"]
        )
    except Exception as exc:  # сетевые/HTTP ошибки не должны ронять опрос
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    if not digest:
        result["error"] = "Не удалось получить digest манифеста"
        return result

    result["new_digest"] = digest
    version = find_version_for_digest(
        project["namespace"], project["repository"], digest
    )
    result["new_version"] = version

    if project["last_digest"] and project["last_digest"] != digest:
        result["changed"] = True
    elif not project["last_digest"]:
        # Первый замер: фиксируем базовое состояние без уведомления.
        result["changed"] = False
        result["baseline"] = True
    return result

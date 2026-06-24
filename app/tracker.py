"""Отслеживание обновлений образов контейнеров в разных реестрах.

Логика: для тега `:latest` запрашивается digest манифеста через стандартный
OCI Distribution API. Поддерживаются Docker Hub, GHCR (ghcr.io), Quay, lscr.io и
другие OCI-совместимые реестры — авторизация определяется автоматически по
ответу реестра (заголовок WWW-Authenticate / Bearer-challenge).

Смена digest у `:latest` = новая продуктивная версия (сигнал к уведомлению).
Для Docker Hub дополнительно (best-effort) определяется семантический тег версии.
"""
import re
import httpx

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

# Псевдонимы Docker Hub
_DOCKERHUB = {"docker.io", "index.docker.io", "registry-1.docker.io", ""}


def parse_image_ref(ref: str) -> dict:
    """Разбирает строку образа в компоненты.

    Примеры:
      nginx                       -> docker.io/library/nginx:latest
      wg-easy/wg-easy:15          -> docker.io/wg-easy/wg-easy:15
      ghcr.io/wg-easy/wg-easy:15  -> ghcr.io/wg-easy/wg-easy:15
    """
    ref = ref.strip()
    if "@" in ref:  # отбрасываем digest, если указан
        ref = ref.split("@", 1)[0]

    registry = "docker.io"
    remainder = ref
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        registry = first
        remainder = ref.split("/", 1)[1]

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


def _registry_host(registry: str) -> str:
    """Возвращает фактический хост реестра для обращения по API."""
    if registry in _DOCKERHUB:
        return "registry-1.docker.io"
    return registry


def _parse_www_auth(value: str) -> dict:
    """Разбирает заголовок WWW-Authenticate: Bearer realm="...",service="...",scope="..."."""
    return dict(re.findall(r'(\w+)="([^"]*)"', value or ""))


def _request_manifest(client, host, repo_path, tag, token=None):
    url = f"https://{host}/v2/{repo_path}/manifests/{tag}"
    headers = {"Accept": _ACCEPT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.get(url, headers=headers, timeout=20)


def get_latest_digest(registry: str, namespace: str, repository: str, tag: str = "latest") -> str:
    """Возвращает digest манифеста тега в указанном реестре.

    Использует стандартный Bearer-challenge: сначала пробуем без токена, если
    реестр отвечает 401 с WWW-Authenticate — получаем токен и повторяем запрос.
    Это работает для Docker Hub, GHCR, Quay и других OCI-реестров.
    """
    host = _registry_host(registry)
    repo_path = f"{namespace}/{repository}" if namespace else repository

    with httpx.Client(follow_redirects=True) as client:
        r = _request_manifest(client, host, repo_path, tag)

        if r.status_code == 401:
            params = _parse_www_auth(r.headers.get("WWW-Authenticate", ""))
            realm = params.get("realm")
            if realm:
                token_params = {}
                if params.get("service"):
                    token_params["service"] = params["service"]
                token_params["scope"] = params.get("scope") or f"repository:{repo_path}:pull"
                tr = client.get(realm, params=token_params, timeout=20)
                tr.raise_for_status()
                data = tr.json()
                token = data.get("token") or data.get("access_token", "")
                r = _request_manifest(client, host, repo_path, tag, token)
            elif registry in _DOCKERHUB:
                # Фолбэк для Docker Hub, если challenge не пришёл
                tr = client.get(DOCKER_AUTH, params={
                    "service": "registry.docker.io",
                    "scope": f"repository:{repo_path}:pull",
                }, timeout=20)
                tr.raise_for_status()
                r = _request_manifest(client, host, repo_path, tag, tr.json().get("token"))

        r.raise_for_status()
        return r.headers.get("Docker-Content-Digest", "")


def find_version_for_digest(namespace: str, repository: str, target_digest: str) -> str | None:
    """Best-effort (только Docker Hub): ищет семантический тег версии, который
    указывает на тот же образ, что и :latest. Возвращает имя тега или None.
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
            continue
        if not _VERSION_RE.match(name):
            continue
        if digest and target_digest and digest == target_digest:
            return name
        candidates.append((name, updated))

    if latest_updated and candidates:
        same_time = [n for n, u in candidates if u == latest_updated]
        if same_time:
            return _best_version(same_time)
    if candidates:
        return _best_version([n for n, _ in candidates])
    return None


def _best_version(names: list[str]) -> str:
    def key(n):
        nums = re.findall(r"\d+", n)
        return tuple(int(x) for x in nums) if nums else (0,)
    return sorted(names, key=key, reverse=True)[0]


def check_project(project) -> dict:
    """Проверяет проект на обновление. Возвращает словарь с результатом."""
    result = {
        "changed": False, "old_digest": project["last_digest"],
        "new_digest": None, "old_version": project["last_version"],
        "new_version": None, "error": None,
    }
    registry = project["registry"]
    try:
        digest = get_latest_digest(
            registry, project["namespace"], project["repository"], project["tag"]
        )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 404):
            result["error"] = (
                f"Образ не найден в реестре {registry} "
                f"(HTTP {code}: возможно, он приватный или указан неверно)"
            )
        else:
            result["error"] = f"Ошибка реестра {registry}: HTTP {code}"
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    if not digest:
        result["error"] = "Реестр не вернул digest манифеста"
        return result

    result["new_digest"] = digest
    # Определение версии по тегу — пока только для Docker Hub.
    if registry in _DOCKERHUB:
        result["new_version"] = find_version_for_digest(
            project["namespace"], project["repository"], digest
        )

    if project["last_digest"] and project["last_digest"] != digest:
        result["changed"] = True
    elif not project["last_digest"]:
        result["baseline"] = True
    return result

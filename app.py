"""Локальная библиотека и каталог подключённых источников."""

import asyncio
import base64
from dataclasses import asdict, dataclass, field, replace
from io import BytesIO
import json
import logging
from math import ceil, isfinite
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
from time import monotonic, sleep
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener, urlopen

# Терминал без ответа на запрос графики не должен печатать traceback до запуска NXCT.
logging.getLogger("textual_image._terminal").setLevel(logging.ERROR)

from PIL import Image as PILImage

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Footer,
    Input,
    Label,
    LoadingIndicator,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)
from textual_image.widget import Image as TerminalImage


SETTINGS_PATH = Path.home() / ".config" / "nxct" / "settings.json"
LIBRARY_PATH = SETTINGS_PATH.with_name("library.json")
HYDRA_API = "https://hydra-api-us-east-1.losbroxas.org"
APP_VERSION = "0.1.8"
RELEASES_API = "https://api.github.com/repos/sxxlxxr/nxct/releases/latest"
LINUX_ASSET = "nxct-linux-x86_64.tar.gz"
RQBIT_OPENER = build_opener(ProxyHandler({}))


@dataclass(frozen=True)
class CatalogueGame:
    title: str
    shop: str
    object_id: str
    genre: str
    source: str = "—"
    image_url: str | None = None


@dataclass(frozen=True)
class DownloadSource:
    name: str
    url: str
    id: str
    fingerprint: str


DEFAULT_SOURCES = (
    DownloadSource("FitGirl", "https://hydralinks.cloud/sources/fitgirl.json", "w4nVajB6",
                   "a251968f4ffcff43986d1adca686a438312fc26201875e16120ea84b199d2048"),
    DownloadSource("GOG", "https://hydralinks.cloud/sources/gog.json", "penJ2nlG",
                   "6000be43dd13beb384b74f771f7a4597bee4618478f41b796f4a3992f9658cc1"),
    DownloadSource("onlinefix", "https://hydralinks.cloud/sources/onlinefix.json", "BVnaGLvo",
                   "fc218d44a284f8ea88bcc62990025a2b5065c9976d0a789c0c313de0be6078c7"),
    DownloadSource("Xatab", "https://hydralinks.cloud/sources/xatab.json", "8QjOPn2e",
                   "1241d6c2eb050f5717d5397a468cef7d68c9485e67d217cb88634c01a38c8b80"),
    DownloadSource("0xEMPRESS", "https://hydralinks.cloud/sources/empress.json", "xqg1lnV4",
                   "f5499b164808bfe6fed2adc27fdc6362129490e754b5b089a1993522abe22905"),
)


@dataclass(frozen=True)
class TorrentVariant:
    title: str
    size: str
    magnet: str
    info_hash: str


@dataclass
class TorrentDownload:
    game: CatalogueGame
    variant: TorrentVariant
    destination: Path
    state: str = "Ожидание"
    progress: float = 0.0
    speed: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    eta_seconds: int | None = None
    connected_peers: int | None = None
    connecting_peers: int | None = None
    started: bool = False
    process: subprocess.Popen | None = None
    port: int | None = None
    token: str | None = None
    torrent_id: int | None = None


def format_data_size(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if amount < 1024 or unit == "ТиБ":
            return f"{int(amount)} Б" if unit == "Б" else f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("недостижимый размер")


def format_eta(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин {seconds % 60} с"
    if seconds < 86400:
        return f"{seconds // 3600} ч {(seconds % 3600) // 60} мин"
    return f"{seconds // 86400} д {(seconds % 86400) // 3600} ч"


def local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def rqbit_api(entry: TorrentDownload, path: str, *, post: bytes | None = None,
              timeout: float = 12) -> object:
    """Вызывает только локальный API дочернего процесса с отдельным паролем."""
    if entry.port is None or entry.token is None or not path.startswith("/"):
        raise ValueError("движок не запущен")
    credentials = base64.b64encode(f"nxct:{entry.token}".encode()).decode("ascii")
    request = Request(f"http://127.0.0.1:{entry.port}{path}", data=post,
                      headers={"Accept": "application/json", "Authorization": f"Basic {credentials}"})
    with RQBIT_OPENER.open(request, timeout=timeout) as response:
        payload = response.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValueError("слишком большой ответ движка")
    return json.loads(payload)


def wait_for_rqbit(entry: TorrentDownload) -> None:
    until = monotonic() + 8
    while monotonic() < until:
        if entry.process is None or entry.process.poll() is not None:
            raise RuntimeError("rqbit завершился до запуска локального API")
        try:
            response = rqbit_api(entry, "/", timeout=0.4)
            if isinstance(response, dict) and response.get("server") == "rqbit":
                return
        except (URLError, OSError, TimeoutError):
            pass
        sleep(0.2)
    raise TimeoutError("rqbit не ответил за 8 секунд")


def hydra_json(path: str, body: dict | None = None) -> object:
    headers = {"Accept": "application/json", "User-Agent": "NXCT/0.1"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(HYDRA_API + path, headers=headers,
                      data=json.dumps(body).encode("utf-8") if body is not None else None)
    with urlopen(request, timeout=12, context=https_context()) as response:
        payload = response.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValueError("ответ каталога слишком большой")
    return json.loads(payload)


class NoImageRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def fetch_game_cover(url: str) -> PILImage.Image:
    """Загружает только ограниченные по размеру изображения с известных HTTPS-CDN."""
    if not isinstance(url, str) or len(url) > 2048 or any(char in url for char in "\r\n\t"):
        raise ValueError("неверная ссылка на обложку")
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    trusted_hosts = ("steamstatic.com", "akamaihd.net", "losbroxas.org",
                     "steamgriddb.com", "launchbox-app.com")
    if (parsed.scheme != "https" or not parsed.path.startswith("/") or parsed.username
            or parsed.password or parsed.fragment or parsed.port not in (None, 443)
            or not any(host == allowed or host.endswith("." + allowed) for allowed in trusted_hosts)):
        raise ValueError("неразрешённый адрес обложки")
    opener = build_opener(HTTPSHandler(context=https_context()), NoImageRedirect())
    request = Request(url, headers={"Accept": "image/jpeg, image/png, image/webp",
                                    "User-Agent": f"NXCT/{APP_VERSION}"})
    with opener.open(request, timeout=8) as response:
        if response.headers.get_content_type() not in ("image/jpeg", "image/png", "image/webp"):
            raise ValueError("неверный формат обложки")
        payload = response.read(4_000_001)
    if len(payload) > 4_000_000:
        raise ValueError("слишком большая обложка")
    with PILImage.open(BytesIO(payload)) as image:
        if image.width * image.height > 8_000_000:
            raise ValueError("слишком большое разрешение обложки")
        image.thumbnail((800, 800))
        return image.convert("RGB")


def bundled_rqbit() -> str | None:
    """Используем комплектный движок, не полагаясь на PATH пользователя."""
    base = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    binary = base / "rqbit"
    if binary.is_file() and os.access(binary, os.X_OK):
        return str(binary)
    return shutil.which("rqbit")


def bundled_installer() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    script = Path(sys.executable).resolve().parent / "install.sh"
    return script if script.is_file() else None


def https_context() -> ssl.SSLContext:
    """Используем системные CA, даже если комплектный OpenSSL ищет их не там."""
    if getattr(sys, "frozen", False) and not os.environ.get("SSL_CERT_FILE"):
        if ssl.get_default_verify_paths().cafile is None:
            for candidate in (
                "/etc/ssl/cert.pem",
                "/etc/ssl/certs/ca-certificates.crt",
                "/etc/pki/tls/cert.pem",
                "/etc/pki/tls/certs/ca-bundle.crt",
            ):
                if Path(candidate).is_file():
                    return ssl.create_default_context(cafile=candidate)
    return ssl.create_default_context()


def installer_environment() -> dict[str, str]:
    """Не даём библиотекам PyInstaller подменить системные библиотеки curl."""
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
    return env


def latest_release_version() -> str | None:
    """Проверяет только опубликованный стабильный Linux-релиз NXCT."""
    request = Request(RELEASES_API, headers={"Accept": "application/vnd.github+json",
                                             "User-Agent": f"NXCT/{APP_VERSION}"})
    try:
        with urlopen(request, timeout=12, context=https_context()) as response:
            payload = response.read(2_000_001)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise
    if len(payload) > 2_000_000:
        raise ValueError("ответ GitHub слишком большой")
    release = json.loads(payload)
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        raise ValueError("неверный формат ответа GitHub")
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("неверная версия релиза")
    assets = release.get("assets")
    if not isinstance(assets, list) or not any(
            isinstance(asset, dict) and asset.get("name") == LINUX_ASSET for asset in assets):
        raise ValueError("у релиза нет сборки для Linux x86_64")
    return tag[1:]


def version_number(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("неверный номер версии")
    return tuple(map(int, value.split(".")))


def fetch_source_page(page: int, query: str, sources: list[DownloadSource]) -> tuple[list[CatalogueGame], int, int]:
    """Страница каталога Hydra для выбранных пользователем источников."""
    if page < 1:
        raise ValueError("неверный номер страницы")
    if not sources:
        raise ValueError("сначала добавьте источник")
    data = hydra_json("/catalogue/search", {
        "title": query, "sortBy": "popularity", "sortOrder": "desc",
        "downloadSourceIds": [source.id for source in sources],
        "downloadSourceFingerprints": [source.fingerprint for source in sources],
        "take": 20, "skip": (page - 1) * 20,
    })
    if not isinstance(data, dict) or not isinstance(data.get("edges"), list):
        raise ValueError("неизвестный формат ответа каталога")
    total = data.get("count")
    if type(total) is not int or total < 0:
        raise ValueError("неверное число игр каталога")
    games = []
    for item in data["edges"][:20]:
        if not isinstance(item, dict):
            continue
        title, shop, object_id = (item.get(key) for key in ("title", "shop", "objectId"))
        if not all(isinstance(value, str) and value.strip() for value in (title, shop, object_id)):
            continue
        if not re.fullmatch(r"[a-z]+", shop) or not re.fullmatch(r"[\w-]+", object_id):
            continue
        genres = item.get("genres")
        genre = ", ".join(value[:30] for value in genres[:2] if isinstance(value, str)) if isinstance(genres, list) else ""
        names = item.get("downloadSources")
        source = ", ".join(value[:40] for value in names[:3] if isinstance(value, str)) if isinstance(names, list) else ""
        image_url = item.get("libraryImageUrl")
        games.append(CatalogueGame(title.strip()[:180], shop, object_id, genre or "—", source or "—",
                                   image_url if isinstance(image_url, str) and len(image_url) <= 2048 else None))
    return games, total, max(1, (total + 19) // 20)


def register_source(url: str) -> DownloadSource:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (len(url) > 1024 or parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.fragment or parsed.port not in (None, 443)
            or not re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", host)
            or host.lower().endswith((".local", ".internal", ".localhost"))):
        raise ValueError("Укажите публичную HTTPS-ссылку на JSON источника Hydra.")
    data = hydra_json("/download-sources", {"url": url})
    if not isinstance(data, dict):
        raise ValueError("неизвестный ответ при добавлении источника")
    source_id, name, fingerprint = (data.get(key) for key in ("id", "name", "fingerprint"))
    if (not isinstance(source_id, str) or not re.fullmatch(r"[\w-]{4,100}", source_id)
            or not isinstance(name, str) or not name.strip()
            or not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint)):
        raise ValueError("Источник пока не готов для поиска: нет ID или fingerprint.")
    return DownloadSource(name.strip()[:80], url, source_id, fingerprint.lower())


def magnet_info_hash(magnet: str) -> str | None:
    if not isinstance(magnet, str) or not magnet.startswith("magnet:?"):
        return None
    for xt in parse_qs(urlsplit(magnet).query).get("xt", []):
        if xt.lower().startswith("urn:btih:"):
            digest = xt[9:]
            if re.fullmatch(r"[0-9a-fA-F]{40}", digest):
                return digest.lower()
            if re.fullmatch(r"[A-Z2-7a-z2-7]{32}", digest):
                return base64.b32decode(digest.upper()).hex()
    return None


def fetch_variants(game: CatalogueGame, source_ids: tuple[str, ...]) -> list[TorrentVariant]:
    params = urlencode({"take": 50, "skip": 0}) + "&" + urlencode(
        [("downloadSourceIds[]", source_id) for source_id in source_ids])
    data = hydra_json(f"/games/{game.shop}/{game.object_id}/download-sources?{params}")
    if not isinstance(data, list):
        raise ValueError("неизвестный формат вариантов загрузки")
    variants = []
    for item in data[:50]:
        if not isinstance(item, dict) or item.get("downloadSourceId") not in source_ids:
            continue
        title, uris = item.get("title"), item.get("uris")
        if not isinstance(title, str) or not isinstance(uris, list):
            continue
        size = item.get("fileSize")
        for uri in uris:
            info_hash = magnet_info_hash(uri)
            if info_hash:
                source_name = item.get("downloadSourceName")
                label = f"{source_name} · {title}" if isinstance(source_name, str) else title
                variants.append(TorrentVariant(label.strip()[:160] or game.title,
                                               str(size)[:40] if size is not None else "—",
                                               uri, info_hash))
    return variants


@dataclass
class InstalledGame:
    title: str
    executable: str
    location: str
    play_seconds: float = 0.0


def load_library() -> tuple[str | None, list[InstalledGame], str]:
    try:
        data = json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("games"), list):
            raise ValueError("неверный формат библиотеки")
        root = data.get("scan_dir")
        root = root if isinstance(root, str) and root else None
        games = []
        for item in data["games"]:
            if not isinstance(item, dict):
                continue
            title, executable, location = (item.get(key) for key in ("title", "executable", "location"))
            seconds = item.get("play_seconds", 0)
            if (all(isinstance(value, str) and value for value in (title, executable, location))
                    and type(seconds) in (int, float) and 0 <= seconds < 10 ** 12):
                games.append(InstalledGame(title, executable, location, float(seconds)))
        return root, games, ""
    except FileNotFoundError:
        return None, [], ""
    except (OSError, ValueError) as error:
        return None, [], f"Не удалось прочитать библиотеку: {error}"


def inspect_scan_directory(value: str) -> tuple[Path | None, str]:
    if not value.strip():
        return None, "Укажите папку с играми."
    try:
        path = Path(value).expanduser().resolve()
        if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
            return None, "Папка не существует или недоступна для чтения."
        if any(part.casefold() in {"steam", ".steam", "steamapps", "steamlibrary"} for part in path.parts):
            return None, "Папки Steam не сканируются."
        return path, "Будут проверены эта папка и подпапки (до трёх уровней)."
    except (OSError, ValueError, RuntimeError):
        return None, "Не удалось проверить папку."


def inspect_executable(value: str) -> tuple[Path | None, str]:
    try:
        path = Path(value).expanduser().resolve()
        windows_game = path.suffix.casefold() == ".exe"
        if not path.is_file() or not os.access(path, os.R_OK if windows_game else os.X_OK):
            return None, "Файл не найден или нет прав на его запуск/чтение."
        with path.open("rb") as stream:
            signature = stream.read(4)
        if windows_game:
            if not signature.startswith(b"MZ"):
                return None, "Это не исполняемый файл Windows (EXE)."
        elif not (signature.startswith(b"\x7fELF") or signature.startswith(b"#!")):
            return None, "Поддерживаются Linux-программы, скрипты и Windows EXE."
        return path, ""
    except (OSError, ValueError, RuntimeError):
        return None, "Не удалось проверить исполняемый файл."


def discover_games(root: Path) -> tuple[list[InstalledGame], bool]:
    """Ищет локальные Linux-игры и EXE, не заходя в Steam и скрытые каталоги."""
    candidates: dict[str, tuple[int, InstalledGame]] = {}
    incomplete = False
    examined = 0
    excluded = {"steam", ".steam", "steamapps", "steamlibrary", "compatdata"}
    unwanted = ("setup", "install", "uninstall", "update", "crash", "vcredist", "wine", "proton")

    def failed(_error: OSError) -> None:
        nonlocal incomplete
        incomplete = True

    for directory, subdirs, files in os.walk(root, topdown=True, followlinks=False, onerror=failed):
        relative_dir = Path(directory).relative_to(root)
        depth = len(relative_dir.parts)
        subdirs[:] = [name for name in subdirs if not name.startswith(".")
                      and name.casefold() not in excluded
                      and not (Path(directory) / name).is_symlink()] if depth < 3 else []
        for filename in files:
            examined += 1
            if examined > 20000:
                return [entry for _, entry in candidates.values()], True
            suffix = Path(filename).suffix.casefold()
            if (filename.startswith(".") or filename.casefold().startswith(unwanted)
                    or suffix in {".dll", ".so", ".desktop", ".py"}):
                continue
            path = Path(directory) / filename
            if path.is_symlink() or not os.access(path, os.R_OK if suffix == ".exe" else os.X_OK):
                continue
            executable, _ = inspect_executable(str(path))
            if executable is None:
                continue
            relative = path.relative_to(root)
            if len(relative.parts) == 1:
                location = str(executable)
                title = path.name
                for extension in (".AppImage", ".x86_64", ".x86", ".sh", ".exe"):
                    if title.lower().endswith(extension.lower()):
                        title = title[:-len(extension)]
                        break
                score = 10
            else:
                title = relative.parts[0]
                location = str(root / title)
                name = path.stem.casefold().replace("_", "").replace("-", "")
                folder = title.casefold().replace("_", "").replace("-", "").replace(" ", "")
                score = (100 if name == folder else 70 if folder in name else
                         50 if name in {"start", "run", "launch", "game"} else 10)
                score -= len(relative.parts)
            game = InstalledGame(title, str(executable), location)
            if location not in candidates or candidates[location][0] < score:
                candidates[location] = score, game
    return [entry for _, entry in candidates.values()], incomplete

THEME_OPTIONS = (
    ("Монохромная", "nxct-mono"),
    ("Тёмная бирюза", "nxct"),
    ("Светлая", "nxct-light"),
    ("Фиолетовая", "nxct-violet"),
    ("Янтарная", "nxct-amber"),
)

# Дополнительные переменные используются также в модальных окнах и заставке.
THEME_PALETTES = {
    "nxct-mono": {
        "bg": "#0e0e0e", "panel": "#1b1b1b", "table": "#141414",
        "overlay": "#000000cc", "accent": "#e0e0e0", "text": "#ebebeb",
        "secondary": "#bdbdbd", "muted": "#a3a3a3", "subtle": "#828282",
        "hover": "#292929", "hover-border": "#777777",
        "selected": "#454545", "selected-text": "#ffffff",
        "input-border": "#606060", "header": "#303030",
        "header-text": "#e0e0e0", "border": "#555555",
        "title": "#ffffff", "note": "#c4c4c4",
        "catalogue": "#202020", "strip": "#252525",
        "strip-text": "#bdbdbd",
    },
    "nxct": {
        "bg": "#0c1118", "panel": "#111b27", "table": "#0f1722",
        "overlay": "#060b12cc", "accent": "#65e0c3", "text": "#d9e2ef",
        "secondary": "#9caec4", "muted": "#8595ab", "subtle": "#62758d",
        "hover": "#1b3440", "hover-border": "#397970",
        "selected": "#224c49", "selected-text": "#e6fff7",
        "input-border": "#25364a", "header": "#192737",
        "header-text": "#9db0c6", "border": "#2c4154",
        "title": "#edf7ff", "note": "#a9a18b",
        "catalogue": "#172638", "strip": "#172332",
        "strip-text": "#94a8bd",
    },
    "nxct-light": {
        "bg": "#f2f6f9", "panel": "#ffffff", "table": "#eaf1f5",
        "overlay": "#253a52aa", "accent": "#086e68", "text": "#243445",
        "secondary": "#425c6c", "muted": "#536a79", "subtle": "#607888",
        "hover": "#dcefea", "hover-border": "#439b89",
        "selected": "#c6e9de", "selected-text": "#133e3c",
        "input-border": "#a9c1c9", "header": "#dce7eb",
        "header-text": "#324e5e", "border": "#9bb7c0",
        "title": "#142c38", "note": "#755517",
        "catalogue": "#e7f1f2", "strip": "#dce9ec",
        "strip-text": "#3e5b69",
    },
    "nxct-violet": {
        "bg": "#110f1d", "panel": "#1b1830", "table": "#161326",
        "overlay": "#0b0918cc", "accent": "#c3a0ff", "text": "#e9e3f5",
        "secondary": "#c4b5dc", "muted": "#a696bc", "subtle": "#8e7ca8",
        "hover": "#362b53", "hover-border": "#8f71b8",
        "selected": "#4b356b", "selected-text": "#f8efff",
        "input-border": "#514264", "header": "#2b2441",
        "header-text": "#c3b4dd", "border": "#564471",
        "title": "#faf2ff", "note": "#c7ad81",
        "catalogue": "#2a2340", "strip": "#241e37",
        "strip-text": "#bbaaD2",
    },
    "nxct-amber": {
        "bg": "#18130e", "panel": "#271d15", "table": "#1f1812",
        "overlay": "#100b07cc", "accent": "#ffca71", "text": "#f1e4d1",
        "secondary": "#d6bea0", "muted": "#b9a287", "subtle": "#947c61",
        "hover": "#44311e", "hover-border": "#a7773e",
        "selected": "#624222", "selected-text": "#fff1d9",
        "input-border": "#5c452f", "header": "#38291c",
        "header-text": "#d7bc92", "border": "#715439",
        "title": "#fff2dd", "note": "#d2b274",
        "catalogue": "#342619", "strip": "#302318",
        "strip-text": "#d0b590",
    },
}


@dataclass
class Settings:
    downloads_dir: str = ""
    games_dir: str = str(Path.home())
    startup_animation: bool = True
    theme: str = "nxct-mono"
    sources: list[DownloadSource] = field(default_factory=lambda: list(DEFAULT_SOURCES))


def write_settings(settings: Settings) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n"
    fd = os.open(SETTINGS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)


def load_settings() -> tuple[Settings, str]:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("ожидался объект JSON")
        settings = Settings()
        for key in ("downloads_dir", "games_dir"):
            if key in data:
                if not isinstance(data[key], str) or (key == "games_dir" and not data[key].strip()):
                    raise ValueError("неверный путь в настройках")
                setattr(settings, key, data[key])
        if "startup_animation" in data:
            if type(data["startup_animation"]) is not bool:
                raise ValueError("неверный параметр анимации")
            settings.startup_animation = data["startup_animation"]
        if "theme" in data and isinstance(data["theme"], str):
            if data["theme"] in THEME_PALETTES:
                settings.theme = data["theme"]
        if "sources" in data:
            if not isinstance(data["sources"], list):
                raise ValueError("неверный список источников")
            settings.sources = []
            for item in data["sources"][:30]:
                if not isinstance(item, dict):
                    continue
                fields = [item.get(key) for key in ("name", "url", "id", "fingerprint")]
                if (all(isinstance(value, str) for value in fields)
                        and re.fullmatch(r"[\w-]{4,100}", fields[2])
                        and re.fullmatch(r"[0-9a-fA-F]{64}", fields[3])):
                    settings.sources.append(DownloadSource(*fields))
        return settings, ""
    except FileNotFoundError:
        return Settings(), ""
    except (OSError, ValueError) as error:
        return Settings(), f"Не удалось прочитать настройки: {error}. Используются значения по умолчанию."


def inspect_directory(value: str) -> tuple[Path | None, str]:
    if not value.strip():
        return None, "Укажите существующую папку."
    try:
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            return None, "Папка не существует. Автоматически создавать её не будем."
        if not os.access(path, os.W_OK | os.X_OK):
            return None, "Нет прав на запись или доступ к папке."
        free = shutil.disk_usage(path).free / (1024 ** 3)
        return path, f"Доступна для записи · свободно {free:.1f} ГиБ"
    except (OSError, ValueError, RuntimeError):
        return None, "Не удалось проверить папку."


class FolderTree(DirectoryTree):
    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [path for path in paths if path.is_dir()]


class FolderPicker(ModalScreen[Path | None]):
    BINDINGS = [Binding("escape", "cancel", "Отмена")]
    DEFAULT_CSS = """
    FolderPicker { align: center middle; background: $nxct-overlay; }
    #folder-dialog { width: 90%; max-width: 100; height: 90%; padding: 1 2;
        border: round $nxct-accent; background: $nxct-panel; }
    #folder-title { height: auto; color: $nxct-accent; text-style: bold; margin-bottom: 1; }
    #folder-tree { height: 1fr; min-height: 3; background: $nxct-bg; }
    #folder-path { margin-top: 1; }
    #folder-info { height: auto; min-height: 2; color: $nxct-secondary; }
    #folder-actions { height: 3; }
    #folder-actions Button { margin-right: 1; }
    """

    def __init__(self, title: str, initial: str, *, scan_folder: bool = False) -> None:
        super().__init__()
        self.dialog_title = title
        self.initial = initial
        self.scan_folder = scan_folder

    def compose(self) -> ComposeResult:
        with Vertical(id="folder-dialog"):
            yield Static(self.dialog_title, id="folder-title", markup=False)
            yield FolderTree(Path.home().anchor, id="folder-tree")
            yield Input(self.initial, placeholder="Абсолютный путь к папке", id="folder-path")
            yield Static(id="folder-info", markup=False)
            with Horizontal(id="folder-actions"):
                yield Button("Выбрать папку", id="confirm-folder", variant="success")
                yield Button("Отмена", id="cancel-folder")

    def on_mount(self) -> None:
        self.check_path()
        self.query_one("#folder-path", Input).focus()

    def check_path(self) -> None:
        inspect = inspect_scan_directory if self.scan_folder else inspect_directory
        path, info = inspect(self.query_one("#folder-path", Input).value)
        self.query_one("#folder-info", Static).update(info)
        self.query_one("#confirm-folder", Button).disabled = path is None

    @on(Input.Changed, "#folder-path")
    def path_changed(self) -> None:
        self.check_path()

    @on(DirectoryTree.DirectorySelected, "#folder-tree")
    def directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        event.stop()
        self.query_one("#folder-path", Input).value = str(event.path)

    @on(Input.Submitted, "#folder-path")
    @on(Button.Pressed, "#confirm-folder")
    def confirm_folder(self) -> None:
        inspect = inspect_scan_directory if self.scan_folder else inspect_directory
        path, _ = inspect(self.query_one("#folder-path", Input).value)
        if path is not None:
            self.dismiss(path)
        else:
            self.check_path()

    @on(Button.Pressed, "#cancel-folder")
    def action_cancel(self) -> None:
        self.dismiss(None)


class StartupScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "skip", "Пропустить"),
        Binding("enter", "skip", "Пропустить"),
        Binding("space", "skip", "Пропустить"),
    ]
    DEFAULT_CSS = """
    StartupScreen { align: center middle; background: $nxct-bg; }
    #startup-card { width: 100%; height: auto; padding: 2; opacity: 0; }
    #startup-logo { height: auto; text-align: center; color: $nxct-accent; text-style: bold; }
    #startup-caption { height: auto; text-align: center; color: $nxct-secondary; margin-top: 2; }
    #startup-skip { text-align: center; height: auto; color: $nxct-subtle; margin-top: 2; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="startup-card"):
            yield Static(
                "███╗  ██╗ ██╗  ██╗  ██████╗ ████████╗\n"
                "████╗ ██║ ╚██╗██╔╝ ██╔════╝ ╚══██╔══╝\n"
                "██╔██╗██║  ╚███╔╝  ██║         ██║   \n"
                "██║╚████║  ██╔██╗  ██║         ██║   \n"
                "██║ ╚███║ ██╔╝ ██╗ ╚██████╗    ██║   \n"
                "╚═╝  ╚══╝ ╚═╝  ╚═╝  ╚═════╝    ╚═╝   ",
                id="startup-logo",
            )
            yield Static("ТВОИ ИГРЫ. ТВОЙ ВЫБОР.\nЛокальный каталог · без аккаунта · без телеметрии", id="startup-caption")
            yield Static("Enter / Esc / Пробел — пропустить", id="startup-skip")

    def on_mount(self) -> None:
        self.finished = False
        self.query_one("#startup-card").styles.animate("opacity", 1.0, duration=0.85, easing="out_cubic")
        self.set_timer(3.0, self.action_skip)

    def action_skip(self) -> None:
        if not self.finished:
            self.finished = True
            self.dismiss(None)


class TorrentPicker(ModalScreen[TorrentVariant | None]):
    BINDINGS = [Binding("escape", "cancel", "Отмена")]
    DEFAULT_CSS = """
    TorrentPicker { align: center middle; background: $nxct-overlay; }
    #torrent-dialog { width: 90%; max-width: 110; height: 75%; padding: 1 2;
        border: round $nxct-accent; background: $nxct-panel; }
    #torrent-options { height: 1fr; min-height: 5; }
    #torrent-note { height: auto; margin: 1 0; color: $nxct-secondary; }
    #torrent-picker-actions { height: auto; }
    #torrent-picker-actions Button { margin-right: 1; }
    """

    def __init__(self, game: CatalogueGame, variants: list[TorrentVariant]) -> None:
        super().__init__()
        self.game = game
        self.variants = variants
        self.selected = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="torrent-dialog"):
            yield Static(f"ВАРИАНТЫ / {self.game.title}", classes="eyebrow", markup=False)
            yield DataTable(id="torrent-options", cursor_type="row", zebra_stripes=True)
            yield Static("Торрент передаёт ваш IP участникам сети. Проверяйте права и безопасность файлов. "
                         "Загрузка начнётся только после выбора папки; файлы не запускаются автоматически.", id="torrent-note")
            with Horizontal(id="torrent-picker-actions"):
                yield Button("Выбрать вариант", id="confirm-torrent", variant="success")
                yield Button("Отмена", id="cancel-torrent")

    def on_mount(self) -> None:
        table = self.query_one("#torrent-options", DataTable)
        table.add_columns("Вариант", "Размер")
        for index, variant in enumerate(self.variants):
            table.add_row(variant.title, variant.size, key=str(index))
        table.focus()

    @on(DataTable.RowHighlighted, "#torrent-options")
    def highlight(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is not None:
            self.selected = int(event.row_key.value)

    @on(Button.Pressed, "#confirm-torrent")
    def confirm(self) -> None:
        self.dismiss(self.variants[self.selected])

    @on(Button.Pressed, "#cancel-torrent")
    def action_cancel(self) -> None:
        self.dismiss(None)


class DeleteTorrentConfirm(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Отмена")]
    DEFAULT_CSS = """
    DeleteTorrentConfirm { align: center middle; background: $nxct-overlay; }
    #delete-dialog { width: 90%; max-width: 100; height: auto; padding: 2;
        border: round $nxct-accent; background: $nxct-panel; }
    #delete-path { height: auto; margin: 1 0; color: $nxct-note; }
    #delete-actions { height: auto; }
    #delete-actions Button { margin-right: 1; }
    """

    def __init__(self, destination: Path) -> None:
        super().__init__()
        self.destination = destination

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-dialog"):
            yield Static("ОТМЕНИТЬ ЗАГРУЗКУ И УДАЛИТЬ ФАЙЛЫ?", classes="eyebrow")
            yield Static("После остановки движка будет удалена вся папка ниже, включая файлы, "
                         "которые вы могли добавить туда вручную. Отмена необратима.")
            yield Static(str(self.destination), id="delete-path", markup=False)
            with Horizontal(id="delete-actions"):
                yield Button("Удалить папку", id="confirm-delete", variant="error")
                yield Button("Оставить файлы", id="cancel-delete")

    @on(Button.Pressed, "#confirm-delete")
    def confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel-delete")
    def action_cancel(self) -> None:
        self.dismiss(False)


class UpdateConfirm(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Отмена")]
    DEFAULT_CSS = """
    UpdateConfirm { align: center middle; background: $nxct-overlay; }
    #update-dialog { width: 90%; max-width: 80; height: auto; padding: 2;
        border: round $nxct-accent; background: $nxct-panel; }
    #update-actions { height: auto; margin-top: 1; }
    #update-actions Button { margin-right: 1; }
    """

    def __init__(self, version: str) -> None:
        super().__init__()
        self.version = version

    def compose(self) -> ComposeResult:
        with Vertical(id="update-dialog"):
            yield Static(f"ОБНОВИТЬ NXCT ДО {self.version}?", classes="eyebrow", markup=False)
            yield Static("NXCT закроется, проверит архив релиза, установит новую версию "
                         "и запустится снова. Настройки и скачанные файлы останутся на месте.")
            with Horizontal(id="update-actions"):
                yield Button("Обновить", id="confirm-update", variant="success")
                yield Button("Отмена", id="cancel-update")

    @on(Button.Pressed, "#confirm-update")
    def confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel-update")
    def action_cancel(self) -> None:
        self.dismiss(False)


class GameCoverPreview(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Закрыть")]
    DEFAULT_CSS = """
    GameCoverPreview { align: center middle; background: $nxct-overlay; }
    #cover-preview-dialog { width: 90%; max-width: 75; height: 85%; padding: 1 2;
        border: round $nxct-accent; background: $nxct-panel; }
    #cover-preview-title { height: auto; color: $nxct-accent; text-style: bold; }
    #cover-preview-image { width: 100%; height: 1fr; }
    """

    def __init__(self, title: str, image: PILImage.Image) -> None:
        super().__init__()
        self.game_title = title
        self.cover_image = image

    def compose(self) -> ComposeResult:
        with Vertical(id="cover-preview-dialog"):
            yield Static(self.game_title, id="cover-preview-title", markup=False)
            yield TerminalImage(self.cover_image, id="cover-preview-image")
            yield Button("Закрыть", id="close-cover")

    @on(Button.Pressed, "#close-cover")
    def action_close(self) -> None:
        self.dismiss(None)


class CatalogueApp(App):
    TITLE = "NXCT — игровой каталог"
    SUB_TITLE = "Локальная библиотека · каталог по запросу"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("slash", "search", "Поиск"),
        Binding("6", "tab('library-tab')", "Библиотека", show=False),
        Binding("1", "tab('search-tab')", "Поиск", show=False),
        Binding("2", "tab('downloads-tab')", "Загрузки", show=False),
        Binding("3", "tab('sources-tab')", "Источники", show=False),
        Binding("4", "tab('settings-tab')", "Настройки", show=False),
        Binding("i", "preview_cover", "Обложка"),
        Binding("escape", "focus_results", "К результатам"),
        Binding("q", "quit", "Выход"),
    ]

    CSS = """
    Screen { background: $nxct-bg; color: $nxct-text; }
    #brandbar { height: 4; padding: 0 1; background: $nxct-panel; align-vertical: middle; scrollbar-size-horizontal: 1; }
    #brand { width: 7; height: 3; content-align: left middle; color: $nxct-accent; text-style: bold; }
    .nav-button { width: auto; min-width: 0; height: 3; padding: 0 1; background: $nxct-panel; color: $nxct-secondary; border: tall transparent; }
    .nav-button:hover, .nav-button:focus { background: $nxct-hover; border: tall $nxct-hover-border; }
    .nav-button.active { background: $nxct-selected; color: $nxct-selected-text; border: tall $nxct-accent; text-style: bold; }
    #market-nav { height: 3; padding: 0 1; background: $nxct-panel; scrollbar-size-horizontal: 1; }
    #library-games { height: 1fr; min-height: 5; background: $nxct-table; }
    #library-empty { height: auto; border: round $nxct-border; padding: 1 2; color: $nxct-muted; }
    #library-root { height: auto; margin-bottom: 1; color: $nxct-secondary; }
    #library-actions, #library-controls { height: auto; margin-bottom: 1; }
    #library-actions Button, #library-controls Button { margin-right: 1; }
    #library-executable { width: 1fr; }
    #library-status { height: auto; min-height: 2; color: $nxct-accent; }
    TabbedContent { height: 1fr; }
    #tabs > ContentTabs { display: none; }
    ContentSwitcher { height: 1fr; }
    TabPane { padding: 1 2; }
    .eyebrow { color: $nxct-accent; text-style: bold; margin-bottom: 1; }
    .muted { color: $nxct-muted; }
    #search-input { border: tall $nxct-input-border; background: $nxct-panel; }
    #search-input:focus { border: tall $nxct-accent; }
    #search-tab .eyebrow { margin-bottom: 0; }
    #search-controls { height: auto; }
    #search-controls Input { width: 1fr; }
    #source-filter { width: 25; margin-right: 1; }
    #search-submit { margin-left: 1; }
    #remote-pages { height: auto; width: 60%; min-width: 47; }
    #remote-page { width: 1fr; min-width: 1; height: auto; color: $nxct-secondary; }
    #remote-pages Button { margin-right: 1; }
    #result-status { height: auto; min-height: 1; color: $nxct-secondary; }
    #result-status-row { height: auto; min-height: 1; }
    #result-status, #update-status { width: 1fr; }
    #search-loading, #update-loading { display: none; width: 3; height: 1; color: $nxct-accent; }
    #results-layout { height: 1fr; }
    #games { width: 3fr; height: 1fr; background: $nxct-table; border: round $nxct-border; }
    DataTable > .datatable--header { background: $nxct-header; color: $nxct-header-text; text-style: bold; }
    DataTable > .datatable--cursor { background: $nxct-selected; color: $nxct-selected-text; }
    #details { width: 2fr; min-width: 29; margin-left: 2; padding: 0 2; border: round $nxct-accent; background: $nxct-panel; }
    #details .eyebrow { margin-bottom: 0; }
    #game-title { height: auto; text-style: bold; color: $nxct-title; }
    #game-cover { display: none; width: 100%; height: 12; margin: 1 0; }
    #game-meta { height: auto; color: $nxct-accent; }
    #game-description { height: auto; color: $nxct-muted; margin-bottom: 2; }
    #game-source { height: auto; color: $nxct-secondary; margin-bottom: 2; }
    #choose-release { width: 100%; margin: 0 0 1 0; }
    #torrent-downloads { height: 1fr; min-height: 5; background: $nxct-table; border: round $nxct-border; }
    #downloads-scroll { height: 1fr; }
    #torrent-download-actions { height: auto; margin-top: 1; }
    #torrent-download-actions Button { margin-right: 1; }
    #download-detail { height: auto; min-height: 2; color: $nxct-text; margin-top: 1; }
    #torrent-download-status { height: auto; min-height: 1; color: $nxct-secondary; margin-top: 1; }
    .info-note { height: auto; color: $nxct-note; margin-top: 1; }
    .section-intro { margin-bottom: 2; color: $nxct-muted; }
    #sources-scroll { height: 1fr; }
    #source-table { height: 1fr; min-height: 7; background: $nxct-table; border: round $nxct-border; }
    #open-source-games { margin: 1 0; }
    #add-source-row { height: auto; margin-bottom: 1; }
    #add-source-row Input { width: 1fr; }
    #add-source-row Button { margin-left: 1; }
    #source-status, #connected-sources { height: auto; margin-bottom: 1; color: $nxct-secondary; }
    .setting-row { height: auto; min-height: 4; padding: 0 1; }
    .setting-label { width: 1fr; padding-top: 1; }
    #theme-select { width: 26; }
    Switch { width: auto; }
    #settings-note { margin-top: 2; color: $nxct-muted; }
    #settings-scroll { height: 1fr; }
    .path-row { height: auto; margin-bottom: 1; }
    .path-row Input { width: 1fr; }
    .path-row Button { width: 12; min-width: 12; margin-left: 1; }
    .path-info { height: auto; color: $nxct-muted; margin-bottom: 1; }
    #settings-status { height: auto; min-height: 2; margin-top: 1; color: $nxct-accent; }
    #update-status { height: auto; min-height: 2; margin-top: 1; color: $nxct-secondary; }
    #update-status-row { height: auto; min-height: 2; }
    #install-update { margin-top: 1; }
    #status-strip { height: 1; padding: 0 2; background: $nxct-strip; color: $nxct-strip-text; }
    Footer { background: $nxct-panel; }
    Screen.narrow #details { width: 1fr; min-width: 25; margin-left: 1; padding: 0 1; }
    Screen.narrow #games { width: 1fr; }
    Screen.narrow #remote-pages { width: 50%; min-width: 0; }
    Screen.narrow TabPane { padding: 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.settings, self.settings_warning = load_settings()
        self.library_root, self.library_games, self.library_warning = load_library()
        self.running_games: dict[str, tuple[subprocess.Popen, float]] = {}
        self.remote_games: list[CatalogueGame] = []
        self.remote_game: CatalogueGame | None = None
        self.cover_request = 0
        self.remote_page = 1
        self.remote_pages = 1
        self.remote_query = ""
        self.remote_filter = "all"
        self.remote_source_ids: tuple[str, ...] = ()
        self.search_loading_request = 0
        self.torrent_downloads: list[TorrentDownload] = []
        self.torrent_closing = False
        self.available_update: str | None = None
        self.pending_update: str | None = None
        for name, palette in THEME_PALETTES.items():
            self.register_theme(Theme(
                name=name, primary=palette["accent"], secondary=palette["hover-border"],
                success=palette["accent"], warning=palette["note"],
                error=palette["secondary"] if name == "nxct-mono" else None,
                foreground=palette["text"], background=palette["bg"],
                surface=palette["panel"], panel=palette["table"],
                dark=name != "nxct-light",
                variables={f"nxct-{key}": value for key, value in palette.items()},
            ))
        self.theme = self.settings.theme

    def compose(self) -> ComposeResult:
        with HorizontalScroll(id="brandbar"):
            yield Static("NXCT", id="brand")
            yield Button("Библиотека", id="open-library", classes="nav-button active")
            yield Button("Каталог", id="open-market", classes="nav-button")
        with HorizontalScroll(id="market-nav"):
            yield Button("Поиск", id="open-search", classes="nav-button active")
            yield Button("Загрузки", id="open-downloads", classes="nav-button")
            yield Button("Источники", id="open-sources", classes="nav-button")
            yield Button("Настройки", id="open-settings", classes="nav-button")
        with TabbedContent(initial="library-tab", id="tabs"):
            with TabPane("Библиотека", id="library-tab"):
                yield Label("МОЯ БИБЛИОТЕКА", classes="eyebrow")
                yield Static(id="library-root", markup=False)
                with Horizontal(id="library-actions"):
                    yield Button("Выбрать папку с играми", id="choose-library-folder", variant="success")
                    yield Button("Пересканировать", id="rescan-library", disabled=self.library_root is None)
                    yield Button("Убрать игру", id="remove-library-game", disabled=True)
                yield Static("Библиотека пуста. Выберите папку с играми — найденные игры добавятся автоматически.", id="library-empty")
                yield DataTable(id="library-games", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="library-controls"):
                    yield Button("Играть", id="launch-library-game", variant="success", disabled=True)
                    yield Input(placeholder="Путь к файлу Linux или Windows EXE", id="library-executable", disabled=True)
                    yield Button("Сохранить путь", id="save-library-path", disabled=True)
                yield Static(id="library-status", markup=False)
                yield Static("EXE запускаются через Wine. Время учитывается, пока NXCT открыт и процесс работает. Steam исключён.", classes="info-note")
            with TabPane("1  Поиск", id="search-tab"):
                yield Label("КАТАЛОГ ИГР", classes="eyebrow")
                with Horizontal(id="search-controls"):
                    yield Select([("Все источники", "all"),
                                  *((source.name, source.id) for source in self.settings.sources)],
                                 value="all", allow_blank=False, id="source-filter")
                    yield Input(placeholder="Название игры…  /", id="search-input")
                    yield Button("Найти →", id="search-submit", variant="success")
                with Horizontal(id="result-status-row"):
                    yield Static("Нажмите «Найти», чтобы открыть игры подключённых источников.", id="result-status", markup=False)
                    yield LoadingIndicator(id="search-loading")
                with Horizontal(id="results-layout"):
                    yield DataTable(id="games", cursor_type="row", zebra_stripes=True)
                    with VerticalScroll(id="details"):
                        yield Button("Варианты загрузки →", id="choose-release", variant="success", disabled=True)
                        yield Static("ВЫБРАННАЯ ИГРА", classes="eyebrow")
                        yield Static(id="game-title", markup=False)
                        yield TerminalImage(id="game-cover")
                        yield Static(id="game-meta", markup=False)
                        yield Static(id="game-description", markup=False)
                        yield Static(id="game-source", markup=False)
                with Horizontal(id="remote-pages"):
                    yield Button("← Назад", id="remote-prev", disabled=True)
                    yield Static("", id="remote-page", markup=False)
                    yield Button("Далее →", id="remote-next", disabled=True)
            with TabPane("2  Загрузки", id="downloads-tab"):
                with VerticalScroll(id="downloads-scroll"):
                    yield Label("МОИ ЗАГРУЗКИ", classes="eyebrow")
                    yield Static("Выберите игру → вариант → папку. После закрытия NXCT загрузки останавливаются.", classes="section-intro")
                    yield Static("Выберите загрузку, чтобы увидеть вариант и папку.", id="download-detail", markup=False)
                    yield DataTable(id="torrent-downloads", cursor_type="row", zebra_stripes=True)
                    with Horizontal(id="torrent-download-actions"):
                        yield Button("Пауза", id="pause-torrent", disabled=True)
                        yield Button("Продолжить", id="resume-torrent", disabled=True)
                        yield Button("Убрать (файлы оставить)", id="remove-torrent", disabled=True)
                    yield Button("Отменить и удалить файлы", id="delete-torrent", disabled=True, variant="error")
                    yield Static("Очередь не сохраняется после закрытия; скачанные файлы остаются на диске.",
                                 id="torrent-download-status", markup=False)
            with TabPane("3  Источники", id="sources-tab"):
                with VerticalScroll(id="sources-scroll"):
                    yield Label("ИСТОЧНИКИ ДАННЫХ", classes="eyebrow")
                    yield Static("Добавьте HTTPS-ссылку на JSON источника Hydra. Запросы каталога — только по нажатию «Найти».", classes="section-intro")
                    with Horizontal(id="add-source-row"):
                        yield Input(placeholder="https://…/source.json", id="source-url")
                        yield Button("Добавить источник", id="add-source", variant="success")
                    yield Static("URL отправится серверу Hydra только после нажатия «Добавить».",
                                 id="source-status", markup=False)
                    yield Label("ПОДКЛЮЧЁННЫЕ", classes="eyebrow")
                    yield Static(id="connected-sources", markup=False)
                    yield DataTable(id="source-table", cursor_type="row", zebra_stripes=True)
                    yield Button("Открыть игры выбранного источника →", id="open-source-games", disabled=True)
                    yield Static("NXCT загружает magnet. Ссылки на HTTPS-файлообменники пока не поддерживаются.",
                                 classes="info-note")
            with TabPane("4  Настройки", id="settings-tab"):
                with VerticalScroll(id="settings-scroll"):
                    yield Label("ПАПКИ", classes="eyebrow")
                    yield Static("Выберите существующие папки. Новые каталоги игр автоматически не создаются.", classes="section-intro")
                    yield Label("Папка загрузок")
                    with Horizontal(classes="path-row"):
                        yield Input(self.settings.downloads_dir, id="downloads-dir")
                        yield Button("Обзор…", id="browse-downloads")
                    yield Static(id="downloads-dir-info", classes="path-info", markup=False)
                    yield Label("Папка установки игр")
                    with Horizontal(classes="path-row"):
                        yield Input(self.settings.games_dir, id="games-dir")
                        yield Button("Обзор…", id="browse-games")
                    yield Static(id="games-dir-info", classes="path-info", markup=False)
                    yield Static("Установка пока не реализована: путь сохраняется только как настройка.", classes="section-intro")
                    yield Label("ИНТЕРФЕЙС", classes="eyebrow")
                    with Horizontal(classes="setting-row"):
                        yield Label("Цветовая тема", classes="setting-label")
                        yield Select(THEME_OPTIONS, value=self.settings.theme, allow_blank=False, id="theme-select")
                    with Horizontal(classes="setting-row"):
                        yield Label("Анимации интерфейса", classes="setting-label")
                        yield Switch(value=self.settings.startup_animation, id="startup-animation")
                    yield Button("Сохранить настройки", id="save-settings", variant="success")
                    yield Static(id="settings-status", markup=False)
                    yield Label("ОБНОВЛЕНИЯ", classes="eyebrow")
                    yield Static(f"Установлена версия NXCT {APP_VERSION}. Проверка — только по нажатию.",
                                 classes="section-intro")
                    yield Button("Проверить обновления", id="check-updates")
                    with Horizontal(id="update-status-row"):
                        yield Static("", id="update-status", markup=False)
                        yield LoadingIndicator(id="update-loading")
                    yield Button("Установить обновление", id="install-update", variant="success", disabled=True)
                    yield Label("КОНФИДЕНЦИАЛЬНОСТЬ", classes="eyebrow")
                    yield Static("Подключённые источники загружаются по нажатию с hydra-api-us-east-1.losbroxas.org; сервер видит ваш IP и запрос. Торрент раскрывает ваш IP участникам сети.\nДобавленные URL хранятся в настройках. Очередь не сохраняется; после выхода файлы остаются на диске.", classes="section-intro")
                    yield Static(f"Файл настроек: {SETTINGS_PATH}", id="settings-note", markup=False)
        yield Static("NXCT  /  Каталог по запросу  /  Торренты управляются внутри приложения", id="status-strip")
        yield Footer()

    def on_mount(self) -> None:
        self.screen.set_class(self.size.width < 95, "narrow")
        self.query_one("#market-nav").display = False
        self.query_one("#library-games", DataTable).add_columns("Игра", "Время", "Состояние")
        self.refresh_library()
        self.set_interval(2, self.poll_games)
        if self.library_warning:
            self.query_one("#library-status", Static).update(self.library_warning)
        self.query_one("#games", DataTable).add_columns("Игра", "Жанр", "Источник")
        self.query_one("#source-table", DataTable).add_columns("Источник", "Адрес", "ID")
        self.query_one("#torrent-downloads", DataTable).add_columns("Игра", "Прогресс", "Скорость", "Состояние")
        self.set_interval(2, self.poll_torrents)
        self.refresh_torrent_downloads()
        self.refresh_sources()
        self.clear_search()
        self.query_one("#choose-library-folder", Button).focus()
        self.update_path_info()
        if self.settings_warning:
            self.query_one("#settings-status", Static).update(self.settings_warning)
        if self.settings.startup_animation:
            self.call_after_refresh(self.push_screen, StartupScreen())

    def update_path_info(self) -> None:
        for field in ("downloads-dir", "games-dir"):
            _, info = inspect_directory(self.query_one(f"#{field}", Input).value)
            self.query_one(f"#{field}-info", Static).update(info)

    @on(Input.Changed, "#downloads-dir")
    @on(Input.Changed, "#games-dir")
    def settings_path_changed(self) -> None:
        self.update_path_info()

    @on(Button.Pressed, "#browse-downloads")
    @on(Button.Pressed, "#browse-games")
    def browse_settings_folder(self, event: Button.Pressed) -> None:
        field = "downloads-dir" if event.button.id == "browse-downloads" else "games-dir"
        widget = self.query_one(f"#{field}", Input)

        def selected(path: Path | None) -> None:
            if path is not None:
                widget.value = str(path)

        self.push_screen(FolderPicker("ВЫБОР ПАПКИ / настройки", widget.value), selected)

    @on(Button.Pressed, "#save-settings")
    def save_settings(self) -> None:
        status = self.query_one("#settings-status", Static)
        paths = []
        for field in ("downloads-dir", "games-dir"):
            value = self.query_one(f"#{field}", Input).value
            if field == "downloads-dir" and not value.strip():
                paths.append("")
                continue
            path, info = inspect_directory(value)
            if path is None:
                status.update(f"Настройки не сохранены: {info}")
                return
            paths.append(str(path))
        theme = self.query_one("#theme-select", Select).value
        if not isinstance(theme, str) or theme not in THEME_PALETTES:
            status.update("Выберите тему оформления.")
            return
        settings = Settings(*paths, self.query_one("#startup-animation", Switch).value,
                            theme, list(self.settings.sources))
        try:
            write_settings(settings)
        except OSError as error:
            status.update(f"Не удалось сохранить настройки: {error}")
            return
        self.settings = settings
        status.update("Настройки сохранены. Анимации применяются при следующем действии или запуске.")

    def on_resize(self, event: events.Resize) -> None:
        self.screen.set_class(event.size.width < 95, "narrow")

    async def on_unmount(self) -> None:
        self.torrent_closing = True
        await asyncio.gather(*(self.stop_rqbit_process(entry) for entry in self.torrent_downloads),
                             return_exceptions=True)
        if self.running_games:
            now = monotonic()
            for game in self.library_games:
                running = self.running_games.get(game.location)
                if running:
                    game.play_seconds += max(0, now - running[1])
            self.running_games.clear()
            self.save_library()

    def save_library(self) -> bool:
        try:
            LIBRARY_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            now = monotonic()
            games = []
            for game in self.library_games:
                record = asdict(game)
                running = self.running_games.get(game.location)
                if running:
                    record["play_seconds"] += max(0, now - running[1])
                games.append(record)
            content = json.dumps({"scan_dir": self.library_root, "games": games}, ensure_ascii=False, indent=2) + "\n"
            fd = os.open(LIBRARY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
            return True
        except OSError as error:
            self.query_one("#library-status", Static).update(f"Библиотека не сохранена: {error}")
            return False

    @staticmethod
    def format_playtime(seconds: float) -> str:
        total = int(seconds)
        return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"

    def selected_library_game(self) -> InstalledGame | None:
        table = self.query_one("#library-games", DataTable)
        row = table.cursor_row
        return self.library_games[row] if 0 <= row < len(self.library_games) else None

    def refresh_library(self) -> None:
        table = self.query_one("#library-games", DataTable)
        row = table.cursor_row
        table.clear()
        now = monotonic()
        for index, game in enumerate(self.library_games):
            running = self.running_games.get(game.location)
            elapsed = max(0, now - running[1]) if running else 0
            table.add_row(game.title, self.format_playtime(game.play_seconds + elapsed),
                          "Запущена" if running else "Готова", key=str(index))
        if self.library_games:
            table.move_cursor(row=min(max(0, row), len(self.library_games) - 1))
        self.query_one("#library-root", Static).update(
            f"Папка сканирования: {self.library_root}" if self.library_root else "Папка ещё не выбрана.")
        self.query_one("#library-empty").display = not self.library_games
        self.query_one("#rescan-library", Button).disabled = self.library_root is None
        self.show_library_game()

    def show_library_game(self) -> None:
        game = self.selected_library_game()
        executable = self.query_one("#library-executable", Input)
        executable.disabled = game is None
        executable.value = game.executable if game else ""
        self.query_one("#save-library-path", Button).disabled = game is None
        self.query_one("#remove-library-game", Button).disabled = game is None or game.location in self.running_games
        self.query_one("#launch-library-game", Button).disabled = game is None or game.location in self.running_games

    @on(DataTable.RowHighlighted, "#library-games")
    def library_highlighted(self) -> None:
        self.show_library_game()

    @on(Button.Pressed, "#choose-library-folder")
    def choose_library_folder(self) -> None:
        def selected(path: Path | None) -> None:
            if path is not None:
                self.run_worker(self.scan_library(path), exclusive=True)

        self.push_screen(FolderPicker("ПАПКА С ИГРАМИ / Steam исключён", self.library_root or str(Path.home()),
                                      scan_folder=True), selected)

    @on(Button.Pressed, "#rescan-library")
    def rescan_library(self) -> None:
        if self.library_root:
            path, info = inspect_scan_directory(self.library_root)
            if path is None:
                self.query_one("#library-status", Static).update(info)
            else:
                self.run_worker(self.scan_library(path), exclusive=True)

    async def scan_library(self, root: Path) -> None:
        status = self.query_one("#library-status", Static)
        status.update("Сканирую выбранную папку…")
        found, incomplete = await asyncio.to_thread(discover_games, root)
        known_locations = {game.location for game in self.library_games}
        known_paths = {game.executable for game in self.library_games}
        added = 0
        for game in found:
            if game.location not in known_locations and game.executable not in known_paths:
                self.library_games.append(game)
                known_locations.add(game.location)
                known_paths.add(game.executable)
                added += 1
        self.library_root = str(root)
        self.refresh_library()
        if self.save_library():
            status.update(f"Найдено: {len(found)} · добавлено: {added}."
                          + (" Сканирование ограничено: часть файлов не проверена." if incomplete else ""))

    @on(Button.Pressed, "#save-library-path")
    def save_library_path(self) -> None:
        game = self.selected_library_game()
        if game is None or game.location in self.running_games:
            return
        path, error = inspect_executable(self.query_one("#library-executable", Input).value)
        status = self.query_one("#library-status", Static)
        if path is None:
            status.update(error)
            return
        game.executable = str(path)
        if self.save_library():
            status.update(f"Путь запуска сохранён: {game.title}")

    @on(Button.Pressed, "#remove-library-game")
    def remove_library_game(self) -> None:
        game = self.selected_library_game()
        if game is None or game.location in self.running_games:
            return
        self.library_games.remove(game)
        self.refresh_library()
        if self.save_library():
            self.query_one("#library-status", Static).update("Игра убрана из библиотеки; файлы игры не удалены.")

    @on(Button.Pressed, "#launch-library-game")
    def launch_library_game(self) -> None:
        game = self.selected_library_game()
        if game is None or game.location in self.running_games:
            return
        path, error = inspect_executable(game.executable)
        status = self.query_one("#library-status", Static)
        if path is None:
            status.update(error)
            return
        command = [str(path)]
        if path.suffix.casefold() == ".exe":
            wine = shutil.which("wine")
            if wine is None:
                status.update("Wine не найден. Установите Wine для запуска Windows EXE.")
                return
            command = [wine, str(path)]
        try:
            process = subprocess.Popen(command, cwd=str(path.parent), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            status.update(f"Не удалось запустить игру: {exc}")
            return
        self.running_games[game.location] = (process, monotonic())
        status.update(f"Запущена: {game.title}. Учитывается время работы процесса.")
        self.refresh_library()

    def poll_games(self) -> None:
        completed = []
        for location, (process, started) in self.running_games.items():
            if process.poll() is not None:
                for game in self.library_games:
                    if game.location == location:
                        game.play_seconds += max(0, monotonic() - started)
                        break
                completed.append(location)
        for location in completed:
            del self.running_games[location]
        if completed:
            self.save_library()
        if self.running_games or completed:
            self.refresh_library()

    @on(Select.Changed, "#theme-select")
    def theme_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str) and event.value in THEME_PALETTES:
            self.theme = event.value

    @on(Button.Pressed, "#check-updates")
    def request_update_check(self) -> None:
        self.run_worker(self.check_updates(), group="release-check", exclusive=True)

    async def check_updates(self) -> None:
        button = self.query_one("#check-updates", Button)
        status = self.query_one("#update-status", Static)
        install = self.query_one("#install-update", Button)
        loading = self.query_one("#update-loading", LoadingIndicator)
        button.disabled = True
        install.disabled = True
        self.available_update = None
        loading.display = self.settings.startup_animation
        status.update("Проверяю релизы NXCT на GitHub…")
        try:
            version = await asyncio.to_thread(latest_release_version)
            if version is None:
                status.update("Публичных релизов пока нет.")
            elif version_number(version) > version_number(APP_VERSION):
                if bundled_installer() is None:
                    status.update(f"Доступна версия {version}. Обновление из интерфейса работает после установки NXCT.")
                else:
                    self.available_update = version
                    install.disabled = False
                    status.update(f"Доступна версия {version}. Нажмите «Установить обновление».")
            else:
                status.update(f"Установлена актуальная версия {APP_VERSION}.")
        except (HTTPError, URLError, OSError, ValueError, TimeoutError) as error:
            status.update(f"Не удалось проверить обновления: {error}")
        finally:
            loading.display = False
            button.disabled = False

    @on(Button.Pressed, "#install-update")
    def request_install_update(self) -> None:
        version = self.available_update
        if version is None or bundled_installer() is None:
            return
        if any(entry.state in ("Подключение", "Загрузка", "Пауза", "Останавливаю") or
               entry.process is not None for entry in self.torrent_downloads):
            self.query_one("#update-status", Static).update(
                "Сначала завершите или уберите активные загрузки. Файлы можно оставить на диске.")
            return

        def confirmed(approved: bool) -> None:
            if approved and self.available_update == version:
                if any(entry.process is not None or entry.state in ("Подключение", "Загрузка", "Пауза", "Останавливаю")
                       for entry in self.torrent_downloads):
                    self.query_one("#update-status", Static).update("Загрузка ещё активна; обновление отменено.")
                    return
                self.pending_update = version
                self.exit()

        self.push_screen(UpdateConfirm(version), confirmed)

    def clear_search(self) -> None:
        self.clear_game_cover()
        self.remote_games = []
        self.remote_game = None
        self.remote_page = self.remote_pages = 1
        self.remote_source_ids = ()
        self.query_one("#games", DataTable).clear()
        self.query_one("#choose-release", Button).disabled = True
        self.query_one("#game-title", Static).update("Игры подключённых источников")
        self.query_one("#game-meta", Static).update("")
        self.query_one("#game-description", Static).update("Выберите источник и нажмите «Найти».")
        self.query_one("#game-source", Static).update("")
        self.query_one("#remote-prev", Button).disabled = True
        self.query_one("#remote-next", Button).disabled = True
        self.query_one("#remote-page", Static).update("")
        self.query_one("#result-status", Static).update(
            "Нажмите «Найти», чтобы загрузить игры." if self.settings.sources
            else "Нет источников. Добавьте источник во вкладке «Источники».")

    def refresh_sources(self) -> None:
        sources = self.settings.sources
        table = self.query_one("#source-table", DataTable)
        row = table.cursor_row
        table.clear()
        for source in sources:
            table.add_row(source.name, urlsplit(source.url).hostname or source.url, source.id,
                          key=source.id)
        if sources:
            table.move_cursor(row=min(max(row, 0), len(sources) - 1))
        self.query_one("#open-source-games", Button).disabled = not sources
        self.query_one("#connected-sources", Static).update(
            f"Всего подключено: {len(sources)}" if sources else "Подключённых источников пока нет.")

    @on(DataTable.RowHighlighted, "#source-table")
    def source_highlighted(self) -> None:
        table = self.query_one("#source-table", DataTable)
        self.query_one("#open-source-games", Button).disabled = not (0 <= table.cursor_row < len(self.settings.sources))

    @on(DataTable.RowSelected, "#source-table")
    @on(Button.Pressed, "#open-source-games")
    def open_source_games(self) -> None:
        row = self.query_one("#source-table", DataTable).cursor_row
        if 0 <= row < len(self.settings.sources):
            self.query_one("#source-filter", Select).value = self.settings.sources[row].id
            self.query_one("#search-input", Input).value = ""
            self.call_after_refresh(self.open_source_search)

    def open_source_search(self) -> None:
        self.action_tab("search-tab")
        self.query_one("#search-input", Input).focus()
        self.call_after_refresh(self.search_remote)

    @on(Input.Submitted, "#source-url")
    @on(Button.Pressed, "#add-source")
    def request_add_source(self) -> None:
        url = self.query_one("#source-url", Input).value.strip()
        status = self.query_one("#source-status", Static)
        if any(source.url == url for source in self.settings.sources):
            status.update("Этот URL уже добавлен.")
            return
        self.run_worker(self.add_source(url), group="register-source", exclusive=True)

    async def add_source(self, url: str) -> None:
        status = self.query_one("#source-status", Static)
        button = self.query_one("#add-source", Button)
        button.disabled = True
        status.update("Регистрирую источник через API Hydra…")
        try:
            source = await asyncio.to_thread(register_source, url)
            if any(existing.id == source.id for existing in self.settings.sources):
                status.update("Этот источник уже подключён.")
                return
            if len(self.settings.sources) >= 30:
                raise ValueError("Максимум 30 источников.")
            updated = replace(self.settings, sources=[*self.settings.sources, source])
            await asyncio.to_thread(write_settings, updated)
            self.settings = updated
            self.refresh_sources()
            status.update(f"Добавлен: {source.name}. Открываю игры источника в поиске.")
            self.query_one("#source-url", Input).value = ""
            self.query_one("#source-filter", Select).set_options(
                [("Все источники", "all"),
                 *((item.name, item.id) for item in self.settings.sources)])
            self.query_one("#source-filter", Select).value = source.id
            self.query_one("#search-input", Input).value = ""
            self.call_after_refresh(self.open_source_search)
        except (HTTPError, URLError, OSError, ValueError, TimeoutError) as error:
            status.update(f"Не удалось добавить источник: {error}")
        finally:
            button.disabled = False

    @on(Select.Changed, "#source-filter")
    def source_filter_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str) and event.value != self.remote_filter:
            self.remote_filter = event.value
            self.clear_search()

    @on(Button.Pressed, "#search-submit")
    def search_button(self) -> None:
        self.search_remote()

    def search_remote(self) -> None:
        selected = self.query_one("#source-filter", Select).value
        sources = [source for source in self.settings.sources
                   if selected == "all" or source.id == selected]
        if not sources:
            self.query_one("#result-status", Static).update("Сначала добавьте источник во вкладке «Источники».")
            return
        query = self.query_one("#search-input", Input).value.strip()
        self.run_worker(self.load_remote_page(1, query, sources, selected),
                        group="remote-search", exclusive=True)

    @on(Button.Pressed, "#remote-prev")
    def previous_remote_page(self) -> None:
        if self.remote_page > 1:
            self.load_selected_page(self.remote_page - 1)

    @on(Button.Pressed, "#remote-next")
    def next_remote_page(self) -> None:
        if self.remote_page < self.remote_pages:
            self.load_selected_page(self.remote_page + 1)

    def load_selected_page(self, page: int) -> None:
        sources = [source for source in self.settings.sources if source.id in self.remote_source_ids]
        self.run_worker(self.load_remote_page(page, self.remote_query, sources, self.remote_filter),
                        group="remote-search", exclusive=True)

    async def load_remote_page(self, page: int, query: str,
                               sources: list[DownloadSource], selected: str) -> None:
        status = self.query_one("#result-status", Static)
        loading = self.query_one("#search-loading", LoadingIndicator)
        self.search_loading_request += 1
        request = self.search_loading_request
        loading.display = self.settings.startup_animation
        status.update("Ищу игры в подключённых источниках…")
        try:
            games, total, pages = await asyncio.to_thread(fetch_source_page, page, query, sources)
        except (HTTPError, URLError, OSError, ValueError, TimeoutError) as error:
            if selected == self.query_one("#source-filter", Select).value:
                status.update(f"Поиск недоступен: {error}")
            return
        finally:
            if request == self.search_loading_request:
                loading.display = False
        if selected != self.query_one("#source-filter", Select).value:
            return
        self.clear_game_cover()
        table = self.query_one("#games", DataTable)
        table.clear()
        self.remote_games = games
        self.remote_game = None
        for index, game in enumerate(games):
            table.add_row(game.title, game.genre, game.source, key=f"r-{index}")
        self.remote_page, self.remote_pages, self.remote_query = page, pages, query
        self.remote_source_ids = tuple(source.id for source in sources)
        self.query_one("#remote-prev", Button).disabled = page <= 1
        self.query_one("#remote-next", Button).disabled = page >= pages
        self.query_one("#remote-page", Static).update(f"Страница {page} из {pages}")
        status.update(f"Найдено игр: {total} · источников: {len(sources)} · API Hydra")
        if games:
            table.move_cursor(row=0)
            self.show_remote_game(games[0])
        else:
            self.query_one("#choose-release", Button).disabled = True
            self.query_one("#game-title", Static).update("Ничего не найдено")
            self.query_one("#game-description", Static).update("Попробуйте другой запрос.")
            self.query_one("#game-meta", Static).update("")
            self.query_one("#game-source", Static).update("")

    def clear_game_cover(self) -> None:
        self.cover_request += 1
        cover = self.query_one("#game-cover", TerminalImage)
        cover.display = False
        cover.image = None

    async def load_game_cover(self, game: CatalogueGame, request: int) -> None:
        cover = self.query_one("#game-cover", TerminalImage)
        try:
            image = await asyncio.to_thread(fetch_game_cover, game.image_url)
        except (HTTPError, URLError, OSError, ValueError, TimeoutError):
            return
        if request != self.cover_request or self.remote_game is not game or not cover.is_attached:
            return
        cover.image = image
        cover.display = True

    def show_remote_game(self, game: CatalogueGame) -> None:
        previous = self.remote_game
        self.remote_game = game
        self.query_one("#choose-release", Button).disabled = False
        self.query_one("#game-title", Static).update(game.title)
        self.query_one("#game-meta", Static).update(f"{game.shop} · {game.genre}")
        self.query_one("#game-description", Static).update("Игра из подключённого источника. Выберите вариант перед скачиванием.")
        self.query_one("#game-source", Static).update(f"ИСТОЧНИК\n{game.source}")
        if previous is game:
            return
        self.clear_game_cover()
        if game.image_url:
            self.run_worker(self.load_game_cover(game, self.cover_request),
                            group="game-cover", exclusive=True)

    def action_preview_cover(self) -> None:
        if self.query_one("#tabs", TabbedContent).active != "search-tab":
            return
        if self.remote_game is None:
            self.query_one("#result-status", Static).update("Сначала выберите игру.")
            return
        image = self.query_one("#game-cover", TerminalImage).image
        if image is None:
            self.query_one("#result-status", Static).update("Обложка этой игры недоступна или ещё загружается.")
            return
        self.push_screen(GameCoverPreview(self.remote_game.title, image))

    async def load_variants(self, game: CatalogueGame, source_ids: tuple[str, ...]) -> None:
        status = self.query_one("#result-status", Static)
        status.update(f"Загружаю варианты: {game.title}…")
        try:
            variants = await asyncio.to_thread(fetch_variants, game, source_ids)
        except (HTTPError, URLError, OSError, ValueError, TimeoutError) as error:
            status.update(f"Не удалось загрузить варианты: {error}")
            return
        if not variants:
            status.update("Для этой игры нет доступных magnet-вариантов подключённых источников.")
            return
        status.update(f"{game.title} · magnet-вариантов: {len(variants)}")

        def selected(variant: TorrentVariant | None) -> None:
            if variant is None:
                return

            def destination_selected(path: Path | None) -> None:
                if path is None:
                    return
                if any(item.variant.info_hash == variant.info_hash for item in self.torrent_downloads):
                    status.update("Этот торрент уже добавлен в NXCT.")
                    return
                entry = TorrentDownload(game, variant, path / f"NXCT-{variant.info_hash}")
                self.torrent_downloads.append(entry)
                self.refresh_torrent_downloads()
                self.action_tab("downloads-tab")
                self.run_worker(self.start_torrent(entry), group="torrent-start", exclusive=False)

            self.call_after_refresh(self.push_screen,
                                    FolderPicker("КУДА СКАЧАТЬ / файлы сохранятся на диск",
                                                 self.settings.downloads_dir), destination_selected)

        self.push_screen(TorrentPicker(game, variants), selected)

    def refresh_torrent_downloads(self) -> None:
        table = self.query_one("#torrent-downloads", DataTable)
        row = table.cursor_row
        table.clear()
        for index, item in enumerate(self.torrent_downloads):
            table.add_row(item.game.title, f"{item.progress:.1f}%",
                          f"{item.speed:.2f} МБ/с" if item.state == "Загрузка" else "—",
                          item.state, key=str(index))
        if self.torrent_downloads:
            table.move_cursor(row=min(max(row, 0), len(self.torrent_downloads) - 1))
        self.update_torrent_buttons()

    def selected_torrent(self) -> TorrentDownload | None:
        row = self.query_one("#torrent-downloads", DataTable).cursor_row
        return self.torrent_downloads[row] if 0 <= row < len(self.torrent_downloads) else None

    def update_torrent_buttons(self) -> None:
        entry = self.selected_torrent()
        self.query_one("#pause-torrent", Button).disabled = entry is None or entry.state != "Загрузка"
        self.query_one("#resume-torrent", Button).disabled = entry is None or entry.state not in ("Пауза", "Ошибка")
        self.query_one("#remove-torrent", Button).disabled = entry is None or entry.state in ("Подключение", "Останавливаю")
        self.query_one("#delete-torrent", Button).disabled = (entry is None or not entry.started
                                                              or entry.state in ("Подключение", "Останавливаю"))
        if entry is None:
            detail = "Очередь пуста. Найдите игру, выберите вариант и папку для загрузки."
        else:
            total = format_data_size(entry.total_bytes) if entry.total_bytes else "—"
            speed = f"{entry.speed:.2f} МБ/с" if entry.state == "Загрузка" else "—"
            eta = format_eta(entry.eta_seconds) if entry.state == "Загрузка" else "—"
            if entry.state == "Загрузка":
                if entry.connected_peers is None:
                    connection = "Поиск пиров"
                elif entry.connected_peers:
                    connection = f"Пиров: {entry.connected_peers}"
                    if entry.connecting_peers:
                        connection += f", подключаются: {entry.connecting_peers}"
                elif entry.connecting_peers:
                    connection = f"Подключаются: {entry.connecting_peers}"
                else:
                    connection = "Нет подключённых пиров"
            else:
                connection = entry.state
            detail = (f"{entry.variant.title}\n"
                      f"Скачано: {format_data_size(entry.downloaded_bytes)} / {total}  ·  "
                      f"Скорость: {speed}  ·  Осталось: {eta}\n"
                      f"Соединение: {connection}\nПапка: {entry.destination}")
        self.query_one("#download-detail", Static).update(detail)

    @on(DataTable.RowHighlighted, "#torrent-downloads")
    def torrent_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self.torrent_closing or not event.data_table.is_attached:
            return
        self.update_torrent_buttons()

    async def start_torrent(self, entry: TorrentDownload) -> None:
        entry.state = "Подключение"
        entry.speed = 0.0
        entry.eta_seconds = None
        entry.connected_peers = None
        entry.connecting_peers = None
        self.refresh_torrent_downloads()
        binary = bundled_rqbit()
        if binary is None:
            entry.state = "Ошибка"
            self.query_one("#torrent-download-status", Static).update(
                "Движок rqbit не установлен. Загрузка не начиналась; NXCT не использует qBittorrent.")
            self.refresh_torrent_downloads()
            return
        try:
            parent, message = inspect_directory(str(entry.destination.parent))
            if parent is None:
                raise ValueError(message)
            resume = entry.started
            if not resume:
                entry.destination.mkdir(mode=0o700, exist_ok=False)
                entry.started = True
            elif not entry.destination.is_dir():
                raise ValueError("Папка загрузки исчезла; продолжение отменено.")
            port, peer_port = local_port(), local_port()
            while peer_port == port:
                peer_port = local_port()
            entry.port = port
            entry.token = secrets.token_urlsafe(32)
            env = os.environ.copy()
            env["RQBIT_HTTP_BASIC_AUTH_USERPASS"] = f"nxct:{entry.token}"
            entry.process = subprocess.Popen([
                binary, "--disable-dht-persistence", "--disable-upnp-port-forward",
                "--disable-lsd", "--umask", "077", "--listen-port", str(peer_port),
                "--http-api-listen-addr", f"127.0.0.1:{port}",
                "server", "start", "--disable-persistence", str(entry.destination),
            ], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
            await asyncio.to_thread(wait_for_rqbit, entry)
            if self.torrent_closing:
                await self.stop_rqbit_process(entry)
                return
            result = await asyncio.to_thread(rqbit_api, entry,
                                             "/torrents?overwrite=true" if resume else "/torrents",
                                             post=entry.variant.magnet.encode("utf-8"), timeout=30)
            if not isinstance(result, dict) or type(result.get("id")) is not int:
                raise ValueError("неожиданный ответ rqbit при добавлении торрента")
            details = result.get("details")
            if (not isinstance(details, dict) or
                    details.get("info_hash", "").lower() != entry.variant.info_hash):
                raise ValueError("хеш торрента не совпадает с выбранным вариантом")
            entry.torrent_id = result["id"]
            if self.torrent_closing:
                await self.stop_rqbit_process(entry)
                return
            entry.state = "Загрузка"
            self.query_one("#torrent-download-status", Static).update(
                f"Загружается в {entry.destination}. Не запускайте полученные файлы без проверки.")
        except (OSError, RuntimeError, ValueError, TimeoutError) as error:
            entry.state = "Ошибка"
            self.query_one("#torrent-download-status", Static).update(f"Загрузка не началась: {error}")
            await self.stop_rqbit_process(entry)
        self.refresh_torrent_downloads()

    def poll_torrents(self) -> None:
        if any(entry.torrent_id is not None and entry.state == "Загрузка" for entry in self.torrent_downloads):
            self.run_worker(self.update_torrent_stats(), group="torrent-stats", exclusive=True)

    async def update_torrent_stats(self) -> None:
        for entry in self.torrent_downloads:
            if entry.torrent_id is None or entry.state != "Загрузка":
                continue
            try:
                if entry.process is None or entry.process.poll() is not None:
                    raise RuntimeError("процесс rqbit неожиданно завершился")
                stats = await asyncio.to_thread(rqbit_api, entry,
                                                f"/torrents/{entry.torrent_id}/stats/v1")
                if not isinstance(stats, dict):
                    raise ValueError("неверный формат статистики rqbit")
                if entry.state != "Загрузка":
                    continue
                total = int(stats["total_bytes"])
                done = int(stats["progress_bytes"])
                if total < 0 or done < 0:
                    raise ValueError("неверный объём загрузки")
                entry.total_bytes = total
                entry.downloaded_bytes = min(done, total) if total else done
                entry.progress = max(0.0, min(100.0, 100 * done / total)) if total > 0 else 0.0
                live = stats.get("live")
                speed = live.get("download_speed") if isinstance(live, dict) else None
                rate = float(speed.get("mbps", 0)) if isinstance(speed, dict) else 0.0
                entry.speed = max(0.0, rate) if isfinite(rate) else 0.0
                snapshot = live.get("snapshot") if isinstance(live, dict) else None
                peers = snapshot.get("peer_stats") if isinstance(snapshot, dict) else None
                connected = peers.get("live") if isinstance(peers, dict) else None
                connecting = peers.get("connecting") if isinstance(peers, dict) else None
                entry.connected_peers = connected if type(connected) is int and connected >= 0 else None
                entry.connecting_peers = connecting if type(connecting) is int and connecting >= 0 else None
                remaining = live.get("time_remaining") if isinstance(live, dict) else None
                duration = remaining.get("duration") if isinstance(remaining, dict) else None
                seconds = duration.get("secs") if isinstance(duration, dict) else None
                entry.eta_seconds = (seconds if entry.speed > 0 and type(seconds) is int
                                     and 0 <= seconds < 10 ** 9 else None)
                if entry.eta_seconds is None and entry.speed > 0 and total > done:
                    estimate = (total - done) / (entry.speed * 1_000_000)
                    entry.eta_seconds = ceil(estimate) if estimate < 10 ** 9 else None
                if stats.get("state") == "error":
                    raise RuntimeError(str(stats.get("error") or "ошибка движка"))
                if stats.get("finished") is True:
                    entry.progress = 100.0
                    await self.stop_rqbit_process(entry)
                    entry.state = "Готово"
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                entry.speed = 0.0
                entry.eta_seconds = None
                self.query_one("#torrent-download-status", Static).update(f"Ошибка проверки загрузки: {error}")
                await self.stop_rqbit_process(entry)
                entry.state = "Ошибка"
        self.refresh_torrent_downloads()

    @on(Button.Pressed, "#pause-torrent")
    def pause_torrent(self) -> None:
        entry = self.selected_torrent()
        if entry is not None and entry.state == "Загрузка":
            self.run_worker(self.control_torrent(entry, "pause", "Пауза"),
                            group="torrent-control", exclusive=False)

    async def control_torrent(self, entry: TorrentDownload, action: str, state: str) -> None:
        entry.state = "Подключение"
        self.refresh_torrent_downloads()
        try:
            if entry.torrent_id is None:
                raise ValueError("торрент не найден в движке")
            await asyncio.to_thread(rqbit_api, entry, f"/torrents/{entry.torrent_id}/{action}", post=b"")
            entry.state = state
            if state == "Пауза":
                entry.speed = 0.0
                entry.eta_seconds = None
                entry.connected_peers = None
                entry.connecting_peers = None
        except (OSError, RuntimeError, ValueError, TimeoutError) as error:
            entry.state = "Ошибка"
            self.query_one("#torrent-download-status", Static).update(f"Команда rqbit не выполнена: {error}")
            await self.stop_rqbit_process(entry)
        self.refresh_torrent_downloads()

    async def stop_rqbit_process(self, entry: TorrentDownload) -> None:
        process = entry.process
        entry.process = None
        entry.torrent_id = None
        entry.port = None
        entry.token = None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.to_thread(process.wait, timeout=6)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait, timeout=3)

    @on(Button.Pressed, "#resume-torrent")
    def resume_torrent(self) -> None:
        entry = self.selected_torrent()
        if entry is not None and entry.state in ("Пауза", "Ошибка"):
            if entry.process is not None and entry.process.poll() is None and entry.torrent_id is not None:
                self.run_worker(self.control_torrent(entry, "start", "Загрузка"),
                                group="torrent-control", exclusive=False)
            else:
                self.run_worker(self.start_torrent(entry), group="torrent-start", exclusive=False)

    @on(Button.Pressed, "#remove-torrent")
    def remove_torrent(self) -> None:
        entry = self.selected_torrent()
        if entry is not None and entry.state not in ("Подключение", "Останавливаю"):
            self.run_worker(self.remove_torrent_entry(entry), group="torrent-stop", exclusive=False)

    async def remove_torrent_entry(self, entry: TorrentDownload) -> None:
        entry.state = "Останавливаю"
        self.refresh_torrent_downloads()
        await self.stop_rqbit_process(entry)
        if entry in self.torrent_downloads:
            self.torrent_downloads.remove(entry)
        self.refresh_torrent_downloads()
        self.query_one("#torrent-download-status", Static).update("Запись убрана из очереди; файлы оставлены на диске.")

    @on(Button.Pressed, "#delete-torrent")
    def confirm_delete_torrent(self) -> None:
        entry = self.selected_torrent()
        if entry is None or not entry.started or entry.state in ("Подключение", "Останавливаю"):
            return

        def confirmed(approved: bool) -> None:
            if approved and entry in self.torrent_downloads:
                self.run_worker(self.delete_torrent_files(entry), group="torrent-delete", exclusive=False)

        self.push_screen(DeleteTorrentConfirm(entry.destination), confirmed)

    async def delete_torrent_files(self, entry: TorrentDownload) -> None:
        entry.state = "Останавливаю"
        self.refresh_torrent_downloads()
        try:
            await self.stop_rqbit_process(entry)
            folder = entry.destination
            if (not entry.started or folder.name != f"NXCT-{entry.variant.info_hash}"
                    or folder.is_symlink() or folder.resolve() != folder.absolute()
                    or not folder.is_dir()):
                raise ValueError("папка загрузки изменилась; удаление отменено")
            await asyncio.to_thread(shutil.rmtree, folder)
        except (OSError, ValueError) as error:
            entry.state = "Ошибка"
            self.query_one("#torrent-download-status", Static).update(f"Папка не удалена полностью: {error}")
        else:
            if entry in self.torrent_downloads:
                self.torrent_downloads.remove(entry)
            self.query_one("#torrent-download-status", Static).update(f"Загрузка отменена; удалена папка {folder}")
        self.refresh_torrent_downloads()

    @on(Button.Pressed, ".nav-button")
    def navigate(self, event: Button.Pressed) -> None:
        section = (event.button.id or "").removeprefix("open-")
        self.action_tab("search-tab" if section == "market" else f"{section}-tab")

    @on(TabbedContent.TabActivated, "#tabs")
    def section_activated(self) -> None:
        pane = self.query_one("#tabs", TabbedContent).active
        if self.settings.startup_animation:
            content = self.query_one(f"#{pane}", TabPane)
            content.styles.opacity = 0.35
            content.styles.animate("opacity", 1.0, duration=0.18, easing="out_cubic")
        is_library = pane == "library-tab"
        self.query_one("#market-nav").display = not is_library
        for button in self.query(".nav-button"):
            section = (button.id or "").removeprefix("open-")
            button.set_class((is_library if section == "library" else
                              not is_library if section == "market" else
                              pane == f"{section}-tab"), "active")

    @on(Button.Pressed, "#choose-release")
    def choose_release(self) -> None:
        if self.remote_game is not None and self.remote_source_ids:
            self.run_worker(self.load_variants(self.remote_game, self.remote_source_ids),
                            group="remote-variants", exclusive=True)

    @on(Input.Changed, "#search-input")
    def search_changed(self) -> None:
        self.query_one("#result-status", Static).update("Нажмите «Найти», чтобы обновить результаты.")

    @on(Input.Submitted, "#search-input")
    def search_submitted(self) -> None:
        self.search_remote()

    @on(DataTable.RowHighlighted, "#games")
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is not None and event.row_key.value.startswith("r-"):
            row = int(event.row_key.value[2:])
            if 0 <= row < len(self.remote_games):
                self.show_remote_game(self.remote_games[row])

    def action_search(self) -> None:
        self.action_tab("search-tab")
        self.query_one("#search-input", Input).focus()

    def action_tab(self, pane: str) -> None:
        self.query_one("#tabs", TabbedContent).active = pane

    def action_focus_results(self) -> None:
        if self.query_one("#tabs", TabbedContent).active == "library-tab":
            self.query_one("#library-games", DataTable).focus()
        else:
            self.action_tab("search-tab")
            self.query_one("#games", DataTable).focus()


if __name__ == "__main__":
    app = CatalogueApp()
    app.run()
    if app.pending_update is not None:
        installer = bundled_installer()
        if installer is None:
            print("NXCT: установщик не найден; обновление отменено.", file=sys.stderr)
        else:
            environment = installer_environment()
            result = subprocess.run(["sh", str(installer), "--version", f"v{app.pending_update}"],
                                    env=environment, check=False)
            if result.returncode == 0:
                launcher = Path.home() / ".local" / "bin" / "nxct"
                try:
                    os.execve(str(launcher), [str(launcher)], environment)
                except OSError as error:
                    print(f"NXCT обновлён. Запустите {launcher} вручную: {error}", file=sys.stderr)
            else:
                print("NXCT: обновление не удалось. Старая версия сохранена.", file=sys.stderr)

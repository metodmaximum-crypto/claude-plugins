"""Общие помощники: HTTP, разбор HTML, карта сайта. Только stdlib."""

import gzip
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Только ASCII: urllib кодирует заголовки в latin-1 и падает на кириллице.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 ReconBot/1.0")
TIMEOUT = 20


def fetch(url, method="GET"):
    """Возвращает dict: url, status, final_url, headers, body, error.

    Никогда не бросает исключение — упавший запрос это тоже находка аудита.
    """
    out = {"url": url, "status": None, "final_url": url, "headers": {},
           "body": "", "error": None}
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            out["status"] = resp.status
            out["final_url"] = resp.geturl()
            out["headers"] = dict(resp.headers)
            charset = resp.headers.get_content_charset() or "utf-8"
            if method == "GET":
                out["body"] = raw.decode(charset, "replace")
    except urllib.error.HTTPError as e:
        out["status"] = e.code
        out["headers"] = dict(e.headers or {})
    except Exception as e:  # таймаут, DNS, TLS — всё это находки
        out["error"] = f"{type(e).__name__}: {e}"
    return out


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta_description = ""
        self.h1 = []
        self.links = []          # (href, текст, в каком блоке)
        self.scripts = []
        self.has_viewport = False
        self.canonical = ""
        self._stack = []
        self._grab = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self._stack.append(tag)
        if tag in ("nav", "header", "footer"):
            self._stack.append(f"@{tag}")
        if tag == "title" and not self.title:
            # Берём только ПЕРВЫЙ <title>. На страницах встречаются вторые:
            # внутри inline-SVG и внутри целых HTML-документов, вставленных
            # в блоки конструктора. Без этого условия заголовки склеивались
            # в одну строку и выдавались за поломку title.
            self._grab = "title"
        elif tag == "h1":
            self._grab = "h1"
            self.h1.append("")
        elif tag == "meta":
            if a.get("name", "").lower() == "description":
                self.meta_description = a.get("content", "")
            if a.get("name", "").lower() == "viewport":
                self.has_viewport = True
        elif tag == "link" and "canonical" in a.get("rel", ""):
            self.canonical = a.get("href", "")
        elif tag == "script" and a.get("src"):
            self.scripts.append(a["src"])
        elif tag == "a" and a.get("href"):
            zone = "footer" if "@footer" in self._stack else (
                "nav" if ("@nav" in self._stack or "@header" in self._stack) else "body")
            self.links.append([a["href"], "", zone])
            self._grab = "a"

    def handle_endtag(self, tag):
        if tag in ("title", "h1", "a"):
            self._grab = None
        if tag in ("nav", "header", "footer") and f"@{tag}" in self._stack:
            self._stack.remove(f"@{tag}")
        if tag in self._stack:
            self._stack.reverse()
            self._stack.remove(tag)
            self._stack.reverse()

    def handle_data(self, data):
        if self._grab == "title":
            self.title += data.strip()
        elif self._grab == "h1" and self.h1:
            self.h1[-1] += data.strip()
        elif self._grab == "a" and self.links:
            # Текст ссылки нужен не меньше адреса: юрстраницы часто живут по
            # адресам вида /include/licenses_detail.php, где по URL не опознать.
            self.links[-1][1] = (self.links[-1][1] + " " + data.strip()).strip()[:120]


def parse_page(html):
    p = _Page()
    try:
        p.feed(html)
    except Exception:
        pass  # битая разметка — тоже находка, но разбор не роняем
    return p


def same_host(url, base):
    a, b = urllib.parse.urlparse(url).netloc, urllib.parse.urlparse(base).netloc
    return a.replace("www.", "") == b.replace("www.", "")


def normalize(href, base):
    """Абсолютный URL без якоря и utm-хвостов; None для нессылок.

    Слеш в конце СОХРАНЯЕТСЯ: Bitrix и ряд других CMS отдают 404 без него,
    и срезание слеша даёт лавину несуществующих «битых ссылок».
    """
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    url = urllib.parse.urljoin(base, href)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    query = "&".join(
        q for q in parts.query.split("&")
        if q and not q.lower().startswith(("utm_", "yclid", "gclid", "from="))
    )
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path or "/", query, "")
    )


def dedup_key(url):
    """Ключ для склейки дублей: без слеша в конце и без www. Не для запросов."""
    parts = urllib.parse.urlsplit(url)
    return (parts.netloc.replace("www.", "")
            + (parts.path.rstrip("/") or "/")
            + ("?" + parts.query if parts.query else ""))


def read_robots(base):
    r = fetch(urllib.parse.urljoin(base, "/robots.txt"))
    found = r["status"] == 200 and "user-agent" in r["body"].lower()
    sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", r["body"]) if found else []
    disallow = re.findall(r"(?im)^\s*disallow:\s*(\S*)", r["body"]) if found else []
    return {"exists": found, "status": r["status"],
            "sitemaps": sitemaps, "disallow_rules": len(disallow)}


def read_sitemap(url, depth=0, seen=None):
    """Разворачивает индексные карты. Возвращает список URL."""
    seen = seen if seen is not None else set()
    if depth > 2 or url in seen or len(seen) > 40:
        return []
    seen.add(url)
    r = fetch(url)
    if r["status"] != 200:
        return []
    body = r["body"]
    urls = []
    if "<sitemapindex" in body:
        for child in re.findall(r"<loc>\s*(.*?)\s*</loc>", body, re.I | re.S)[:40]:
            urls += read_sitemap(child.strip(), depth + 1, seen)
    else:
        urls = [u.strip() for u in re.findall(r"<loc>\s*(.*?)\s*</loc>", body, re.I | re.S)]
    return urls


def sections(urls, base=None):
    """Группирует URL по первому сегменту пути: раздел -> сколько страниц."""
    buckets = {}
    for u in urls:
        path = urllib.parse.urlparse(u).path.strip("/")
        key = path.split("/")[0] if path else "(главная)"
        buckets[key] = buckets.get(key, 0) + 1
    return dict(sorted(buckets.items(), key=lambda kv: -kv[1]))


# --- Ключи -----------------------------------------------------------------

ENV_KEYS = ("YC_API_KEY", "YC_FOLDER_ID", "YANDEX_OAUTH_TOKEN",
            "GOOGLE_CSE_KEY", "GOOGLE_CSE_CX", "PAGESPEED_KEY")


def find_env():
    """Где лежат ключи: $RECON_SITE_ENV → ~/.recon-site/.env → .env рядом со скиллом.

    Основное место — ~/.recon-site/.env. При установке плагином папка скилла
    заменяется на каждом обновлении, и ключи рядом со скриптами бы пропадали.
    """
    candidates = []
    if os.environ.get("RECON_SITE_ENV"):
        candidates.append(Path(os.environ["RECON_SITE_ENV"]).expanduser())
    candidates.append(Path.home() / ".recon-site" / ".env")
    candidates.append(Path(__file__).resolve().parent.parent / ".env")
    return next((p for p in candidates if p.is_file()), None)


def read_env():
    """(ключи, путь к файлу). Переменные окружения с теми же именами приоритетнее файла."""
    env, path = {}, find_env()
    if path:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ENV_KEYS:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env, path

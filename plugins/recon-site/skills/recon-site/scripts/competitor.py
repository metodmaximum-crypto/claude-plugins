"""Шаг 5 регламента: разбор структуры сайта-лидера выдачи.

Запуск:  python scripts/competitor.py https://konkurent.ru [--out файл.json]

Выписывает разделы и страницы по меню, подвалу и карте сайта — то есть
ровно те три источника, которые называет регламент. Решение «берём / не
берём / у них лишнее» принимает человек, скрипт только достаёт факты.
"""

import argparse
import json
import sys
import urllib.parse
from datetime import date

from lib import (dedup_key, fetch, normalize, parse_page, read_robots,
                 read_sitemap, same_host, sections)

MAX_TITLES = 25  # сколько страниц открыть ради заголовка


def looks_geo_landing(urls):
    """Признак федеральной сети городских посадочных (/khabarovsk/, /spb/ ...).

    Регламент требует отличать настоящего локального конкурента от
    федерального сайта с городской посадочной — это разные выводы для КП.
    """
    cities = ("khabarovsk", "habarovsk", "moskva", "msk", "spb", "piter",
              "novosibirsk", "ekaterinburg", "krasnodar", "vladivostok",
              "kazan", "samara", "rostov", "perm", "ufa", "omsk")
    hits = set()
    for u in urls:
        first = urllib.parse.urlparse(u).path.strip("/").split("/")[0].lower()
        if first in cities:
            hits.add(first)
    return sorted(hits)


def analyze(base):
    out = {"сайт": base, "дата_замера": date.today().isoformat()}

    home = fetch(base)
    if home["error"] or home["status"] != 200:
        out["ошибка"] = home["error"] or f"код ответа {home['status']}"
        return out

    page = parse_page(home["body"])
    out["главная"] = {"title": page.title or None, "h1": page.h1 or None}

    # --- Меню и подвал -----------------------------------------------------
    menu, footer, other = {}, {}, {}
    titles_by_url = {}
    for href, text, zone in page.links:
        url = normalize(href, home["final_url"])
        if not url or not same_host(url, home["final_url"]):
            continue
        target = menu if zone == "nav" else (footer if zone == "footer" else other)
        target.setdefault(dedup_key(url), url)
        if text:
            titles_by_url.setdefault(dedup_key(url), text)

    out["меню"] = [{"url": u, "подпись": titles_by_url.get(k) or None}
                   for k, u in sorted(menu.items())]
    out["подвал"] = [{"url": u, "подпись": titles_by_url.get(k) or None}
                     for k, u in sorted(footer.items())]

    # --- Карта сайта -------------------------------------------------------
    robots = read_robots(base)
    sitemap_urls = []
    for sm in robots["sitemaps"] or [base.rstrip("/") + "/sitemap.xml"]:
        sitemap_urls += read_sitemap(sm)
    sitemap_urls = sorted(set(sitemap_urls))

    out["карта_сайта"] = {
        "найдена": bool(sitemap_urls),
        "страниц": len(sitemap_urls),
    }

    # --- Разделы -----------------------------------------------------------
    all_urls = sorted(set(list(menu.values()) + list(footer.values())
                          + list(other.values()) + sitemap_urls))
    out["разделы"] = sections(all_urls)
    out["всего_страниц_найдено"] = len(all_urls)

    geo = looks_geo_landing(all_urls)
    out["городские_посадочные"] = {
        "найдены": geo,
        "вывод": (
            f"похоже на федеральную сеть городских посадочных ({len(geo)} городов) — "
            "это не локальный конкурент" if len(geo) >= 2 else
            "признаков городских посадочных нет — вероятно, локальный сайт"
        ),
    }

    # --- Заголовки страниц меню (рабочие названия для структуры) ----------
    titles = []
    for url in sorted(menu.values())[:MAX_TITLES]:
        r = fetch(url)
        if r["status"] == 200:
            p = parse_page(r["body"])
            titles.append({
                "url": url,
                "title": p.title or None,
                "h1": p.h1[0] if p.h1 else None,
            })
    out["страницы_меню"] = titles
    out["заголовки_ограничены"] = len(menu) > MAX_TITLES

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out", help="куда сохранить JSON")
    args = ap.parse_args()

    base = args.url if args.url.startswith("http") else "https://" + args.url
    data = analyze(base)

    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Сохранено: {args.out}")
        print(f"  разделов: {len(data.get('разделы', {}))}, "
              f"страниц: {data.get('всего_страниц_найдено', 0)}")
    else:
        print(text)


if __name__ == "__main__":
    sys.exit(main())

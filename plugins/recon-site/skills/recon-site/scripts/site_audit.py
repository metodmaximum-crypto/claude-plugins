"""Шаг 2 регламента: экспресс-аудит текущего сайта клиента.

Запуск:  python scripts/site_audit.py https://example.ru [--out файл.json]

Собирает находки, а не оценки. Всё, что не удалось проверить, помечается
явно — чтобы агент не выдал пробел в данных за отсутствие проблемы.
"""

import argparse
import json
import sys
from datetime import date

from lib import (dedup_key, fetch, normalize, parse_page, read_robots,
                 read_sitemap, same_host, sections)

# Юрстраницы, которые регламент требует посчитать (минимум 4–7 на сайте).
# Ищем и по адресу, и по тексту ссылки: адрес часто ничего не говорит.
LEGAL = {
    "политика ПДн": ("polit", "privacy", "personal", "pdn",
                     "политик", "конфиденциальн", "персональн"),
    "согласие на обработку": ("soglas", "consent", "согласие", "согласи"),
    "оферта / правила": ("oferta", "offer", "usloviya", "terms", "dogovor",
                         "оферт", "услови", "договор", "правила"),
    "куки": ("cookie", "cooki", "куки"),
}
MAX_LINK_CHECKS = 40


def audit(base):
    out = {"сайт": base, "дата_замера": date.today().isoformat()}

    # --- Главная -----------------------------------------------------------
    home = fetch(base)
    out["главная"] = {
        "код_ответа": home["status"],
        "финальный_url": home["final_url"],
        "редирект": home["final_url"].rstrip("/") != base.rstrip("/"),
        "ошибка": home["error"],
    }
    if home["error"] or home["status"] != 200:
        out["вывод"] = "Сайт не открылся — аудит невозможен, это уже находка."
        return out

    page = parse_page(home["body"])
    body_lower = home["body"].lower()

    out["главная"].update({
        "title": page.title or None,
        "description": page.meta_description or None,
        "h1": page.h1 or None,
        "canonical": page.canonical or None,
        "вес_html_кб": round(len(home["body"].encode("utf-8")) / 1024, 1),
    })

    # --- Аналитика и мобильность ------------------------------------------
    out["аналитика"] = {
        "яндекс_метрика": "mc.yandex.ru" in body_lower or "ym(" in body_lower,
        "google_analytics": "googletagmanager.com" in body_lower
        or "google-analytics.com" in body_lower,
    }
    out["мобильная_версия"] = {
        "есть_viewport": page.has_viewport,
        "комментарий": None if page.has_viewport
        else "нет meta viewport — вероятны проблемы на мобильных",
    }
    out["https"] = base.startswith("https://") or home["final_url"].startswith("https://")

    # --- robots.txt и карта сайта -----------------------------------------
    robots = read_robots(base)
    out["robots"] = robots

    sitemap_urls = []
    for sm in robots["sitemaps"] or [base.rstrip("/") + "/sitemap.xml"]:
        sitemap_urls += read_sitemap(sm)
    sitemap_urls = sorted(set(sitemap_urls))
    out["карта_сайта"] = {
        "найдена": bool(sitemap_urls),
        "страниц_в_карте": len(sitemap_urls),
        "разделы": sections(sitemap_urls) if sitemap_urls else {},
    }

    # --- Ссылки с главной: меню, подвал, разделы --------------------------
    internal, external = {}, set()  # ключ склейки -> (рабочий url, зона)
    for href, _, zone in page.links:
        url = normalize(href, home["final_url"])
        if not url:
            continue
        if same_host(url, home["final_url"]):
            internal.setdefault(dedup_key(url), (url, zone))
        else:
            external.add(url)

    internal_urls = [u for u, _ in internal.values()]
    out["ссылки_с_главной"] = {
        "внутренних": len(internal),
        "внешних": len(external),
        "в_меню": sum(1 for _, z in internal.values() if z == "nav"),
        "в_подвале": sum(1 for _, z in internal.values() if z == "footer"),
    }
    out["структура_по_главной"] = sections(internal_urls)

    # --- Юрстраницы --------------------------------------------------------
    # Пары «адрес + текст ссылки» с главной, плюс голые адреса из карты сайта.
    link_text = {}
    for href, text, _zone in page.links:
        u = normalize(href, home["final_url"])
        if u:
            link_text.setdefault(dedup_key(u), text)
    haystack = [(u, link_text.get(dedup_key(u), "")) for u in internal_urls]
    haystack += [(u, "") for u in sitemap_urls]

    legal_found = {}
    for name, patterns in LEGAL.items():
        hit = next(
            (u for u, text in haystack
             if any(p in u.lower() or p in text.lower() for p in patterns)),
            None,
        )
        legal_found[name] = hit
    out["юрстраницы"] = {
        "найдено": {k: v for k, v in legal_found.items() if v},
        "не_найдено": [k for k, v in legal_found.items() if not v],
    }

    # --- 404 ---------------------------------------------------------------
    probe = fetch(base.rstrip("/") + "/nesushchestvuyushchaya-stranica-proverka-404")
    out["страница_404"] = {
        "код_ответа": probe["status"],
        "корректна": probe["status"] == 404,
        "комментарий": None if probe["status"] == 404
        else f"на несуществующий адрес отдаётся {probe['status']}, а должен 404",
    }

    # --- Битые ссылки (выборка) -------------------------------------------
    checked, broken = 0, []
    for url in internal_urls[:MAX_LINK_CHECKS]:
        r = fetch(url, method="HEAD")
        if r["status"] is None or r["status"] >= 400:
            r = fetch(url)  # часть серверов не любит HEAD — перепроверяем GET
        checked += 1
        if r["error"] or (r["status"] and r["status"] >= 400):
            broken.append({"url": url, "код": r["status"], "ошибка": r["error"]})
    out["битые_ссылки"] = {
        "проверено": checked,
        "всего_внутренних": len(internal),
        "выборка_ограничена": len(internal) > MAX_LINK_CHECKS,
        "битых": len(broken),
        "список": broken,
    }

    # --- Сводка находок ----------------------------------------------------
    findings = []
    if not out["аналитика"]["яндекс_метрика"]:
        findings.append("Метрика не установлена — аналитики нет, задача проекта")
    if not page.has_viewport:
        findings.append("нет meta viewport — мобильная версия под вопросом")
    if not out["https"]:
        findings.append("сайт не на https")
    if not robots["exists"]:
        findings.append("нет robots.txt")
    if not sitemap_urls:
        findings.append("нет карты сайта (sitemap.xml)")
    if not out["страница_404"]["корректна"]:
        findings.append(out["страница_404"]["комментарий"])
    if out["юрстраницы"]["не_найдено"]:
        findings.append("не найдены юрстраницы: "
                        + ", ".join(out["юрстраницы"]["не_найдено"]))
    if broken:
        findings.append(f"битых внутренних ссылок: {len(broken)}")
    if not page.title:
        findings.append("у главной нет title")
    if not page.meta_description:
        findings.append("у главной нет description")
    out["находки"] = findings

    out["не_проверено"] = [
        "скорость загрузки и Core Web Vitals — нужен PageSpeed Insights",
        "видимость в поиске (site:) — нужен Search API",
        "реальный трафик — нужен доступ к Метрике клиента",
    ]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out", help="куда сохранить JSON")
    args = ap.parse_args()

    base = args.url if args.url.startswith("http") else "https://" + args.url
    data = audit(base)

    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Сохранено: {args.out}")
        for f_ in data.get("находки", []):
            print(f"  - {f_}")
    else:
        print(text)


if __name__ == "__main__":
    sys.exit(main())

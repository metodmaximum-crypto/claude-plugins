"""Шаг 4 регламента: снятие выдачи и классификация топа.

Движки:
  --engine yandex      Yandex Search API (нужны YC_API_KEY, YC_FOLDER_ID)
  --engine google-cse  Google Custom Search JSON API (GOOGLE_CSE_KEY, GOOGLE_CSE_CX)

Запуск:
  python scripts/serp.py --engine yandex --region 213 -q "аренда авто пхукет"
  python scripts/serp.py --engine google-cse --gl th --hl ru --queries queries.txt

ВАЖНО про google-cse: это НЕ реальная выдача Google. Другой индекс, нет
локального пака, порядок отличается. Годится для «кто вообще есть в нише»,
не годится для «кто в топ-5 с геолокацией». Настоящая выдача — режим браузера.
"""

import argparse
import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from lib import read_env

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
AGG_FILE = ROOT / "references" / "aggregators.md"

# Разделы справочника -> тип позиции в выдаче.
# Разделы справочника -> тип позиции. Всё, что не сервис поисковика, — агрегатор,
# поэтому новую отраслевую вертикаль достаточно добавить в aggregators.md,
# не трогая код.
SECTION_TYPE = {
    "сервисы поисковиков": "сервис",
}
SECTION_TYPE_DEFAULT = "агрегатор"


def load_env():
    return read_env()[0]


def load_domains():
    """Единственный источник правды — references/aggregators.md."""
    if not AGG_FILE.exists():
        return {}
    text = AGG_FILE.read_text(encoding="utf-8")
    domains, section = {}, None
    in_block = False
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip().lower()
            in_block = False
        elif line.startswith("```"):
            in_block = not in_block
        elif in_block and section:
            kind = SECTION_TYPE.get(section, SECTION_TYPE_DEFAULT)
            for token in line.split():
                domains[token.strip().lower()] = kind
    return domains


DOMAINS = load_domains()


def classify(url):
    """сервис / агрегатор / рейтинг / сайт + сам домен."""
    host = urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    for pattern, kind in DOMAINS.items():
        base = pattern.replace("*", "")
        if pattern.endswith(".*"):
            if host == base.rstrip(".") or host.startswith(base):
                return kind, host
        elif "/" in pattern:
            if pattern in url.lower():
                return kind, host
        elif host == pattern or host.endswith("." + pattern):
            return kind, host
    return "сайт", host


def looks_like_rating(title):
    return bool(re.search(
        r"\bтоп[\s-]?\d|\bлучш|рейтинг|\btop[\s-]?\d|\bbest\b|обзор",
        title or "", re.I))


def egress_info():
    """Откуда физически уходит запрос.

    Для Google это определяет выдачу сильнее, чем параметр gl: у Google
    приоритет за IP. Снятая «не оттуда» выдача выглядит достоверной и врёт,
    поэтому страна выхода пишется в артефакт всегда.
    """
    try:
        req = urllib.request.Request("https://ipinfo.io/json",
                                     headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
            return {"страна": d.get("country"), "город": d.get("city"),
                    "провайдер": d.get("org")}
    except Exception as e:
        return {"страна": None, "ошибка": f"{type(e).__name__}: {e}"}


def http_json(url, data=None, headers=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data else None,
        headers=headers or {},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return True, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# --- Движки ----------------------------------------------------------------

def search_yandex(query, env, region, depth):
    ok, res = http_json(
        "https://searchapi.api.cloud.yandex.net/v2/web/search",
        {
            "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query,
                      "page": "0"},
            "groupSpec": {"groupMode": "GROUP_MODE_DEEP",
                          "groupsOnPage": str(depth), "docsInGroup": "1"},
            "region": str(region),
            "l10n": "LOCALIZATION_RU",
            "folderId": env.get("YC_FOLDER_ID", ""),
            "responseFormat": "FORMAT_XML",
        },
        {"Authorization": f"Api-Key {env.get('YC_API_KEY', '')}",
         "Content-Type": "application/json"},
    )
    if not ok:
        return None, res
    xml = base64.b64decode(res.get("rawData", "")).decode("utf-8", "replace")
    if not xml.strip():
        return None, "API вернул пустой rawData"
    results = []
    # Тег приходит с атрибутами: <doc id="...">. Голый <doc> не встречается,
    # поэтому старый шаблон r"<doc>" молча давал ноль позиций.
    # \b нужен, чтобы не цеплять соседний <doccount>.
    for doc in re.findall(r"<doc\b[^>]*>(.*?)</doc>", xml, re.S)[:depth]:
        url = re.search(r"<url>(.*?)</url>", doc, re.S)
        title = re.search(r"<title>(.*?)</title>", doc, re.S)
        results.append({
            "url": (url.group(1).strip() if url else ""),
            "title": re.sub(r"<[^>]+>", "", title.group(1)).strip() if title else "",
        })
    if not results:
        # Ответ пришёл, а позиций ноль — это почти всегда разъехавшийся разбор,
        # а не пустая выдача. Молчать здесь нельзя: разведка выглядит снятой.
        found = re.search(r"<found-human>(.*?)</found-human>", xml, re.S)
        return None, ("разбор XML не дал ни одной позиции"
                      + (f"; поиск сообщает: {found.group(1).strip()}" if found else "")
                      + ". Проверьте формат <doc> в ответе API.")
    return results, None


def search_google_cse(query, env, gl, hl, depth):
    params = urllib.parse.urlencode({
        "key": env.get("GOOGLE_CSE_KEY", ""),
        "cx": env.get("GOOGLE_CSE_CX", ""),
        "q": query,
        "num": min(depth, 10),
        "gl": gl,
        "hl": hl,
    })
    ok, res = http_json(f"https://www.googleapis.com/customsearch/v1?{params}")
    if not ok:
        return None, res
    return [{"url": i.get("link", ""), "title": i.get("title", "")}
            for i in res.get("items", [])], None


# --- Сборка ----------------------------------------------------------------

def run(query, args, env):
    if args.engine == "yandex":
        rows, err = search_yandex(query, env, args.region, args.depth)
        where = {"движок": "yandex", "регион": args.region}
    else:
        rows, err = search_google_cse(query, env, args.gl, args.hl, args.depth)
        where = {"движок": "google-cse", "страна": args.gl, "язык": args.hl,
                 "оговорка": "индекс CSE, не реальная выдача Google"}

    out = {"запрос": query, "дата_замера": date.today().isoformat(), **where}
    if err:
        out["ошибка"] = err
        return out

    positions, counts = [], {"сервис": 0, "агрегатор": 0, "рейтинг": 0, "сайт": 0}
    for i, row in enumerate(rows, 1):
        kind, host = classify(row["url"])
        if kind == "сайт" and looks_like_rating(row["title"]):
            kind = "рейтинг"
        counts[kind] += 1
        positions.append({"позиция": i, "домен": host, "тип": kind,
                          "title": row["title"][:120], "url": row["url"]})

    total = len(positions) or 1
    occupied = counts["сервис"] + counts["агрегатор"] + counts["рейтинг"]
    out.update({
        "позиций": len(positions),
        "топ": positions,
        "по_типам": counts,
        "доля_занятого_топа": round(occupied / total, 2),
        "настоящие_конкуренты": [p["домен"] for p in positions if p["тип"] == "сайт"],
    })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["yandex", "google-cse"], required=True)
    ap.add_argument("-q", "--query", action="append", default=[])
    ap.add_argument("--queries", help="файл со списком запросов, по одному в строке")
    ap.add_argument("--region", default="225", help="ID региона Яндекса (225 = Россия)")
    ap.add_argument("--gl", default="ru", help="страна для Google (th, ru, us)")
    ap.add_argument("--hl", default="ru", help="язык интерфейса Google")
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--expect-country",
                    help="ожидаемая страна выхода (RU, TH)")
    ap.add_argument("--geo-terms", default="",
                    help="названия мест через запятую: пхукет,phuket,патонг. "
                         "Запрос с таким словом Google трактует по локации из "
                         "запроса, а не по IP — для него проверка мягкая")
    ap.add_argument("--force", action="store_true",
                    help="снять несмотря на несовпадение страны выхода")
    ap.add_argument("--out")
    args = ap.parse_args()

    queries = list(args.query)
    if args.queries:
        queries += [l.strip() for l in
                    Path(args.queries).read_text(encoding="utf-8").splitlines()
                    if l.strip() and not l.startswith("#")]
    if not queries:
        ap.error("нужен хотя бы один запрос: -q «...» или --queries файл")

    geo_terms = [t.strip().lower() for t in args.geo_terms.split(",") if t.strip()]
    with_geo = {q: any(t in q.lower() for t in geo_terms) for q in queries}

    egress = egress_info()
    print(f"Запрос уходит из: {egress.get('город')} / {egress.get('страна')} "
          f"({egress.get('провайдер')})")

    if args.engine != "yandex":
        want = (args.expect_country or "").upper()
        got = (egress.get("страна") or "").upper()
        blind = [q for q, has in with_geo.items() if not has]
        if want and want != got:
            if blind and not args.force:
                print(f"\n[X] страна выхода {got or '?'} ≠ ожидаемой {want}, "
                      f"и {len(blind)} запросов без географии в тексте:")
                for q in blind[:5]:
                    print(f"      {q}")
                print("    Для них локацию подставит только IP — выдача будет "
                      "чужой.\n    Либо добавьте место в запрос, либо снимайте "
                      "из нужной страны,\n    либо --force, если это осознанно.")
                sys.exit(2)
            print(f"[!] страна выхода {got or '?'} ≠ {want}. "
                  "Запросы с географией в тексте Google трактует по ней — "
                  "приемлемо;\n    локальный пак и реклама всё равно будут чужими.")

    env = load_env()
    if not DOMAINS:
        print("[!] references/aggregators.md не прочитан — классификация будет пустой")

    results = []
    for q in queries:
        print(f"  {q} ...", end=" ", flush=True)
        r = run(q, args, env)
        r["география_запроса"] = ("место названо в запросе" if with_geo[q]
                                 else "определяется страной выхода")
        print("ошибка" if "ошибка" in r else
              f"занято {int(r['доля_занятого_топа'] * 100)}%")
        results.append(r)

    payload = {"дата_замера": date.today().isoformat(),
               "движок": args.engine, "страна_выхода": egress,
               "запросов": len(results), "результаты": results}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"\nСохранено: {args.out}")
        ok = [r for r in results if "ошибка" not in r]
        if ok:
            avg = sum(r["доля_занятого_топа"] for r in ok) / len(ok)
            print(f"Средняя доля занятого топа: {int(avg * 100)}%")
            sites = {d for r in ok for d in r["настоящие_конкуренты"]}
            print(f"Настоящих сайтов в выдаче: {len(sites)}")
            for s in sorted(sites)[:15]:
                print(f"  - {s}")
    else:
        print(text)


if __name__ == "__main__":
    main()

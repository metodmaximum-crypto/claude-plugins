"""Спрос по Wordstat через Yandex Search API.

Запуск:
  python scripts/wordstat.py --queries phrases.txt --regions 76,11457 --out 02-wordstat.json
  python scripts/wordstat.py -q "узи хабаровск" -q "гинеколог хабаровск" --regions 76

Регион задаётся ID Яндекса. Узнать ID: python scripts/check_access.py "фраза" <ID>
(там же печатается название региона — сверяйте, ошибка в регионе тихая и дорогая).

Чекпоинт пишется после каждой фразы: прогон можно прервать и повторить,
уже снятое не переснимается и повторно не оплачивается.

Только stdlib.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from lib import read_env

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
API = "https://searchapi.api.cloud.yandex.net/v2/wordstat"


def load_env():
    env, path = read_env()
    if not path and not env.get("YC_API_KEY"):
        sys.exit("[X] " + "ключи не найдены. Положите их в ~/.recon-site/.env "
             "(образец — .env.example в папке скилла).")
    for k in ("YC_API_KEY", "YC_FOLDER_ID"):
        if not env.get(k):
            sys.exit(f"[X] не заполнен {k} ({path or 'переменные окружения'})")
    return env


def call(method, payload, key, tries=3):
    req = urllib.request.Request(
        f"{API}/{method}", data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Api-Key {key}", "Content-Type": "application/json"},
        method="POST")
    for n in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            if e.code == 429:
                # 429 НИКОГДА не повторяем: повтор тратит ту же квоту, которая
                # уже кончилась, и отодвигает окно. Три повтора на фразу
                # превращают прогон в 3× расход и добивают лимит в ноль.
                return None, f"RATE_LIMIT: HTTP 429: {body}"
            if e.code in (500, 502, 503) and n < tries - 1:
                time.sleep(3 * (n + 1)); continue
            return None, f"HTTP {e.code}: {body}"
        except Exception as e:
            if n < tries - 1:
                time.sleep(3); continue
            return None, f"{type(e).__name__}: {e}"


def region_names(folder, key):
    """id -> название. Ответ API кладёт название в label, не в name."""
    data, err = call("getRegionsTree", {"folderId": folder}, key)
    names = {}
    if err:
        return names

    def walk(node):
        if isinstance(node, dict):
            rid = node.get("id") or node.get("regionId")
            nm = node.get("name") or node.get("label")
            if rid is not None and nm:
                names[str(rid)] = nm
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return names


def base_count(data, phrase):
    """Частотность базовой фразы + связанные запросы.

    Берём totalCount: это частотность фразы со всеми её уточнениями —
    ровно то, что показывает Wordstat рядом с запросом. Сумму строк
    складывать нельзя, она больше базы (уточнения пересекаются).

    Запасной путь — строка, совпадающая с фразой. Нужен редко: у
    низкочастотных запросов сама фраза в свой топ не попадает, и тогда
    без totalCount данных не будет вовсе.
    """
    rows = data.get("results") or data.get("topRequests") or data.get("requests") or []
    norm = lambda s: " ".join(str(s).lower().split())
    total, related = None, []

    raw_total = data.get("totalCount")
    if str(raw_total).isdigit():
        total = int(raw_total)

    for r in rows:
        txt = r.get("phrase") or r.get("text") or ""
        raw = r.get("count", 0)
        cnt = int(raw) if str(raw).isdigit() else 0
        if norm(txt) == norm(phrase):
            if total is None:
                total = cnt
        else:
            related.append({"фраза": txt, "показов": cnt})
    return total, related[:6]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-q", "--query", action="append", default=[])
    ap.add_argument("--queries", help="файл со списком фраз, по одной в строке")
    ap.add_argument("--regions", default="225", help="ID через запятую (225 = Россия)")
    ap.add_argument("--related", type=int, default=15, help="сколько связанных запросов запрашивать")
    ap.add_argument("--out", default="02-wordstat.json")
    args = ap.parse_args()

    phrases = list(args.query)
    if args.queries:
        phrases += [l.strip() for l in Path(args.queries).read_text(encoding="utf-8").splitlines() if l.strip()]
    phrases = list(dict.fromkeys(phrases))
    if not phrases:
        sys.exit("[X] не задано ни одной фразы: --queries файл или -q фраза")
    if len(phrases) > 30:
        print(f"[!] фраз {len(phrases)}. Регламент: 10–30 базовых на срез. "
              f"Длинные хвосты не берите — базовый запрос уже включает уточнения.")

    env = load_env()
    key, folder = env["YC_API_KEY"], env["YC_FOLDER_ID"]
    regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    names = region_names(folder, key)
    regmap = {r: names.get(r, r) for r in regions}
    for r, n in regmap.items():
        print(f"регион {r} = {n}" + ("   [!] в справочнике не найден — проверьте ID" if n == r else ""))

    out = Path(args.out)
    result = {"дата_замера": date.today().isoformat(),
              "источник": "Yandex Search API, /v2/wordstat/topRequests",
              "регионы": regmap, "запросы": []}
    if out.exists():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
            if prev.get("запросы"):
                result = prev
                print(f"[i] продолжаю с чекпоинта: уже снято {len(prev['запросы'])}")
                # Названия регионов из чекпоинта важнее текущих: если getRegionsTree
                # упёрся в 429, names пуст и regmap подставляет сами ID. Тогда строки
                # чекпоинта, записанные по названиям, перестают находиться — и ниже
                # весь снятый прогон улетает в «пустышки». Проверено на mda-medusa:
                # так стёрлось 19 уже оплаченных фраз.
                for rid, nm in (prev.get("регионы") or {}).items():
                    if rid in regmap and regmap[rid] == rid and nm != rid:
                        regmap[rid] = nm
                        print(f"    название региона {rid} взято из чекпоинта: {nm}")
                result["регионы"] = regmap
        except Exception:
            pass
    # Пустышки (все регионы None) — это упавшие замеры, а не снятые. Иначе при
    # возобновлении они считаются «уже сделанными» и фраза молча остаётся без цифры.
    # Сверяем и по названию региона, и по ID: в старых чекпоинтах ключ мог быть любым.
    reg_keys = set(regmap.values()) | set(regmap.keys())
    empty = [q for q in result["запросы"] if all(q.get(k) is None for k in reg_keys)]
    if empty:
        result["запросы"] = [q for q in result["запросы"] if q not in empty]
        print(f"[i] выброшено пустых строк из чекпоинта: {len(empty)} — будут пересняты")
    done = {q["фраза"] for q in result["запросы"]}

    todo = [p for p in phrases if p not in done]
    budget = len(todo) * len(regions) + 1  # +1 на getRegionsTree
    print(f"[i] к снятию {len(todo)} фраз × {len(regions)} рег. = ~{budget} обращений "
          f"при часовой квоте 100")
    if budget > 90:
        print("    [!] прогон не поместится в час. Разбейте список или поднимите квоту "
              "(Yandex Cloud → Квоты → search-api.wordstatRequestsPerHour.rate)")

    head = "".join(f"{n[:14]:>15}" for n in regmap.values())
    print(f"\n{'запрос':<44}{head}")
    print("-" * (44 + 15 * len(regions)))
    rate_limited = None
    for phrase in todo:
        row = {"фраза": phrase}
        for rid, rname in regmap.items():
            data, err = call("topRequests", {"folderId": folder, "phrase": phrase,
                                             "regions": [rid], "numPhrases": args.related}, key)
            if err and err.startswith("RATE_LIMIT"):
                rate_limited = phrase
                break
            if err:
                row[rname] = None
                row.setdefault("ошибки", []).append(f"{rname}: {err}")
            else:
                cnt, rel = base_count(data, phrase)
                row[rname] = cnt
                if rid == regions[0]:
                    row["связанные"] = rel
            time.sleep(0.7)
        if rate_limited:
            break  # частичную строку в чекпоинт не пишем
        result["запросы"].append(row)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        vals = "".join(f"{str(row.get(n) if row.get(n) is not None else '—'):>15}" for n in regmap.values())
        print(f"{phrase[:44]:<44}{vals}")

    print("-" * (44 + 15 * len(regions)))
    if rate_limited:
        print(f"\n[X] Часовая квота Wordstat исчерпана на фразе «{rate_limited}».")
        print("    Прогон остановлен намеренно: каждое следующее обращение уходит")
        print("    в отказ и продлевает окно ожидания.")
        print(f"    Снято до остановки: {len(result['запросы'])} из {len(phrases)}.")
        print("    Запустите ту же команду позже — чекпоинт сохранён, снятое не переснимается.")
    totals = "".join(f"{sum(q.get(n) or 0 for q in result['запросы']):>15}" for n in regmap.values())
    print(f"{'ИТОГО':<44}{totals}")
    nulls = [q["фраза"] for q in result["запросы"] if all(q.get(n) is None for n in regmap.values())]
    if nulls:
        print(f"\n[!] без данных ({len(nulls)}): {', '.join(nulls[:5])}")
        print("    Такие строки в файле разведки помечаются «гипотеза», а не выдуманным числом.")
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()

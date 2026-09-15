"""Проверка доступа к Yandex Search API: Wordstat + справочник регионов.

Запуск:  python scripts/check_access.py [фраза] [регион]
Пример:  python scripts/check_access.py "стоматология хабаровск" 76

Ключ и folderId читаются из ~/.recon-site/.env (или .env в папке скилла). В вывод они не попадают.
Только stdlib — ставить ничего не нужно.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from lib import read_env

if hasattr(sys.stdout, "reconfigure"):  # Windows: консоль по умолчанию не UTF-8
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = "https://searchapi.api.cloud.yandex.net/v2/wordstat"


def load_env():
    env, path = read_env()
    if not path and not env.get("YC_API_KEY"):
        die("ключи не найдены. Положите их в ~/.recon-site/.env\n"
            "    (образец — .env.example в папке скилла).")
    print(f"Ключи из: {path or 'переменных окружения'}")
    for key in ("YC_API_KEY", "YC_FOLDER_ID"):
        if not env.get(key):
            die(f"не заполнен {key}")
    folder = env["YC_FOLDER_ID"]
    if folder.startswith("aje"):
        die(
            f"YC_FOLDER_ID={folder} — это идентификатор сервисного аккаунта, не каталога.\n"
            "    У каталогов ID начинается с b1g. Возьмите его из адреса консоли:\n"
            "    console.yandex.cloud/folders/<вот этот кусок>"
        )
    if not folder.startswith("b1g"):
        print(f"[!] YC_FOLDER_ID={folder} не похож на ID каталога (обычно b1g...)")
    return env


def die(msg):
    print(f"\n[X] {msg}\n")
    sys.exit(1)


HINTS = {
    401: "ключ не принят — проверьте YC_API_KEY, он мог обрезаться при копировании",
    403: "нет прав — роль search-api.executor не назначена тому сервисному\n"
    "      аккаунту, которому принадлежит ключ, либо ключ выпущен с ограниченной областью действия",
    402: "не привязан платёжный аккаунт",
    404: "не найден — проверьте YC_FOLDER_ID (кусок после /folders/ в адресе консоли)",
}


def call(method, api_key, payload):
    """Возвращает (ok, данные_или_текст_ошибки). Не прерывает выполнение."""
    req = urllib.request.Request(
        f"{ROOT}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Api-Key {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:600]
        return False, f"HTTP {e.code}\n      {HINTS.get(e.code, '')}\n      {body}"
    except urllib.error.URLError as e:
        return False, f"сеть недоступна: {e.reason}"


def main():
    phrase = sys.argv[1] if len(sys.argv) > 1 else "стоматология хабаровск"
    region = sys.argv[2] if len(sys.argv) > 2 else "76"

    env = load_env()
    key, folder = env["YC_API_KEY"], env["YC_FOLDER_ID"]
    print(f"Ключ: ...{key[-4:]}   Каталог: {folder}")

    # 1. Справочник регионов — заодно подтверждаем название региона по его ID.
    print("\n[1/2] getRegionsTree ...", end=" ", flush=True)
    ok_tree, tree = call("getRegionsTree", key, {"folderId": folder})
    print("ok" if ok_tree else "ОШИБКА")
    if not ok_tree:
        print(f"      {tree}")
        tree = {}

    names = {}

    def walk(node):
        if isinstance(node, dict):
            rid = node.get("id") or node.get("regionId")
            # API отдаёт название в поле label; name оставлен на случай смены схемы
            name = node.get("name") or node.get("label")
            if rid is not None and name:
                names[str(rid)] = name
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(tree)
    region_name = names.get(str(region))
    if ok_tree:
        print(f"      регионов в справочнике: {len(names)}")
        if region_name:
            print(f"      регион {region} = {region_name}")
        else:
            print(f"      [!] регион {region} в справочнике не найден — проверьте ID")

    # 2. Частотность — проверяем отдельно, даже если справочник не отдался:
    #    так видно, сплошной это отказ или только на одном методе.
    print("\n[2/2] topRequests ...", end=" ", flush=True)
    ok_top, data = call(
        "topRequests",
        key,
        {
            "folderId": folder,
            "phrase": phrase,
            "regions": [str(region)],
            "numPhrases": 10,
        },
    )
    print("ok" if ok_top else "ОШИБКА")
    if not ok_top:
        print(f"      {data}")

    if not (ok_tree or ok_top):
        die(
            "оба метода отказали одинаково — дело не в методе, а в доступе.\n"
            "    Проверьте в консоли, какому сервисному аккаунту принадлежит ключ,\n"
            "    и что роль search-api.executor назначена именно ему."
        )
    if not ok_top:
        die("справочник доступен, а частотность нет — вопрос в правах на Wordstat.")

    rows = data.get("results") or data.get("topRequests") or data.get("requests") or []
    if not rows:
        print("\n[!] запрос прошёл, но список пуст — узкая ниша или другой регион.")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:800])
        return

    print(f"\n  Фраза: {phrase!r}   Регион: {region_name or region}\n")
    print(f"  {'запрос':<48} {'показов/мес':>12}")
    print(f"  {'-' * 48} {'-' * 12}")
    for row in rows[:10]:
        text = row.get("phrase") or row.get("text") or "?"
        raw = row.get("count", 0)
        count = int(raw) if str(raw).isdigit() else 0  # приходит строкой
        print(f"  {text[:48]:<48} {count:>12,}".replace(",", " "))

    print("\n[OK] Доступ работает. Ключ, каталог и регион подтверждены.")
    print("     Проверьте списание в биллинге — это фактическая цена ~11 вызовов.")


if __name__ == "__main__":
    main()

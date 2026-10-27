"""
Marka izleme paneli üretici: veri.py'nin günlük serilerini panel_template.html'e
gömüp index.html yazar (sayfa statik; veri derleme anında içine girer).

Kullanım:
    python build_panel.py            # canlı API'lerden çek
    python build_panel.py veri.json  # kayıtlı veriden derle (deneme için)
"""

import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "panel_template.html")
OUTPUT = os.path.join(HERE, "index.html")


def main() -> None:
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        import veri
        data = veri.collect()

    # "</script>" veri içinde script bloğunu kapatmasın.
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    api = json.dumps(os.environ.get("HEDEF_API_URL", ""))
    block = f"const DATA = {payload};\nconst HEDEF_API_URL = {api};"

    with open(TEMPLATE, encoding="utf-8") as fh:
        template = fh.read()
    if "/*__DATA__*/" not in template:
        sys.exit("Şablonda /*__DATA__*/ yer tutucusu yok.")
    with open(OUTPUT, "w", encoding="utf-8") as fh:
        fh.write(template.replace("/*__DATA__*/", block))
    print(f"index.html yazıldı — {len(data['days'])} gün, {data['days'][0]} … {data['today']}, "
          f"{len(data.get('targets') or {})} hedef")


if __name__ == "__main__":
    main()

"""
Meta (Facebook/Instagram) Ads -> Google Sheets anlık rapor senkronizasyonu.

Aqua di Polo, Frime ve Pierre Cardin markalarının Trendyol/Hepsiburada
CPAS hesaplarındaki "shared items" (collaborative ads / catalog segment)
metriklerini üç sabit dönem için (Dün / Son 7 Gün / Bu Ay) çekip her
markanın kendi sekmesine yazar.

Her çalıştırma sekmedeki eski veriyi TEMİZLER ve güncel anlık durumu
yazar — biriken bir log değil, her seferinde tazelenen bir tablo.

Kurulum için README.md dosyasına bakın.

Kullanım:
    python sync.py
"""

import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

GRAPH_API_VERSION = "v26.0"

# Marka -> {kanal: ad_account_id}
AD_ACCOUNTS = {
    "Aqua di Polo": {
        "Trendyol": "1491645661756000",
        "Hepsiburada": "1659658078807461",
    },
    "Frime": {
        "Trendyol": "1094178842462484",
        "Hepsiburada": "793918126452562",
    },
    "Pierre Cardin": {
        "Trendyol": "1453774025708518",
        "Hepsiburada": "1436412821275075",
    },
}

# Marka -> marka bazlı süresiz System User token'ının .env değişken adı.
# O değişken boşsa/tanımsızsa genel META_ACCESS_TOKEN'a (60 günlük) düşülür.
BRAND_TOKEN_ENV_VAR = {
    "Aqua di Polo": "META_ACCESS_TOKEN_AQUA_DI_POLO",
    "Frime": "META_ACCESS_TOKEN_FRIME",
    "Pierre Cardin": "META_ACCESS_TOKEN_PIERRE_CARDIN",
}

# Sabit üç dönem: (görünen isim, Graph API date_preset)
PERIODS = [
    ("Dün", "yesterday"),
    ("Son 7 Gün", "last_7d"),
    ("Bu Ay", "this_month"),
]

INSIGHTS_FIELDS = [
    "spend",
    "catalog_segment_actions",
    "catalog_segment_value",
    "catalog_segment_value_website_purchase_roas",
    "catalog_segment_value_omni_purchase_roas",
    "catalog_segment_value_mobile_purchase_roas",
    "inline_link_clicks",
]

SHEET_HEADER = [
    "Dönem",
    "Kanal",
    "Ad Account ID",
    "Amount Spent",
    "Content Views (shared)",
    "Adds to Cart (shared)",
    "Purchases (shared)",
    "Purchase Conversion Value (shared)",
    "Purchase ROAS (shared)",
    "Link Clicks",
    "Son Güncelleme (UTC)",
]


def resolve_access_token(brand: str) -> str:
    brand_var = BRAND_TOKEN_ENV_VAR.get(brand)
    brand_token = os.environ.get(brand_var) if brand_var else None
    token = brand_token or os.environ.get("META_ACCESS_TOKEN")
    if not token:
        sys.exit(f"{brand} için ne {brand_var} ne de META_ACCESS_TOKEN .env'de tanımlı.")
    return token


def _find_action_value(actions, candidates):
    """catalog_segment_actions / catalog_segment_value listesi aynı olayı
    (ör. bir satın alma) birden çok kırılımla AYNI ANDA döner: omni_purchase
    (kanala göre tekilleştirilmiş toplam) ile birlikte onu oluşturan
    onsite_app_purchase, offsite_conversion.fb_pixel_purchase,
    app_custom_event.fb_mobile_purchase ve "purchase" alias'ı gibi alt
    kırılımlar da ayrı satırlar olarak listede yer alır. Bunların hepsini
    toplamak aynı satın almayı 3-4 kez saymaya yol açar, bu yüzden
    candidates sırasına göre TEK bir kanonik action_type seçiyoruz
    (öncelik: omni_*, yoksa genel alias)."""
    by_type = {item.get("action_type"): item.get("value") for item in (actions or [])}
    for candidate in candidates:
        value = by_type.get(candidate)
        if value is not None:
            return float(value)
    return None


def _first_roas(*roas_lists):
    """Birden fazla ROAS alanından (omni/website/mobile) ilk bulunan değeri döner."""
    for roas_list in roas_lists:
        for item in roas_list or []:
            value = item.get("value")
            if value is not None:
                return float(value)
    return None


def fetch_account_insights(ad_account_id: str, access_token: str, date_preset: str) -> dict:
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/act_{ad_account_id}/insights"
    params = {
        "fields": ",".join(INSIGHTS_FIELDS),
        "date_preset": date_preset,
        "access_token": access_token,
    }
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data", [])
    if not data:
        return {
            "spend": None,
            "content_views": None,
            "add_to_cart": None,
            "purchases": None,
            "purchase_value": None,
            "roas": None,
            "link_clicks": None,
        }

    row = data[0]
    actions = row.get("catalog_segment_actions", [])
    values = row.get("catalog_segment_value", [])

    return {
        "spend": row.get("spend"),
        "content_views": _find_action_value(actions, ["omni_view_content", "view_content"]),
        "add_to_cart": _find_action_value(actions, ["omni_add_to_cart", "add_to_cart"]),
        "purchases": _find_action_value(actions, ["omni_purchase", "purchase"]),
        "purchase_value": _find_action_value(values, ["omni_purchase", "purchase"]),
        "roas": _first_roas(
            row.get("catalog_segment_value_omni_purchase_roas"),
            row.get("catalog_segment_value_website_purchase_roas"),
            row.get("catalog_segment_value_mobile_purchase_roas"),
        ),
        "link_clicks": row.get("inline_link_clicks"),
    }


def open_spreadsheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"], scopes=scopes
    )
    client = gspread.authorize(creds)
    return client.open_by_key(os.environ["GOOGLE_SHEET_ID"])


def get_or_create_worksheet(spreadsheet, brand: str):
    try:
        worksheet = spreadsheet.worksheet(brand)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=brand, rows=100, cols=len(SHEET_HEADER))
    return worksheet


def main():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    spreadsheet = open_spreadsheet()
    total_rows = 0

    for brand, channels in AD_ACCOUNTS.items():
        access_token = resolve_access_token(brand)
        worksheet = get_or_create_worksheet(spreadsheet, brand)
        rows = [SHEET_HEADER]

        for channel, ad_account_id in channels.items():
            for period_label, date_preset in PERIODS:
                print(f"Çekiliyor: {brand} / {channel} / {period_label} ({ad_account_id})...")
                try:
                    metrics = fetch_account_insights(ad_account_id, access_token, date_preset)
                except requests.HTTPError as exc:
                    print(f"  HATA: {exc.response.text}", file=sys.stderr)
                    continue

                rows.append([
                    period_label,
                    channel,
                    f"'{ad_account_id}",  # başına ' koyup bilimsel gösterimi engelliyoruz
                    metrics["spend"],
                    metrics["content_views"],
                    metrics["add_to_cart"],
                    metrics["purchases"],
                    metrics["purchase_value"],
                    metrics["roas"],
                    metrics["link_clicks"],
                    now_str,
                ])

        worksheet.clear()
        worksheet.update(range_name="A1", values=rows, value_input_option="USER_ENTERED")
        total_rows += len(rows) - 1

    print(f"Toplam {total_rows} satır, {len(AD_ACCOUNTS)} marka sekmesine yazıldı (Dün/Son 7 Gün/Bu Ay).")


if __name__ == "__main__":
    main()

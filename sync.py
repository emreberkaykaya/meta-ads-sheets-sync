"""
Meta (Facebook/Instagram) Ads + Shopify -> Google Sheets anlık rapor senkronizasyonu.

İki ayrı rapor besler:

1. CPAS — Aqua di Polo, Frime ve Pierre Cardin markalarının Trendyol/Hepsiburada
   hesaplarındaki "shared items" (collaborative ads / catalog segment) metrikleri.
2. D2C — Frime ve Aqua di Polo'nun kendi web sitesine giden Meta hesabından
   harcama/ROAS/CPC/CTR, Shopify'dan gerçek sipariş ve ciro. ROAS burada Meta'nın
   kendi attribution'ı yerine Meta harcaması ÷ Shopify cirosu olarak hesaplanır —
   Frime'ın pikseli satın alma döndürmediği için Meta'nın kendi ROAS'ı güvenilmez.

Her ikisi de aynı dört sabit dönem için çalışır (Dün / Son 7 Gün / Bu Ay / Geçen Ay).
Her çalıştırma ilgili sekmedeki eski veriyi TEMİZLER ve güncel anlık durumu
yazar — biriken bir log değil, her seferinde tazelenen bir tablo.

Kurulum için README.md dosyasına bakın.

Kullanım:
    python sync.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

GRAPH_API_VERSION = "v26.0"
SHOPIFY_API_VERSION = "2026-01"

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

# Sabit dönemler: (görünen isim, Graph API date_preset, gösterim sırası).
# "Bu Ay" ve "Geçen Ay" birbirini kapsamayan ayrı takvim aralıkları olduğu
# için (nested değiller), metrik büyüklüğüne göre sıralamak kronolojik
# sırayı garanti etmez — bu yüzden açık bir "Sıra" sütunu tutuyoruz ve
# Looker Studio tablolarını buna göre sıralıyoruz.
PERIODS = [
    ("Dün", "yesterday", 1),
    ("Son 7 Gün", "last_7d", 2),
    ("Bu Ay", "this_month", 3),
    ("Geçen Ay", "last_month", 4),
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
    "Sıra",
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

# ---------------------------------------------------------------------------
# D2C: markanın kendi web sitesine giden Meta hesabı + Shopify mağazası.
# Yalnızca Frime ve Aqua di Polo'nun kendi Shopify erişimi var; Pierre Cardin
# D2C raporunda yok.
# ---------------------------------------------------------------------------

D2C_META_ACCOUNTS = {
    "Frime": "2121533615302118",
    "Aqua di Polo": "1215686160473015",
}

# Marka -> (SHOPIFY_ACCESS_TOKEN_ENV_VAR, SHOPIFY_SHOP_ENV_VAR)
D2C_SHOPIFY_ENV_VARS = {
    "Frime": ("SHOPIFY_ACCESS_TOKEN_FRIME", "SHOPIFY_SHOP_FRIME"),
    "Aqua di Polo": ("SHOPIFY_ACCESS_TOKEN_AQUA_DI_POLO", "SHOPIFY_SHOP_AQUA_DI_POLO"),
}

D2C_META_FIELDS = ["reach", "impressions", "clicks", "spend"]

D2C_SHEET_HEADER = [
    "Dönem",
    "Sıra",
    "Marka",
    "Reach",
    "Impressions",
    "Clicks",
    "Amount Spent",
    "Shopify Revenue",
    "Shopify Orders",
    "Son Güncelleme (UTC)",
]

# Meta'nın date_preset'leriyle aynı takvim mantığı, ama Shopify orders.json
# created_at_min/max ile sorgulanabilecek somut tarih sınırları üretir.
# Europe/Istanbul yerel saatiyle hesaplanır ki "Dün" ve "Bu Ay" iki sistemde
# de aynı günü işaret etsin.
try:
    from zoneinfo import ZoneInfo
    ISTANBUL = ZoneInfo("Europe/Istanbul")
except ImportError:  # pragma: no cover
    ISTANBUL = timezone(timedelta(hours=3))


def d2c_period_bounds():
    """PERIODS ile aynı sırayla [(label, since, until, sort_order), ...] döner.
    `since` dahil, `until` hariçtir (yarı açık aralık)."""
    now_ist = datetime.now(ISTANBUL)
    today_start = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    week_start = today_start - timedelta(days=7)
    month_start = today_start.replace(day=1)
    prev_month_end = month_start
    prev_month_start = (month_start - timedelta(days=1)).replace(day=1)

    return [
        ("Dün", yesterday_start, today_start, 1),
        ("Son 7 Gün", week_start, today_start, 2),
        ("Bu Ay", month_start, today_start, 3),
        ("Geçen Ay", prev_month_start, prev_month_end, 4),
    ]


def fetch_d2c_meta_metrics(ad_account_id: str, access_token: str, since: datetime, until: datetime) -> dict:
    """Şimdiki ana kadar süren dönemler (Dün hariç) `until`ı 'bugün' olarak
    Meta'ya gönderemez — Graph API `time_range` gün bazlıdır ve until dahildir.
    Bu yüzden until'dan 1 gün çıkarıp gün bazlı kapsayıcı aralığa çeviriyoruz."""
    since_day = since.date()
    until_day = (until - timedelta(days=1)).date()
    if until_day < since_day:
        until_day = since_day

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/act_{ad_account_id}/insights"
    params = {
        "fields": ",".join(D2C_META_FIELDS),
        "time_range": f'{{"since":"{since_day}","until":"{until_day}"}}',
        "access_token": access_token,
    }
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json().get("data", [])
    if not data:
        return {"reach": None, "impressions": None, "clicks": None, "spend": None}

    row = data[0]
    return {
        "reach": row.get("reach"),
        "impressions": row.get("impressions"),
        "clicks": row.get("clicks"),
        "spend": row.get("spend"),
    }


def fetch_shopify_metrics(shop: str, access_token: str, since: datetime, until: datetime) -> dict:
    """Verilen [since, until) aralığındaki iptal edilmemiş siparişlerin
    toplam cirosunu ve sipariş sayısını döner. Cursor tabanlı sayfalama ile
    (Link header) tüm siparişleri gezer — tek sayfaya güvenmek yüksek hacimli
    dönemlerde (Geçen Ay gibi) ciroyu eksik gösterir."""
    revenue = 0.0
    orders = 0
    url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    params = {
        "status": "any",
        "created_at_min": since.isoformat(),
        "created_at_max": (until - timedelta(seconds=1)).isoformat(),
        "fields": "total_price,cancelled_at",
        "limit": 250,
    }
    headers = {"X-Shopify-Access-Token": access_token}

    while url:
        response = requests.get(url, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        for order in response.json().get("orders", []):
            if order.get("cancelled_at"):
                continue
            revenue += float(order["total_price"])
            orders += 1

        next_url = None
        link_header = response.headers.get("Link", "")
        for part in link_header.split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
        url = next_url
        params = None  # sonraki sayfanın URL'sinde parametreler zaten var

    return {
        "orders": orders,
        "revenue": round(revenue, 2),
        "aov": round(revenue / orders, 2) if orders else None,
    }


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


def get_or_create_worksheet(spreadsheet, title: str, header: list[str] = SHEET_HEADER):
    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=100, cols=len(header))
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
            for period_label, date_preset, sort_order in PERIODS:
                print(f"Çekiliyor: {brand} / {channel} / {period_label} ({ad_account_id})...")
                try:
                    metrics = fetch_account_insights(ad_account_id, access_token, date_preset)
                except requests.HTTPError as exc:
                    print(f"  HATA: {exc.response.text}", file=sys.stderr)
                    continue

                rows.append([
                    period_label,
                    sort_order,
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

    print(f"CPAS: {total_rows} satır, {len(AD_ACCOUNTS)} marka sekmesine yazıldı (Dün/Son 7 Gün/Bu Ay/Geçen Ay).")

    sync_d2c(spreadsheet, now_str)


def resolve_d2c_meta_token(brand: str, ad_account_id: str) -> str:
    """CPAS için verilen marka-özel System User token'ının D2C (web sitesi)
    hesabına erişimi olmayabilir — CPAS ve D2C farklı Business Manager
    varlıkları olabilir. Marka token'ını dener, erişim reddedilirse
    (OAuthException #200) genel META_ACCESS_TOKEN'a düşer."""
    brand_token = resolve_access_token(brand)
    probe = requests.get(
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/act_{ad_account_id}/insights",
        params={"fields": "spend", "date_preset": "yesterday", "access_token": brand_token},
        timeout=15,
    )
    if probe.status_code == 200:
        return brand_token

    general_token = os.environ.get("META_ACCESS_TOKEN")
    if general_token and general_token != brand_token:
        print(f"  Not: {brand} marka token'ı D2C hesabına erişemiyor, genel token'a düşülüyor.")
        return general_token
    return brand_token


def sync_d2c(spreadsheet, now_str: str) -> None:
    worksheet = get_or_create_worksheet(spreadsheet, "D2C", header=D2C_SHEET_HEADER)
    rows = [D2C_SHEET_HEADER]
    periods = d2c_period_bounds()

    for brand, ad_account_id in D2C_META_ACCOUNTS.items():
        access_token = resolve_d2c_meta_token(brand, ad_account_id)
        token_var, shop_var = D2C_SHOPIFY_ENV_VARS[brand]
        shopify_token = os.environ.get(token_var)
        shopify_shop = os.environ.get(shop_var)
        if not shopify_token or not shopify_shop:
            print(f"  ATLANDI: {brand} için Shopify erişimi ({token_var}) tanımlı değil.", file=sys.stderr)
            continue

        for period_label, since, until, sort_order in periods:
            print(f"Çekiliyor (D2C): {brand} / {period_label}...")
            try:
                meta = fetch_d2c_meta_metrics(ad_account_id, access_token, since, until)
            except requests.HTTPError as exc:
                print(f"  HATA (Meta): {exc.response.text}", file=sys.stderr)
                continue
            try:
                shop = fetch_shopify_metrics(shopify_shop, shopify_token, since, until)
            except requests.HTTPError as exc:
                print(f"  HATA (Shopify): {exc.response.text}", file=sys.stderr)
                continue

            rows.append([
                period_label,
                sort_order,
                brand,
                meta["reach"],
                meta["impressions"],
                meta["clicks"],
                meta["spend"],
                shop["revenue"],
                shop["orders"],
                now_str,
            ])

    worksheet.clear()
    worksheet.update(range_name="A1", values=rows, value_input_option="USER_ENTERED")
    print(f"D2C: {len(rows) - 1} satır, D2C sekmesine yazıldı.")


if __name__ == "__main__":
    main()

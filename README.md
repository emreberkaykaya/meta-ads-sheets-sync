# Meta Ads → Google Sheets Anlık Rapor Senkronizasyonu

Aqua di Polo, Frime, Pierre Cardin markalarının Trendyol/Hepsiburada CPAS
hesaplarındaki "shared items" metriklerini (Purchases, Content Views,
Adds to Cart, Purchase ROAS with shared items + Amount Spent, Link Clicks)
üç sabit dönem için (**Dün / Son 7 Gün / Bu Ay**) çekip her markanın kendi
Google Sheet sekmesine yazar.

Her çalıştırma sekmeyi temizler ve güncel anlık durumu yazar — biriken
bir log değil, her seferinde tazelenen bir tablo.

## 0) Kurulum

```bash
cd /Users/ebk/Downloads/meta-ads-sheets-sync
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env` dosyasını doldurun (App ID, marka bazlı System User token'ları,
Google Sheets bilgileri). Bu dosyayı kimseyle paylaşmayın.

## 1) Token'lar — süresiz System User token'ları

Her marka için Business Manager'da bir System User oluşturulup
`ads_read` + `business_management` izniyle **süresiz (never-expiring)**
token üretildi:

- `META_ACCESS_TOKEN_AQUA_DI_POLO`
- `META_ACCESS_TOKEN_FRIME`
- `META_ACCESS_TOKEN_PIERRE_CARDIN`

Bu token'lar süresi dolmaz, yenileme gerekmez. Yeni bir marka eklenirse
aynı adımlar tekrarlanır: ilgili Business Manager → System users → Add →
ad hesaplarını "View performance" ile ata → App'i (Reklam Raporu
Entegrasyonu) o business'a bağla ve System User'a "View insights" ver →
Generate token → expiry "Never" → izinler `ads_read` + `business_management`.

## 2) Google Sheets bağlantısı

1. Google Cloud Console'da bir proje seçin, **Google Sheets API**'yi açın.
2. Bir **Service Account** oluşturup JSON key indirin, dosyayı
   `service-account.json` olarak bu klasöre kaydedin.
3. Google Sheet'i oluşturup ID'sini `.env` içindeki `GOOGLE_SHEET_ID`'ye
   yazın.
4. Sheet'i, service account JSON'daki `client_email` adresine **Editör**
   olarak paylaşın.

## 3) Manuel çalıştırma

```bash
source venv/bin/activate
python sync.py
```

Her çalıştırmada her marka sekmesi temizlenip Dün/Son 7 Gün/Bu Ay için
Trendyol + Hepsiburada satırlarıyla yeniden yazılır. Tarih parametresi
yok — üç dönem sabit ve otomatik hesaplanıyor.

## 4) Otomatik/zamanlanmış çalıştırma (GitHub Actions — ücretsiz)

`.github/workflows/daily-sync.yml` her gün 06:00 UTC'de (09:00 Türkiye
saati) otomatik çalışır, ayrıca Actions sekmesinden elle de
tetiklenebilir ("Run workflow").

Repo Settings → Secrets and variables → Actions'a şu secret'lar eklendi:
- `META_ACCESS_TOKEN_AQUA_DI_POLO`
- `META_ACCESS_TOKEN_FRIME`
- `META_ACCESS_TOKEN_PIERRE_CARDIN`
- `GOOGLE_SHEET_ID`
- `GOOGLE_SERVICE_ACCOUNT_JSON` (service-account.json dosyasının tüm içeriği)

## Notlar

- `catalog_segment_actions` / `catalog_segment_value` alanları resmi Meta
  Ads MCP sunucusunda (mcp.facebook.com/ads) henüz desteklenmiyor, bu
  yüzden bu script doğrudan Facebook Graph API'ye bağlanıyor.
- Token'lar süresiz olduğu için normal şartlarda bakım gerekmez. Meta
  tarafında System User veya App erişimi manuel olarak iptal edilirse
  script `401` hatası verir — o durumda ilgili marka için 1. adım
  tekrarlanır.

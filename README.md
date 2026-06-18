# ahenk-debug

**Lider tarafında çevrimdışı görünen veya uzak komut kabul etmeyen Ahenk
istemcilerini yerinde teşhis eden tek dosyalık araç.**

`ahenk_debug.py`, bir Pardus ETAP etkileşimli tahtasında Ahenk'in kayıt ve
bağlanma zincirini baştan sona inceler; arızayı **kimlik/klon çakışması**,
**ağ/bağlantı** ve **yerel yazılım** sınıflarına ayırır; ayrıca tahtanın hangi
**okul/şehir/ilçe** adına kayıtlı olduğunu ETA API'sinden sorgular. Hiçbir harici
Python paketine ihtiyaç duymaz (yalnızca standart kütüphane + sistemde hazır
bulunan `psutil`/`ss`). Raporu, çalıştığı anda operatöre **otomatik** ulaştırır
(paste.rs + ntfy.sh; bkz. [Çıktıyı operatöre gönderme](#çıktıyı-operatöre-gönderme)).

İndirme/kurulum gerekmeden, doğrudan depodan boru hattıyla çalıştırın (root ister):

```bash
curl -fsSL https://raw.githubusercontent.com/enseitankado/ahenk-debug/main/run.sh | sudo bash
```

---

## İçindekiler

1. [Hızlı başlangıç](#hızlı-başlangıç)
2. [Komut satırı seçenekleri](#komut-satırı-seçenekleri)
3. [Çıkış kodları](#çıkış-kodları)
4. [Arka plan: Ahenk kayıt & bağlantı mimarisi](#arka-plan-ahenk-kayıt--bağlantı-mimarisi)
5. [Rapor bölümleri](#rapor-bölümleri)
6. [Canlı bağlantı doğrulaması](#canlı-bağlantı-doğrulaması-bayat-log--güncel-hata)
7. [ETA Kayıt Sunucusu (eta-register API)](#eta-kayıt-sunucusu-eta-register-api)
8. [Çıktıyı operatöre gönderme](#çıktıyı-operatöre-gönderme)
9. [Faz (Faz 1/2/3) tespiti](#faz-faz-123-tespiti)
10. [Arıza sınıflarını ayırt etme](#arıza-sınıflarını-ayırt-etme)
11. [Sık görülen senaryolar ve çözümleri](#sık-görülen-senaryolar-ve-çözümleri)
12. [JSON çıktısı](#json-çıktısı)
13. [Gereksinimler ve sınırlar](#gereksinimler-ve-sınırlar)
14. [Veri kaynakları](#veri-kaynakları)
15. [Gizlilik ve güvenlik](#gizlilik-ve-güvenlik)

---

## Hızlı başlangıç

```bash
# Tam, okunabilir rapor  (ROOT ZORUNLU)
sudo ./ahenk_debug.py

# Başka seçenekler
sudo ./ahenk_debug.py --json                    # JSON çıktı (betikler için)
sudo ./ahenk_debug.py --no-net                  # ağ/API/aktif testleri atla (hızlı)
sudo ./ahenk_debug.py --out rapor.txt           # raporu dosyaya da yaz
sudo ./ahenk_debug.py --mac AA:BB:CC:DD:EE:FF    # başka bir tahtayı MAC ile sorgula
```

> **Root zorunludur.** Araç, root olmadan çalıştırılırsa bir uyarı basıp **çıkar**
> (çıkış kodu `3`). Sebep: `/etc/ahenk/ahenk.conf` ve
> `/etc/ahenk/config.d/messaging.conf` dosyaları `0600`'dür (yalnız root) ve
> UID/parola, broker adresi, TLS sertifika yolu gibi kritik alanları içerir;
> ayrıca aktif Pulsar testi ve süreç soketleri de root yetkisi ister.

Araç, yapılandırma okuma açısından salt-okurdur; hiçbir Ahenk ayarını veya
servisini değiştirmez. Tek istisna **aktif Pulsar testidir** (varsayılan olarak
çalışır): Ahenk'in açılışta zaten yaptığı gibi `test-topic-lider`'e bir test
mesajı yollar, komut aboneliğine dokunmaz.

---

## Komut satırı seçenekleri

| Seçenek | Açıklama |
|---|---|
| _(yok)_ | Renkli, bölümlü tam rapor + sonda Özet/Teşhis |
| `--json` | Tüm ham veriyi ve bulguları JSON olarak basar (betiklerle işlenebilir) |
| `--no-net` | Ağ testlerini (DNS/TCP/TLS, ETA API) atlar; yalnız yerel kontroller. Canlı TCP soketi kontrolü yine de çalışır (yerel) |
| `--out DOSYA` | Raporu (renksiz) belirtilen dosyaya da yazar |
| `--db YOL` | `ahenk.db` yolunu elle verir (varsayılan: conf'taki `BASE/dbPath`) |
| `--mac MAC` | ETA API'de **bu makinenin değil**, verilen MAC'i sorgular (uzaktaki bir tahtanın kaydını kontrol için) |
| `--no-send` | Otomatik gönderimi kapatır (varsayılan: rapor paste.rs+ntfy.sh ile operatöre **otomatik** gönderilir; bkz. [Çıktıyı operatöre gönderme](#çıktıyı-operatöre-gönderme)) |
| `--send-to KONU` | Gönderim için ntfy konusu/URL'si (varsayılan: gömülü konu; `AHENK_DEBUG_NTFY` ortam değişkeniyle de verilebilir) |

> **Aktif Pulsar testi** her çalıştırmada otomatik yapılır; yalnızca `--no-net`
> ile veya messenger Pulsar değilse atlanır.

---

## Çıkış kodları

| Kod | Anlamı |
|---|---|
| `0` | Kritik veya uyarı düzeyi bulgu yok |
| `1` | En az bir **WARN** var, **FAIL** yok |
| `2` | En az bir **FAIL** (kritik) bulgu var |
| `3` | Root değil — araç çalışmadan çıktı (sudo ile yeniden deneyin) |

Bu sayede araç izleme/otomasyon içinde de kullanılabilir.

---

## Arka plan: Ahenk kayıt & bağlantı mimarisi

Aracın neye baktığını anlamak için Ahenk'in kayıt ve bağlanma akışını bilmek
yeterli. (Ahenk 2.0.10 için geçerlidir.)

### Kimlik

- Kayıt sırasında Ahenk, rastgele bir **`uuid4` JID** ve rastgele bir **`uuid4`
  parola** üretir; ikisini de hem `ahenk.db` (`registration` tablosu) hem
  `ahenk.conf` `[CONNECTION]` bölümünde saklar. Tekillik, Lider/ETA tarafında
  tahtanın MAC'i üzerinden kurulur.
- Lider tarafı tahtayı **kablolu ethernet MAC'i** ile tanır. Bu MAC, **ilk PCI
  veri yolu, sürücüye bağlı, kablosuz olmayan** arayüzden alınır. Uygun bir
  kablolu arayüz yoksa kayıt **tamamlanamaz**.

### Mesajlaşma (Pulsar)

- İstemci, `task-<uuid>` adlı topic'e `ahenk-<uuid>` abonelik adıyla ve
  **`ConsumerType.Exclusive`** ile bağlanır.
- **Exclusive** olması kritiktir: aynı UUID iki makinede kullanılırsa (klon imaj)
  ikinci istemci broker'dan **`ConsumerBusy`** hatası alır, komut topic'ini
  dinleyemez → Lider'de **çevrimdışı/yanıtsız** görünür.
- Bağlantı kurulurken Ahenk önce `test-topic-lider`'e bir test mesajı yollayarak
  kendini sınar; araç da aynı testi yaparak bağlantıyı canlı olarak doğrular.

### İki aşamalı kayıt zinciri

```
1) Tahta  ──(MAC + okul/il/ilçe)──►  ETA API (eta-register)
2) Ahenk  ──(MAC/UUID)──►  Lider  ──(MAC kayıtlı mı?)──►  ETA API
                                   └─ kayıtlı DEĞİLSE → kayıt İLERLEMEZ
```

Yani tahta önce `eta-register` aracıyla okul/il/ilçe seçilerek ETA API'sine
kaydedilir. Ahenk Lider'e bağlandığında Lider arka planda ETA API'ye "bu MAC
kayıtlı mı?" diye sorar. Kayıtlı değilse süreç durur ve tahta çevrimdışı kalır.

---

## Rapor bölümleri

### 1) Genel Durum, Servis ve Yerel Sağlık
Ahenk paket sürümü, `ahenk.service` aktif/etkin durumu ve başlangıç zamanı.
Ayrıca Ahenk'i **yerelde sekteye uğratan** sorunlar:
- **Servis crash-loop** — `NRestarts` sayısı. Sürekli yeniden başlıyorsa
  (≥3) başlangıçta ölüyor demektir (FAIL).
- **Disk doluluğu** (`/` ve `/var`) — dolu disk SQLite DB ve log yazımını
  engeller, kaydı bozar (≥%90 WARN, ≥%95 FAIL).
- **Mevcut oturum hataları** — servisin son başlangıcından (ActiveEnter) bu yana
  loglardaki ERROR/Traceback/ImportError satırları (ConsumerBusy hariç). Başlangıç/
  bağımlılık çökmesi varsa FAIL.

### 2) Kimlik (UUID / Parola / Kayıt)
DB'deki UUID (jid), parola (maskeli), `registered` bayrağı, kayıt zaman damgası,
**DB bütünlük kontrolü** (`PRAGMA integrity_check` — bozuk/kilitli DB Ahenk'i
durdurur). Root ile: **`ahenk.conf` UID/parola ile DB'nin tutarlılığı** ve Pulsar
topic/abonelik adları.

**Kayıt türü** (`dn` alanına göre):
- **Tam kayıt** — `dn` dolu: Lider, ajan için LDAP dizininde bir nesne (DN)
  oluşturmuş; dizin-tabanlı politikalar uygulanabilir → **OK**.
- **`registered_without_ldap`** — `registered=1` ama `dn` boş: Lider kayıt
  yanıtında boş `agentDn` döndürmüş. Mesajlaşma/komut/görev **çalışır**, fakat
  **LDAP dizin ağacına bağlı politikalar uygulanmaz** → **WARN**. Neden Lider
  tarafıdır (kayıt anında LDAP'a erişememe, agent-OU yapılandırması veya bilinçli
  LDAP'sız kurulum); bu Ahenk sürümünde kayıt sonrası LDAP tamamlama adımı
  yorum satırı olduğundan `dn` kendiliğinden dolmaz. Bulgu, Lider'de ajanın LDAP
  ağacında görünüp görünmediğini kontrol etmeyi önerir.
- **Kayıt tamamlanmamış** — `registered=0` ve `dn` boş: tahta Lider'e hiç
  kaydolamamış olabilir → **FAIL**.

### 3) Kimlik MAC'i, Klon/Çakışma ve Canlı Bağlantı
- Tüm ağ arayüzleri (MAC, sürücü, veri yolu, kablosuz/kablolu, durum).
- Seçilen **kimlik MAC'i** ve `etainfo.network`'ün gerçek sonucu (çökerse yakalar
  — bu başlı başına bir teşhistir).
- **Klon tespiti:** kayıt anındaki MAC (DB `params`) ile **canlı donanım MAC**
  karşılaştırması. Farklıysa imaj başka donanıma kopyalanmış olabilir.
- **Canlı bağlantı doğrulaması** (aşağıdaki bölüme bakın).

### 4) Bağlantı, Temel TCP ve DNS
Önce **temel ağ katmanı** (root gerektirmeden, her zaman):
- **Kablolu arayüz linki** — kimlik arayüzünün `carrier`/`operstate`/IPv4/hız
  durumu. Kablo yoksa (carrier=0) veya IP yoksa hiçbir sunucuya ulaşılamaz (FAIL).
- **Varsayılan ağ geçidi** ve **ağ geçidine ping** (L3 ulaşılabilirlik).
- **DNS sunucuları** (`resolv.conf`) + broker adının çözümü.
- **Broker'a DOĞRUDAN TCP testi** (gecikme ölçümlü) — config root ile okunabiliyorsa
  oradan, okunamıyorsa **canlı soketlerden keşfedilen** broker uç noktasına bağlanır.
  Ahenk bu porta TCP ile bağlanamazsa Lider'e subscribe **olamaz**.

Sonra (root + ağ ile) derin testler: Pulsar/XMPP için **DNS → TCP → TLS el
sıkışması → sertifika bitişi**; kayıt (register) ucu erişimi; logdaki son
başarılı yayın ve son hata.

### 5) ETA Kayıt Sunucusu — Okul / Şehir / İlçe
Tahtanın MAC'inin **hangi şehir / ilçe / okul / birim** adına kayıtlı olduğunu
ETA API'sinden çeker: `school_name`, `city_name`, `town_name`, `school_code`,
`unit_name`, `board_id` ve API'nin tuttuğu faz bilgisi. (Aşağıdaki bölüme bakın.)

### 6) Sistem / Dağıtım / Çekirdek
Dağıtım ve alt sürüm (ör. *Pardus ETAP GNU/Linux 23*), `lsb_release`, çekirdek
sürümü, mimari, hostname, **NTP saat senkronu** (saat kayması TLS doğrulamasını
ve zaman damgalarını bozar).

### 7) Donanım, Faz ve Dokunmatik
İşlemci, anakart (üretici/model), BIOS, RAM, GPU(lar) + sürücü; **Faz 1/2/3
tahmini** (yerel) ve ETA API faz bilgisi; **dokunmatik donanımı + sürücüsü**
(`/proc/bus/input/devices` + USB kimliği + bağlı çekirdek sürücüsü).

### Özet / Teşhis
Tüm bulguları **önem sırasına** göre (FAIL → WARN → OK → INFO), nedeniyle ve
önerilen çözümüyle listeler.

---

## Canlı bağlantı doğrulaması (bayat log ≠ güncel hata)

Loglardaki `ConsumerBusy` kayıtları **eski bir oturuma ait olup artık geçerli
olmayabilir.** Bu yüzden araç statik log sayımıyla yetinmez; hatanın **güncel mi
yoksa bayat mı** olduğunu birden çok kanıtla belirler:

1. **Servis yeniden başlama korelasyonu** — `ahenk.service` başlangıç zamanı ile
   son `ConsumerBusy` zaman damgası karşılaştırılır. Hata, mevcut oturumdan
   önceyse **bayat** sayılır.
2. **Sonraki başarı olayı** — `ConsumerBusy`'den sonra logda "Connected to
   Pulsar" / "Message published" / mesaj alımı varsa, sorun **çözülmüş**
   demektir. (Loglar `ahenk.log` + `ahenk.log.1` kronolojik sırayla birleştirilir.)
3. **Canlı TCP soketi** — `ss` (veya root ile psutil) kullanılarak Ahenk
   sürecinin broker'a (Pulsar `6650/6651`, XMPP `5222/5223`) o anki
   **ESTABLISHED** bağlantıları sayılır. Varsa, makine komut topic'ine bağlı
   demektir. Bu kontrol yereldir, `--no-net` ile bile çalışır.
4. **Aktif Pulsar testi** — Ahenk'in açılışta yaptığı bağlantı testini birebir
   yapar: **gerçek uid/parola** ile broker'a bağlanıp `test-topic-lider`'e bir
   test mesajı yollar. Böylece DNS + TCP + TLS + **kimlik doğrulamanın** o an
   çalıştığı kanıtlanır. Komut aboneliğine (`ahenk-<uid>`) **dokunmaz**,
   dolayısıyla çalışan Ahenk'i etkilemez. (`--no-net` ile atlanır.)

**Karar tablosu:**

| Durum | Bulgu |
|---|---|
| ConsumerBusy **bayat** + bağlantı **canlı** | **OK** — "Geçmiş ConsumerBusy güncel değil, bağlantı sağlıklı" |
| ConsumerBusy **güncel** + sonrasında başarı yok | **FAIL** — "Aktif klon/çakışma sürüyor" |
| ConsumerBusy var ama canlılık **doğrulanamadı** | **WARN** — ağ erişimiyle (--no-net'siz) tekrar çalıştırın |

---

## ETA Kayıt Sunucusu (eta-register API)

Araç, tahtanın okul kaydını `eta-register`'ın kullandığı ETA API'sinden **salt-okur**
sorgular — cihazın her açılışta yaptığı sorgunun aynısını yapar.

**Asıl sorgu:**

```
GET {BACKEND_URL}/board/check?mac=<mac>
Header: etap-app-code: eta_register!
```

`BACKEND_URL` ve gizli header, kuruluysa `eta-register`'ın kendi
`config.py`'sinden okunur (üretim varsayılanı:
`http://api-etap.eba.gov.tr:1000/api`). **Kayıtlı** bir MAC için örnek yanıt:

```json
{
  "msg": "Success",
  "registered": true,
  "registered_ip": true,
  "data": {
    "school_code": 123456,
    "school_name": "Örnek Mesleki ve Teknik Anadolu Lisesi",
    "city_id": 1,   "city_name": "ÖRNEK İL",
    "town_id": 10,  "town_name": "ÖRNEK İLÇE",
    "board_id": 9999,
    "unit_name": "Sınıf-1",
    "phase": "4. Phase"
  }
}
```

> Yukarıdaki değerler örnektir; gerçek alanlar tahtanın kayıtlı olduğu okula göre döner.

**Kayıtsız** MAC için: `{"registered": false, "data": null}` → araç bunu **FAIL**
olarak işaretler, çünkü Lider bu MAC için kaydı reddeder.

ETA API'nin ilgili diğer uçları (referans):

| Uç | İşlev |
|---|---|
| `GET /board/check?mac=<mac>` | MAC kayıtlı mı + okul/il/ilçe bilgisi |
| `GET /city` | İl listesi |
| `GET /town/id/{city_id}` | İlçe listesi |
| `GET /school/no-limit/{city_id}/{town_id}` | Okul listesi |
| `GET /school/code/{code}` | Okul kodu doğrulama |
| `POST /board` | Tahta kaydı (city_id, town_id, school_code, mac_id, donanım, unit_name) |
| `POST /board/update` | Tahta kaydı güncelleme |

---

## Çıktıyı operatöre gönderme

Araç, raporu **her çalışmada otomatik** olarak operatöre ulaştırır — kullanıcının
ekran çıktısını seçip kopyalayıp dosyaya kaydedip mail/WhatsApp ile göndermesine
gerek kalmaz. Hiçbir sunucu kurulmaz; iki ücretsiz servis kullanılır:

- **[paste.rs](https://paste.rs)** — tam rapor metni yüklenir, kısa bir URL üretilir.
- **[ntfy.sh](https://ntfy.sh)** — özet + rapor linki, operatörün bir **konusuna**
  (topic) anlık bildirim olarak itilir.

Yani normal çalıştırmada (`sudo ./ahenk_debug.py` veya kurulumsuz `curl … | sudo bash`)
gönderim kendiliğinden yapılır. Otomatik göndermeyi **kapatmak** için `--no-send`
kullanın (örn. yalnız yerel inceleme); `--no-net` verildiğinde de gönderim atlanır.

Araç çalışınca:
1. Raporu paste.rs'e yükler → `https://paste.rs/xxxx`.
2. ntfy konusuna şu içerikte bildirim iter (tıklanabilir link, önem önceliğiyle):
   ```
   Başlık: ahenk: <hostname>
   host: <hostname> | MAC: <kimlik MAC>
   okul: <il> / <ilçe> / <okul>
   servis: active | canlı TCP: 3 | kayıt: ...
   BULGU: 1 FAIL, 2 WARN
   · [FAIL] ...
   Tam rapor: https://paste.rs/xxxx
   ```

### Raporları telefondan okuma (tek seferlik kurulum)

1. Telefona **ntfy** uygulamasını kurun (Android: Play Store / F-Droid, iOS: App Store)
   **veya** bilgisayarda `https://ntfy.sh/ahenk-debug` adresini açın.
2. Uygulamada **"+ Abone ol / Subscribe to topic"** deyip şu konuyu girin:
   ```
   ahenk-debug
   ```
   (Sunucu: `ntfy.sh` — varsayılan.)
3. Artık çalışan her tahtanın özeti + rapor linki anlık bildirim olarak düşer.
   FAIL varsa **urgent**, WARN varsa **high** önceliğiyle gelir.
4. Bildirime dokunun → özet açılır; içindeki **paste.rs linkine** dokunun → tam
   raporu tarayıcıda okuyun. (Web arayüzünde de aynı konu listelenir.)

Konuyu değiştirmek için: `--send-to <konu>` veya `AHENK_DEBUG_NTFY=<konu>` ortam
değişkeni.

> **Gizlilik.** Rapor; okul adı, MAC, IP gibi tanımlayıcı bilgi içerir. `ahenk-debug`
> herkese açık ve kolay tahmin edilen bir konudur; bilen herkes abone olup gelen
> raporları görebilir ya da konuya çöp mesaj gönderebilir. Hassas kullanımda kendi
> rastgele konunuzu (`--send-to <rastgele-konu>`) belirleyin. `paste.rs` linkleri
> tahmin edilemez ama içerik üçüncü tarafta barınır.

---

## Faz (Faz 1/2/3) tespiti

ETAP tahtaları donanım kuşağına göre fazlara ayrılır. Araç, işlemci markası ve
anakart üreticisinden **yerel** bir tahmin üretir ve mümkünse ETA API'nin faz
bilgisiyle yan yana gösterir:

| İşlemci | Faz |
|---|---|
| Intel i3-2330M | Faz 1 (VESTEL) |
| Intel i3-3120M | Faz 2 Kısım 1 (INTEL/VESTEL) |
| AMD A10-5750M | Faz 2 Kısım 1 (AMD/VESTEL) |
| Intel i3-4000M | Faz 2 Kısım 2 (VESTEL) |
| Intel i3-8100T | Faz 3 (anakart GIGABYTE ise → ARÇELİK) |

Tabloda olmayan işlemcilerde "Bilinmiyor (manuel kontrol)" denir; donanım
bilgileri yine de raporlanır. **Dokunmatik donanım** `/proc/bus/input/devices`
üzerinden adı, USB kimliği (`vendor:product`) ve bağlı çekirdek sürücüsüyle
listelenir.

---

## Arıza sınıflarını ayırt etme

| Belirti | Bölüm | Olası neden |
|---|---|---|
| `ConsumerBusy` **güncel** (servis restart sonrası, sonrasında başarı yok) | 3 | **Aktif klon imaj** — aynı UUID başka makinede |
| `ConsumerBusy` var ama **bayat** (canlı TCP + sonraki başarı) | 3 | Geçmiş sorun, **şu an sağlıklı** (FAIL değil) |
| Kayıt MAC ≠ canlı MAC | 3 | İmaj farklı donanıma kopyalanmış |
| Kimlik MAC'i belirlenemiyor / `etainfo` hatası | 3 | Kablolu ethernet/sürücü yok → kayıt çöker |
| Kablolu arayüzde carrier/IP yok | 4 | **Fiziksel/DHCP** sorunu — hiçbir sunucuya ulaşılamaz |
| Broker portuna TCP açılamıyor | 4 | **Güvenlik duvarı / yanlış adres / sunucu kapalı** → subscribe olamaz |
| DNS sunucusu yok / ad çözülemiyor | 4 | **DNS** sorunu — broker adı IP'ye çevrilemez |
| DNS/TCP/TLS başarısız, ağ geçidi yok | 4 | **Ağ/bağlantı** sorunu |
| ETA API'de `registered:false` | 5 | Tahta okul/il/ilçe ile **hiç kaydedilmemiş** → Lider reddeder |
| `registered=1` ama `dn` boş | 2 | **registered_without_ldap** — mesajlaşma çalışır, LDAP politikaları uygulanmaz (Lider tarafı) |
| `registered=0` ve `dn` boş | 2 | Kayıt **hiç tamamlanmamış** |
| Servis pasif / crash-loop, conf↔db uyuşmazlığı | 1–2 | **Yerel yazılım** sorunu |
| Disk dolu, DB bütünlüğü bozuk | 1–2 | **Yerel kaynak** sorunu — DB/log yazımı bozulur |
| Mevcut oturumda ImportError/Traceback | 1 | **Bağımlılık/başlangıç** çökmesi |
| Saat NTP ile senkron değil | 6 | TLS/sertifika ve zaman damgası hataları |

---

## Sık görülen senaryolar ve çözümleri

### A) Klon imaj / UUID çakışması (aktif ConsumerBusy)
Aynı UUID birden çok makinede. Çakışan makinede kimliği sıfırlayıp yeniden
kaydedin (yeni `uuid4` üretilir):

```bash
sudo systemctl stop ahenk.service
sudo /usr/bin/python3 /usr/share/ahenk/ahenkd.py clean   # uid/parola/DB temizler
sudo systemctl start ahenk.service                       # yeni UUID ile yeniden kayıt
```

Ardından aracı tekrar çalıştırıp `ConsumerBusy`'nin durduğunu ve canlı bağlantının
kurulduğunu doğrulayın.

### B) Tahta ETA API'de kayıtlı değil
`eta-register` aracını (etapadmin kullanıcısıyla) açıp doğru **il/ilçe/okul** ile
tahtayı kaydedin. Kayıttan sonra `--mac` ile veya doğrudan araçla `registered:true`
olduğunu doğrulayın.

### C) Ağ/bağlantı sorunu
Bölüm 4'teki DNS/TCP/TLS satırlarına bakın. Güvenlik duvarı, yanlış broker
adresi, süresi dolmuş/eksik TLS sertifikası veya kopuk ağ geçidi olabilir.
Aktif Pulsar testi (otomatik) kimlik doğrulamanın da çalışıp çalışmadığını gösterir.

### D) Kimlik MAC'i çözülemiyor
Tahtada **kablolu ethernet** sürücüsü yüklü ve arayüz mevcut olmalı. Yalnız
USB/WiFi adaptör varsa `etainfo.network.get()` `None` döner ve kayıt çöker.
Sürücüyü (ör. `r8169`) ve kablo bağlantısını kontrol edin.

---

## JSON çıktısı

`--json` ile tüm ham veri ve bulgular tek bir JSON nesnesi olarak döner. Başlıca
anahtarlar:

| Anahtar | İçerik |
|---|---|
| `ahenk_version`, `service_active`, `service_enabled` | Servis durumu |
| `service_nrestarts`, `service_result` | Crash-loop / son sonuç |
| `disk`, `session_errors` | Disk doluluğu / mevcut oturum hataları |
| `uuid`, `registered`, `dn`, `db.integrity` | Kimlik & DB bütünlüğü |
| `net_basics` | `{link, nameservers, broker_targets[], broker_tcp_open}` (temel TCP/DNS) |
| `nics`, `identity_mac`, `live_mac`, `registered_mac`, `etainfo` | Ağ kimliği & klon |
| `live_connection` | `{host, port, ips, established[], count, pid, owned_by_ahenk, method}` |
| `logs` | Her olay için `{count, last, last_dt}` |
| `active_probe` | aktif Pulsar testi sonucu `{ok, auth_ok, stage, error}` |
| `messenger_type`, `pulsar`/`xmpp` | Bağlantı yapılandırması |
| `eta_api`, `school` | ETA kayıt sorgusu sonucu ve okul/il/ilçe |
| `send` | otomatik gönderim sonucu `{paste_url, ntfy_topic, paste_ok, ntfy_ok}` |
| `distro`, `kernel`, `arch` | Sistem |
| `cpu`, `board`, `bios`, `memory`, `gpu`, `phase`, `touch` | Donanım & faz |
| `findings` | `[{severity, title, detail}]` özet bulgular |

---

## Gereksinimler ve sınırlar

- **Root zorunludur.** Root olmadan araç bir uyarı basıp çıkar (kod `3`).
- **Python 3** (yalnızca standart kütüphane). `psutil` ve `ss` varsa kullanılır,
  yoksa zarifçe atlanır.
- Aktif Pulsar testi için Ahenk'in paketlediği Pulsar istemci kütüphanesi
  (`/usr/share/ahenk/base/messaging/pulsar/pulsar_client_libs`) kullanılır;
  yüklenemezse o test atlanır, diğer teşhisler sürer.
- Sanal makine, farklı dağıtım alt sürümü, farklı çekirdek veya farklı donanım
  (faz) bulunan makinelerde de çalışacak şekilde sysfs/komut çıktıları savunmacı
  biçimde okunur.

---

## Veri kaynakları

| Kaynak | Kullanım |
|---|---|
| `/etc/ahenk/ahenk.conf`, `config.d/*.conf` | UID/parola, broker, TLS, messenger_type |
| `/etc/ahenk/ahenk.db` (salt-okur) | registration & messaging tabloları |
| `/var/log/ahenk.log[.1]` | Olay zaman çizelgesi (ConsumerBusy, bağlantı vb.) |
| `/sys/class/net`, `/sys/devices/.../dmi/id`, `/sys/bus/pci` | NIC, anakart/BIOS, GPU |
| `/proc/cpuinfo`, `/proc/meminfo`, `/proc/bus/input/devices` | CPU, RAM, dokunmatik |
| `systemctl`, `ss`, `lsb_release`, `timedatectl`, `openssl`, `ip` | Servis/ağ/sistem |
| `etainfo.network` (içe aktarılır) | Ahenk'in kullandığı kimlik MAC'ini birebir üretir |
| ETA API `…/board/check?mac=` | Okul/il/ilçe kayıt durumu |

---

## Gizlilik ve güvenlik

- Parolalar raporda **maskelenir** (`e2a2…8df5`); tam parola hiçbir yerde açıkça
  gösterilmez.
- Araç, Ahenk yapılandırmasını ve servisini **değiştirmez** (salt-okur).
- Aktif Pulsar testi, broker'a yalnızca **tek bir test mesajı** yollar (Ahenk'in
  açılışta zaten yaptığı işlem) ve komut aboneliğine dokunmaz.
- ETA API sorgusu, cihazın kendi MAC'i ile yapılan, cihazın her açılışta yaptığı
  salt-okur bir sorgudur.
- Otomatik gönderim açıkken rapor; `paste.rs` ve `ntfy.sh` üçüncü taraf
  servislerine gider (bkz. [Çıktıyı operatöre gönderme](#çıktıyı-operatöre-gönderme)).
  İstemiyorsanız `--no-send` ile kapatın.
```

# ahenk-debug

Lider tarafında çevrimdışı görünen veya uzak komut kabul etmeyen Ahenk
istemcilerini yerinde teşhis eder, Pardus ETAP etkileşimli tahtasında Ahenk'in
kayıt ve bağlanma zincirini baştan sona inceler; arızayı kimlik/klon çakışması,
ağ/bağlantı ve yerel yazılım sınıflarına ayırır.

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
7. [Çıktıyı operatöre gönderme](#çıktıyı-operatöre-gönderme)
8. [Faz (Faz 1/2/3) tespiti](#faz-faz-123-tespiti)
9. [Arıza sınıflarını ayırt etme](#arıza-sınıflarını-ayırt-etme)
10. [Sık görülen senaryolar ve çözümleri](#sık-görülen-senaryolar-ve-çözümleri)
11. [JSON çıktısı](#json-çıktısı)
12. [Gereksinimler ve sınırlar](#gereksinimler-ve-sınırlar)
13. [Veri kaynakları](#veri-kaynakları)
14. [Gizlilik ve güvenlik](#gizlilik-ve-güvenlik)

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
| `--no-net` | Ağ testlerini (DNS/TCP/TLS) atlar; yalnız yerel kontroller. Canlı TCP soketi kontrolü yine de çalışır (yerel) |
| `--out DOSYA` | Raporu (renksiz) belirtilen dosyaya da yazar |
| `--db YOL` | `ahenk.db` yolunu elle verir (varsayılan: conf'taki `BASE/dbPath`) |
| `--mac MAC` | Kayıt durumu sorgusunu **bu makinenin değil**, verilen MAC için yapar (uzaktaki bir tahtayı kontrol için) |
| `--no-send` | Otomatik gönderimi kapatır (varsayılan: rapor paste.rs+ntfy.sh ile operatöre **otomatik** gönderilir; bkz. [Çıktıyı operatöre gönderme](#çıktıyı-operatöre-gönderme)) |
| `--renk=NE-ZAMAN` | Renkli çıktı: `otomatik` (varsayılan; yalnız terminalde), `her-zaman` ya da `hicbir-zaman`. `NO_COLOR` ortam değişkeni tanımlıysa renk kapanır |
| `-h`, `--help`, `--yardim` | Türkçe yardımı (seçenekler, örnekler, çıkış kodları) gösterir; root gerekmez |

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
| `64` | Hatalı kullanım (tanınmayan seçenek, eksik argüman) |
| `130` | Ctrl+C ile kesildi |

Bu sayede araç izleme/otomasyon içinde de kullanılabilir.

---

## Arka plan: Ahenk kayıt & bağlantı mimarisi

Aracın neye baktığını anlamak için Ahenk'in kayıt ve bağlanma akışını bilmek
yeterli. (Ahenk 2.0.10 için geçerlidir.)

### Kimlik

- Kayıt sırasında Ahenk, rastgele bir **`uuid4` JID** ve rastgele bir **`uuid4`
  parola** üretir; ikisini de hem `ahenk.db` (`registration` tablosu) hem
  `ahenk.conf` `[CONNECTION]` bölümünde saklar. Tekillik, Lider tarafında
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

---

## Rapor bölümleri

### 1) Genel Durum, Servis ve Yerel Sağlık
Ahenk paket sürümü, `ahenk.service` aktif/etkin durumu ve başlangıç zamanı.

Ahenk ETAP imajında (ISO) gelmez; tahtaya **yalnızca ETA Kayıt** kurar. ETA
Kayıt'ın arka plan denetimi yalnızca **etapadmin** oturum açılışında çalışır ve
tahta kayıtlı + Ahenk kurulu ise başka işlem yapmadan çıkar (durmuş servisi
başlatmaz). Bu yüzden:
- **Ahenk kurulu değil** → **FAIL**; ETA Kayıt günlüğünden nedeni (sanal makine,
  etapadmin dışı oturum, dokunmatik üretici reddi, 10 dk internet yok, MAC yok…)
  ve çözüm gösterilir.
- **Ahenk kendini durdurmuş** — Pulsar'a bağlanamayınca ya da kayıt denemeleri
  tükenince Ahenk `systemctl stop ahenk.service` ile kendini durdurur; systemd
  bunu yeniden başlatmaz → **FAIL**.
- Her iki durumda önerilen çözüm: etapadmin ile **ETA Kayıt → Düzenle → (sınıf
  adını değiştir) → Kaydet**. Kayıt güncellenince ETA Kayıt `apt install ahenk` ve
  `systemctl enable --now ahenk.service` çalıştırır. Kayıtlı tahtada ETA Kayıt
  yalnızca bilgi sayfasını gösterir; kurulumu yalnızca **Kaydet** tetikler.
- **ETA Kayıt günlüğü** — `/home/*/.cache/eta-register/eta-register.log` (arka plan
  denetimi) ve `/root/.cache/eta-register/eta-register.log` (pkexec ile çalışan
  kurulum adımı): son denetimin zamanı ve sonucu, denetim sayısı, son kayıt/
  güncelleme, Ahenk kurulum ve servis başlatma sonucu.
- **Ahenk en son sürüm mü?** — kurulu sürüm, ETAP deposunda **yayındaki en son
  sürümle** karşılaştırılır (`depo.etap.org.tr/etap/dists/<sürüm>/contrib/…/Packages.gz`
  doğrudan okunur); eskiyse **WARN**. Yerel apt adayı yayındakinden eskiyse
  "yerel paket listesi eski (`apt update` yapılmamış)" diye ayrıca belirtilir.
  Depoya ulaşılamazsa ya da `--no-net` verilmişse yerel apt listesine
  (`apt-cache policy ahenk`) göre karşılaştırılır. `eta-unattended-upgrade` kurulu
  değilse otomatik güncelleme ETAP paketlerini yükseltmez ve yeni Ahenk bu
  bağımlılığı istediğinden `apt-get upgrade` onu geri tutar; çözüm
  `sudo apt install ahenk` ya da `apt full-upgrade`. ETAP deposu tanımlı değilse
  ayrıca uyarılır.
- **ETA Kayıt en son sürüm mü?** — `eta-register` de aynı şekilde yayındaki en son
  sürümle karşılaştırılır; eskiyse **WARN** (çözüm `sudo apt install eta-register`),
  kurulu değilse **WARN** (Ahenk'i kuran ve başlatan uygulama odur).

Ayrıca Ahenk'i **yerelde sekteye uğratan** sorunlar:
- **Servis crash-loop** — `NRestarts` sayısı. Sürekli yeniden başlıyorsa
  (≥3) başlangıçta ölüyor demektir (FAIL).
- **Disk doluluğu** (`/` ve `/var`) — dolu disk SQLite DB ve log yazımını
  engeller, kaydı bozar (≥%90 WARN, ≥%95 FAIL).
- **Mevcut oturum hataları** — servisin son başlangıcından (ActiveEnter) bu yana
  loglardaki ERROR/Traceback/ImportError satırları (ConsumerBusy hariç). Başlangıç/
  bağımlılık çökmesi varsa FAIL.
- **Lider kayıt isteği hataları** — Ahenk'in `/register` isteği HTTP hata kodu, zaman
  aşımı ya da bağlantı hatası almışsa ayrı bir **FAIL** bulgusu verilir. HTTP 5xx
  **sunucu tarafı** hatasıdır: ETA Kayıt → Düzenle → Kaydet yalnızca yeni UUID ile
  yeni bir deneme başlatır, sonucu değiştirmez. Bulgu operatöre iletilecek bilgileri
  (MAC, board_id, okul kodu, hostname, son deneme saati) içerir. Ahenk bu yüzden
  kendini durdurmuşsa "kendini durdurmuş" bulgusu da nedeni bu olarak gösterir.
- **Kayıt öncesi beklenen sertifika hatası** — `messaging.conf`'taki sertifika yolu
  (`tls_trust_certs_file_path`) kayıt başarılı olunca Lider'in yanıtıyla yazılır.
  Yol boşken Ahenk açılışta indirdiği sertifikayı `open('')` ile yazmaya çalışır ve
  `FileNotFoundError` alır. Bu satır hata sayısına katılmaz, "kayıt öncesi BEKLENEN
  hata" diye ayrıca gösterilir.

### 2) Ahenk Kimliği ve Lider Kaydı (UUID / Parola)
DB'deki UUID (jid), parola (maskeli), `registered` bayrağı, kayıt zaman damgası,
**DB bütünlük kontrolü** (`PRAGMA integrity_check` — bozuk/kilitli DB Ahenk'i
durdurur). Root ile: **`ahenk.conf` UID/parola ile DB'nin tutarlılığı** ve Pulsar
topic/abonelik adları.

Kayıt tamamlanmamışken `ahenk.conf`'taki UID ve `messaging.conf`'taki Pulsar
adresi boştur (ikisi de kayıt yanıtıyla yazılır); bu durumda UID/DB karşılaştırması
ve Pulsar adres kontrolleri hata vermez, "kayıt tamamlanınca yazılır" diye bilgi verir.

**Lider kaydı** (`registered` bayrağına göre):
- `registered=1` → tamamlanmış → **OK**.
- `registered=0` → tamamlanmamış → **FAIL**; nedeni (ör. Lider kayıt isteği HTTP 500)
  bulguda gösterilir.

İki kayıt birbirinden ayrıdır ve rapor bunu açıkça belirtir:

| | ETA kaydı (5. bölüm) | Lider kaydı (2. bölüm) |
|---|---|---|
| Ne | tahtanın envanteri: okul, derslik adı, board_id | Ahenk'in mesajlaşma kimliği: UUID/parola |
| Kim yapar | ETA Kayıt programı | Ahenk servisi |
| Sunucu | `api-etap.eba.gov.tr` | `register-liderahenk.eba.gov.tr` |

`registered` **yerel** bir bayraktır (`/etc/ahenk/ahenk.db`): Lider'in kaydı geçmişte
kabul ettiğini gösterir, bugün hâlâ geçerli olduğunu doğrulamaz (Lider'de ajan
silinse bile 1 kalır ve Ahenk yeniden kayıt olmaz). Bu yüzden rapor, 3. bölümde
**"Lider kaydı (sunucu tarafı)"** satırını ayrıca verir:
- **sunucu tanıyor** — Ahenk bu kimlikle Pulsar'a bağlı ya da aktif test kimlik
  doğrulamasını geçti (OK);
- **sunucu TANIMIYOR** — aktif test oturumu reddetti; kayıt Lider'de silinmiş/
  değişmiş olabilir (FAIL);
- **sunucu tarafı doğrulanamadı** — canlı bağlantı yok ve test kimliği doğrulayamadı
  (INFO).

Zaman çizelgesinin son satırı ve operatöre giden özet bunu birlikte yazar, ör.
`Lider kaydı: var (yerel DB) — sunucu tanıyor`.

ETA kaydının başarılı olması (ör. derslik adının değişmesi) Lider kaydını garanti
etmez: ETA Kayıt kaydettikten sonra Ahenk'i başlatır, Ahenk de Lider'e ayrıca kayıt
olur. Lider kaydı başarısız olursa Ahenk kendini durdurur.

Mevcut mimari LDAP kullanmadığından LDAP'a dayalı kontroller yapılmaz: `dn`
(LDAP dizin nesnesi) yalnızca bilgi olarak gösterilir, boş olması bulgu üretmez.

### 3) Kimlik MAC'i, Klon/Çakışma ve Canlı Bağlantı
- Tüm ağ arayüzleri (MAC, sürücü, veri yolu, kablosuz/kablolu, durum).
- Seçilen **kimlik MAC'i** ve `etainfo.network`'ün gerçek sonucu (çökerse yakalar
  — bu başlı başına bir teşhistir).
- **Kayıt MAC'i karşılaştırması:** Ahenk'in kayıt anındaki MAC'i (DB `params`) ile
  bugünkü **kimlik MAC'i** karşılaştırılır. Farklıysa iki durum ayrılır:
  - Kayıt MAC'i **bu tahtadaki başka bir kartın** MAC'iyse → **WARN** "kimlik ağ
    kartı değişmiş (aynı tahta, klon değil)": iki PCI kablolu kart arasında seçim
    değişmiş (sürücü yüklenmemesi, arayüz adı/sıra değişimi, yeni imaj). UUID MAC'e
    bağlı olmadığından yeniden kayıt gerekmez.
  - Kayıt MAC'i **bu tahtada yoksa** → **FAIL** "klon imaj ya da ağ kartı/anakart
    değişimi"; iki olasılık ve çözümleri ayrı ayrı açıklanır.
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

### 5) ETA Kaydı (tahta envanteri)
Tahtanın **kayıtlı olduğu okul / şehir / ilçe / birim** bilgisini ve kayıt
durumunu gösterir. Bilgiler her çalıştırmada, tahtanın kimlik MAC'iyle ETA
sunucusuna yapılan `GET /api/board/check?mac=…` sorgusundan (ETA Kayıt'ın kendi
sorgusu) canlı alınır.

**Farklı MAC ile kayıt:** Kimlik MAC'iyle kayıt yoksa, Ahenk'in kayıt anındaki
MAC'i ve tahtadaki diğer kablolu kartların MAC'leri de sorgulanır. Biriyle kayıt
bulunursa okul/birim bilgileri o kayıttan gösterilir ve **FAIL** "Tahta ETA'da
FARKLI BİR MAC ile kayıtlı" verilir: **yeniden kaydetmeyin** — ETA Kayıt'ın kayıt
ekranı yeni bir kayıt (yeni board_id) oluşturur, eski kayıt sahipsiz kalır; ETA
Kayıt'ın Düzenle'si de yalnızca mevcut MAC kayıtlıyken görünür. Çözüm: eski MAC'li
kartın yeniden kimlik kartı seçilmesini sağlamak ya da operatörden ilgili
board_id kaydının MAC'ini güncellemesini istemek. Hiçbirinde kayıt yoksa bulgu
bunu da belirtir. (`--mac` verilmişse ek sorgu yapılmaz.)

### 6) Sistem / Dağıtım / Çekirdek
Dağıtım ve alt sürüm (ör. *Pardus ETAP GNU/Linux 23*), `lsb_release`, çekirdek
sürümü, mimari, hostname, **çalışma süresi** (son açılıştan bu yana; açılış zamanıyla), **NTP saat senkronu** (saat kayması TLS doğrulamasını
ve zaman damgalarını bozar).

### 7) Donanım, Faz ve Dokunmatik
İşlemci, anakart (üretici/model), BIOS, RAM, GPU(lar) + sürücü; **Faz 1/2/3
tahmini**; **dokunmatik donanımı + sürücüsü**
(`/proc/bus/input/devices` + USB kimliği + bağlı çekirdek sürücüsü).

### Özet / Teşhis
Tüm bulguları **önem sırasına** göre (FAIL → WARN → OK → INFO), nedeniyle ve
önerilen çözümüyle listeler. Ahenk kendini durdurmuşsa bulgu nedenine göre kısa ve
olay sırasıyla yazılır (ör. "Ahenk başladı, Lider kaydı başarısız olunca kendini
durdurdu (HTTP 500)" ya da "Ahenk Pulsar'a bağlanamayınca kendini durdurdu").

### Zaman çizelgesi
Raporun en sonunda, **son açılıştan bu yana** olaylar zaman sırasıyla listelenir
(her olay türünün son kaydı): tahtanın açılışı, ETA Kayıt denetimi ve kaydı/
güncellemesi, ETA Kayıt'ın Ahenk kurulumu ve servis başlatması, Ahenk servisinin
başlaması, Lider kaydı (başarılı/başarısız), Pulsar bağlantısı ve hataları,
ConsumerBusy, son başarılı yayın, Ahenk'in kendini durdurması ve raporun alındığı
an. Örnek:

```
2026-10-21 11:42:05  ETA Kayıt: ETA kaydı kaydedildi/güncellendi (derslik: Derslik002)
2026-10-21 11:42:19  ETA Kayıt: Ahenk servisini etkinleştirip başlattı
2026-10-21 11:42:20  Ahenk servisi başladı
2026-10-21 11:42:23  Ahenk: Lider kayıt isteği başarısız — HTTP 500 ×3 (son: 11:42:27)
2026-10-21 11:42:29  Ahenk kendini durdurdu (Lider kaydı başarısız)
```
JSON çıktısında `timeline` alanındadır.

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
4. **Aktif Pulsar testi** — Ahenk'in açılışta yaptığı bağlantı sınamasını
   **gerçek uid/parola** ile yapar ve **her adımın süresini ayrı ölçer**:

   | Adım | Katman | Ne sınanır |
   |---|---|---|
   | TCP bağlantısı | ağ | broker portuna erişim (güvenlik duvarı, yönlendirme) |
   | TLS el sıkışma | ağ | şifreli bağlantı, sertifika |
   | Kimlik doğrulama | uygulama | Pulsar sunucusunun tahtanın uid/parolasını kabul etmesi |
   | Topic arama | uygulama | proxy'nin `test-topic-lider`'i iç broker'a yönlendirmesi |
   | Producer açma | uygulama | sunucunun topic'i yükleyip yazıcı açması |
   | Mesaj gönderme + onay | uygulama | mesajın kalıcı depoya yazılıp onaylanması |

   Rapordaki **Katman** satırı sonucu açıkça yazar: TCP/TLS başarısızsa sorun **ağ
   katmanındadır**; TCP ve TLS başarılı olup sonraki bir adım hata/zaman aşımı
   veriyorsa ağ sağlamdır ve sorun **uygulama katmanında (Pulsar sunucusu
   tarafında)**, adı verilen adımdadır. Başarılı ama 5 sn'yi aşan adımlar **YAVAŞ**
   (WARN) işaretlenir. Kimlik doğrulama ile topic arama, oturum iki kez açılarak
   ayrılır; süre istemci günlüğündeki "Connected to broker" anından ölçülür.
   Her adım en çok **60 sn** bekler (Ahenk 120 sn bağlanma / 300 sn işlem bekler).
   Sunucu oturum isteğini protokol düzeyinde `ERROR` ile reddederse (kütüphane
   bunu yalnızca `ConnectError` diye bildirir) **kimlik reddi** olarak raporlanır.
   Test yalnızca **Ahenk'in Pulsar bağlantısı canlı değilken** yapılır; bağlantı
   canlıysa (aktif TCP ya da yeni başarılı olay) "yapılmadı" diye belirtilir, çünkü
   canlı bağlantı aynı şeyi zaten kanıtlar. Kayıt tamamlanmamışsa tahtanın Pulsar kimliği
   olmadığından test atlanır. Komut aboneliğine (`ahenk-<uid>`) **dokunmaz**,
   çalışan Ahenk'i etkilemez. (`--no-net` ile atlanır.)

**Karar tablosu:**

| Durum | Bulgu |
|---|---|
| ConsumerBusy **bayat** (servis yeniden başlamadan önce ya da sonrasında başarı var) | **Gösterilmez** — geçmiş oturuma ait |
| ConsumerBusy **güncel** + sonrasında başarı yok | **FAIL** — "Aktif klon/çakışma sürüyor" |

Aynı kural Pulsar bağlantı hataları ve "Ahenk günlüğündeki son hata" satırı için de
geçerlidir: geçmiş hatalar (**son açılıştan önceki**, servis yeniden başlamadan önceki,
sonrasında başarılı olay olan ya da şu an canlı broker bağlantısı varken kalanlar)
raporda gösterilmez. Son açılış ölçütü, servis çalışmıyorken de (başlangıç zamanı
yokken) eski hataların ayıklanmasını sağlar; ConsumerBusy ve not_authorized için de
uygulanır.

**Bilinen zararsız Ahenk hataları** "son hata" seçilirken atlanır ve ayrıca bilgi
(INFO) olarak açıklamasıyla gösterilir. Şu an tanınan: tahta kapanırken
`resource-usage` eklentisinde `module 'shutdown' has no attribute 'handle_mode'`
(Ahenk'te isim çakışması; eklentinin `shutdown.py`'si "bilgisayarı kapat" görevinin
modülüdür, kapanış bildirimi işleyicisi değildir — bir şey bozulmaz).

---

## Çıktıyı operatöre gönderme

Araç, raporu **her çalışmada otomatik** olarak operatöre ulaştırır — kullanıcının
ekran çıktısını seçip kopyalayıp dosyaya kaydedip mail/WhatsApp ile göndermesine
gerek kalmaz. Hiçbir sunucu kurulmaz; iki ücretsiz servis kullanılır:

- **[paste.rs](https://paste.rs)** — tam rapor metni yüklenir, kısa bir URL üretilir.
- **[ntfy.sh](https://ntfy.sh)** — özet + rapor linki, operatörün bir **konusuna**
  (topic) anlık bildirim olarak itilir.

Gönderim başarılıysa ekrana ayrıca bir ileti yazılmaz; gönderilemezse nedeni
("Rapor operatöre gönderilemedi: …") yazılır — o durumda rapor elle iletilmelidir.

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

Konuyu değiştirmek için: çalıştırmadan önce `AHENK_DEBUG_NTFY=<konu>` ortam
değişkenini ayarlayın (ya da koddaki gömülü konuyu değiştirin).

> **Gizlilik.** Rapor; okul adı, MAC, IP gibi tanımlayıcı bilgi içerir. `ahenk-debug`
> herkese açık ve kolay tahmin edilen bir konudur; bilen herkes abone olup gelen
> raporları görebilir ya da konuya çöp mesaj gönderebilir. Hassas kullanımda
> `AHENK_DEBUG_NTFY` ile kendi rastgele konunuzu belirleyin. `paste.rs` linkleri
> tahmin edilemez ama içerik üçüncü tarafta barınır.

---

## Faz (Faz 1/2/3) tespiti

ETAP tahtaları donanım kuşağına göre fazlara ayrılır. Araç, işlemci markası ve
anakart üreticisinden bir tahmin üretir:

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
| Kayıt MAC'i bu tahtadaki başka bir kartın MAC'i | 3 | **Kimlik kartı seçimi değişmiş** — aynı tahta, klon değil |
| Kayıt MAC'i bu tahtada yok | 3 | **Klon imaj** ya da **ağ kartı/anakart değişimi** |
| ETA'da kimlik MAC'iyle yok, başka MAC ile kayıtlı | 5 | **Farklı MAC ile kayıt** — yeniden kaydetmeyin, MAC'i güncelletin |
| Kimlik MAC'i belirlenemiyor / `etainfo` hatası | 3 | Kablolu ethernet/sürücü yok → kayıt çöker |
| Kablolu arayüzde carrier/IP yok | 4 | **Fiziksel/DHCP** sorunu — hiçbir sunucuya ulaşılamaz |
| Broker portuna TCP açılamıyor | 4 | **Güvenlik duvarı / yanlış adres / sunucu kapalı** → subscribe olamaz |
| DNS sunucusu yok / ad çözülemiyor | 4 | **DNS** sorunu — broker adı IP'ye çevrilemez |
| DNS/TCP/TLS başarısız, ağ geçidi yok | 4 | **Ağ/bağlantı** sorunu |
| `registered=0` | 2 | **Lider kaydı** tamamlanmamış (ETA kaydından ayrı) |
| Ahenk kurulu değil | 1 | **ETA Kayıt kurulumu yapmamış** — Düzenle → Kaydet |
| Ahenk kayıt isteği HTTP 5xx alıyor | 2 | **Lider kayıt sunucusu hatası** — sunucu tarafı; Düzenle → Kaydet çözmez, operatöre bildirin (rapor MAC/board_id/saat verir) |
| Aktif test: TCP/TLS başarılı, sonraki adım hata/zaman aşımı | 3 | **Uygulama katmanı** (Pulsar sunucusu) — ağ sağlam |
| Aktif test: sunucu oturumu reddediyor | 3 | **Kimlik reddi** — uid/parola Pulsar'da geçersiz (silinmiş kayıt/klon) |
| Servis pasif ve logda kendini durdurma | 1 | **Ahenk kendini durdurmuş** — ağ/Pulsar sorunu sonrası |
| Kurulu Ahenk < yayındaki en son sürüm | 1 | **Eski Ahenk** — `apt install ahenk` |
| Kurulu eta-register < yayındaki en son sürüm | 1 | **Eski ETA Kayıt** — `apt install eta-register` |
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

### B) Ağ/bağlantı sorunu
Bölüm 4'teki DNS/TCP/TLS satırlarına bakın. Güvenlik duvarı, yanlış broker
adresi, süresi dolmuş/eksik TLS sertifikası veya kopuk ağ geçidi olabilir.
Aktif Pulsar testi (otomatik) kimlik doğrulamanın da çalışıp çalışmadığını gösterir.

### C) Kimlik MAC'i çözülemiyor
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
| `school` | tahtanın kayıtlı olduğu okul/il/ilçe |
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

---

## Gizlilik ve güvenlik

- Parolalar raporda **maskelenir** (`e2a2…8df5`); tam parola hiçbir yerde açıkça
  gösterilmez.
- Araç, Ahenk yapılandırmasını ve servisini **değiştirmez** (salt-okur).
- Aktif Pulsar testi, broker'a yalnızca **tek bir test mesajı** yollar (Ahenk'in
  açılışta zaten yaptığı işlem) ve komut aboneliğine dokunmaz.
- Otomatik gönderim açıkken rapor; `paste.rs` ve `ntfy.sh` üçüncü taraf
  servislerine gider (bkz. [Çıktıyı operatöre gönderme](#çıktıyı-operatöre-gönderme)).
  İstemiyorsanız `--no-send` ile kapatın.
```

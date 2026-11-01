/*
  Ultimate CH552 4-Password Macro Pad (80-Character Safe Delay Edition)

  REVISI - penulisan DataFlash yang aman:
  - Data dari serial ditampung dulu di RAM, baru ditulis setelah lengkap & valid
  - Baca serial pakai timeout (tidak lagi menunggu selamanya)
  - Interrupt dimatikan selama tiap penulisan byte, lalu hasilnya diverifikasi
  - Byte yang isinya sudah sama tidak ditulis ulang (hemat siklus tulis)
  - Penanda commit di alamat 8: data setengah jadi tidak akan pernah diketik
  - Saat boot, data TIDAK ditimpa default kecuali chip benar-benar kosong
  - Hanya karakter ASCII yang bisa diketik (0x20-0x7E) yang diterima/diketik
  - P1.5 dijadikan "ground" untuk tombol (output LOW setelah firmware jalan)

  Layout DataFlash (alamat 0-7) dan protokol serial ('#' dan '?') TETAP SAMA,
  jadi aplikasi konfigurator di PC tidak perlu diubah.
*/

#ifndef USER_USB_RAM
#error "PENTING: Pastikan menu Tools > USB Settings sudah diubah ke opsi USER CODE!"
#endif

#include "src/CdcHidCombo/USBCDC.h"
#include "src/CdcHidCombo/USBHIDKeyboard.h"

#define BUTTON_PIN  32   // Pin P3.2 (Main Macro Button, INPUT_PULLUP)
#define LED_BUILTIN 30   // Pin P3.0 for the onboard status LED

// P1.5 dipakai sebagai "ground" tombol: switch dipasang antara P3.2 dan P1.5.
// Saat USB dicolok, P1.5 masih input ber-pull-up (HIGH), jadi tidak memicu
// bootloader. Baru setelah firmware jalan, P1.5 dijadikan output LOW.
// JANGAN sambungkan P1.5 ke P3.0: P3.0 adalah output LED, bisa korsleting.
#define GROUND_PIN  15   // Pin P1.5

// =========================================================================
// LAYOUT DATAFLASH (128 byte, alamat 0..127)
//   0..7  : metadata [len1, ptr1, len2, ptr2, len3, ptr3, len4, ptr4]
//   8     : penanda commit (0xA5 = penulisan terakhir selesai & valid)
//   10..89: data password (maks 80 byte total)
// =========================================================================
#define ADDR_META_START      0
#define ADDR_COMMIT          8
#define COMMIT_VALID         0xA5
#define COMMIT_SEDANG_TULIS  0x00
#define DYNAMIC_DATA_POOL    10
#define MAX_TOTAL            80
#define DATAFLASH_SIZE       128
#define SERIAL_TIMEOUT_MS    2000

uint8_t lenPass[4];
uint8_t ptrPass[4];
bool dataSiap = false;   // false = data tidak valid, perangkat tidak akan mengetik

__xdata uint8_t bufBaru[MAX_TOTAL];   // buffer RAM untuk data dari serial

// Multi-Click & Long-Press Intercept Engine Variables
unsigned long waktuTombolDitekan = 0;
unsigned long waktuTerakhirKlik = 0;
bool statusTombolSebelumnya = HIGH;
bool sedangDitekanTahan = false;
uint8_t jumlahKlik = 0;

const unsigned long jedaDouble = 350;     // Jeda maksimal antar klik (ms)
const unsigned long jedaLongPress = 750;  // Durasi klik-tahan (ms)

unsigned long waktuTerakhirBlink = 0;
bool statusLEDBlink = LOW;

// =========================================================================
// UTILITAS
// =========================================================================
bool karakterBisaDiketik(uint8_t c) {
  return (c >= 0x20 && c <= 0x7E);
}

// Mengembalikan byte (0..255), atau -1 kalau timeout.
int16_t readSerialTimeout(void) {
  unsigned long t0 = millis();
  while (USBSerial_available() <= 0) {
    if (millis() - t0 > SERIAL_TIMEOUT_MS) return -1;
  }
  return (uint8_t)USBSerial_read();
}

void kedipError(void) {
  for (uint8_t i = 0; i < 3; i++) {
    digitalWrite(LED_BUILTIN, HIGH); delay(80);
    digitalWrite(LED_BUILTIN, LOW);  delay(80);
  }
}

// Tulis 1 byte dengan aman.
// eeprom_write_byte() sudah menjalankan urutan SAFE_MOD sendiri; urutan itu
// tidak boleh disela interrupt (USB), jadi interrupt dimatikan sebentar.
// Hasil tulis diverifikasi dan dicoba ulang maksimal 3 kali.
bool tulisByteAman(uint8_t addr, uint8_t val) {
  if (addr >= DATAFLASH_SIZE) return false;
  if (eeprom_read_byte(addr) == val) return true;   // sudah sama, tidak perlu tulis
  for (uint8_t coba = 0; coba < 3; coba++) {
    uint8_t eaLama = EA;
    EA = 0;
    eeprom_write_byte(addr, val);
    EA = eaLama;
    delay(2);
    if (eeprom_read_byte(addr) == val) return true;
  }
  return false;
}

// Metadata valid kalau pointer tersusun berurutan mulai alamat 10
// dan total panjang tidak melebihi 80.
bool metadataValid(void) {
  uint16_t total = 0;
  uint8_t harapPtr = DYNAMIC_DATA_POOL;
  for (uint8_t i = 0; i < 4; i++) {
    if (lenPass[i] > MAX_TOTAL) return false;
    if (ptrPass[i] != harapPtr) return false;
    total += lenPass[i];
    harapPtr += lenPass[i];
  }
  return (total <= MAX_TOTAL);
}

// Simpan 4 password dari bufBaru.
// Urutan: tandai "sedang menulis" -> data -> metadata -> tandai valid.
// Kalau daya putus di tengah jalan, penanda tetap 0x00, sehingga saat boot
// perangkat menolak mengetik (bukan mengetik password setengah jadi).
bool simpanSemua(uint8_t *lenBaru) {
  uint8_t ptrBaru[4];
  uint8_t p = DYNAMIC_DATA_POOL;
  uint8_t total = 0;
  for (uint8_t i = 0; i < 4; i++) {
    ptrBaru[i] = p;
    p += lenBaru[i];
    total += lenBaru[i];
  }

  if (!tulisByteAman(ADDR_COMMIT, COMMIT_SEDANG_TULIS)) return false;

  for (uint8_t i = 0; i < total; i++) {
    if (!tulisByteAman(DYNAMIC_DATA_POOL + i, bufBaru[i])) return false;
  }
  for (uint8_t i = 0; i < 4; i++) {
    if (!tulisByteAman(ADDR_META_START + i * 2,     lenBaru[i])) return false;
    if (!tulisByteAman(ADDR_META_START + i * 2 + 1, ptrBaru[i])) return false;
  }

  if (!tulisByteAman(ADDR_COMMIT, COMMIT_VALID)) return false;

  for (uint8_t i = 0; i < 4; i++) {
    lenPass[i] = lenBaru[i];
    ptrPass[i] = ptrBaru[i];
  }
  return true;
}

void muatDariFlash(void) {
  bool semuaKosong = true;
  for (uint8_t i = 0; i < 4; i++) {
    lenPass[i] = eeprom_read_byte(ADDR_META_START + i * 2);
    ptrPass[i] = eeprom_read_byte(ADDR_META_START + i * 2 + 1);
    if (lenPass[i] != 0xFF || ptrPass[i] != 0xFF) semuaKosong = false;
  }
  uint8_t commit = eeprom_read_byte(ADDR_COMMIT);

  // Chip benar-benar baru (semua metadata & penanda 0xFF): isi default.
  if (semuaKosong && commit == 0xFF) {
    const char *def = "Password1Password2Password3Password4";
    for (uint8_t i = 0; i < 36; i++) bufBaru[i] = def[i];
    uint8_t lenDef[4] = {9, 9, 9, 9};
    dataSiap = simpanSemua(lenDef);
    return;
  }

  // Selain chip kosong, JANGAN pernah menimpa apa pun secara otomatis.
  if (!metadataValid()) { dataSiap = false; return; }

  if (commit == COMMIT_VALID) { dataSiap = true; return; }

  // Migrasi dari firmware lama yang belum punya penanda commit.
  if (commit == 0xFF) {
    dataSiap = tulisByteAman(ADDR_COMMIT, COMMIT_VALID);
    return;
  }

  // Penanda 0x00 / nilai lain: penulisan terakhir tidak selesai.
  dataSiap = false;
}

void ketikPassword(uint8_t idx) {
  if (!dataSiap) { kedipError(); return; }

  uint8_t len = lenPass[idx];
  uint8_t ptr = ptrPass[idx];
  if (len == 0) return;
  if ((uint16_t)ptr + len > DYNAMIC_DATA_POOL + MAX_TOTAL) { kedipError(); return; }

  // Cek dulu semua karakter. Kalau ada yang rusak, jangan ketik sama sekali
  // (mengetik password setengah benar hanya memicu login gagal / akun terkunci).
  for (uint8_t i = 0; i < len; i++) {
    if (!karakterBisaDiketik(eeprom_read_byte(ptr + i))) { kedipError(); return; }
  }

  for (uint8_t i = 0; i < len; i++) {
    char c = eeprom_read_byte(ptr + i);
    Keyboard_press(c);
    delay(8);
    Keyboard_release(c);
    delay(8);
  }
  delay(100);
  Keyboard_press('\n');
  delay(8);
  Keyboard_release('\n');
}

// =========================================================================
// SETUP
// =========================================================================
void setup() {
  USBInit();
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_BUILTIN, OUTPUT);

  // P1.5 -> LOW: sekarang switch P3.2--P1.5 berfungsi seperti tombol ke GND
  digitalWrite(GROUND_PIN, LOW);
  pinMode(GROUND_PIN, OUTPUT);
  digitalWrite(GROUND_PIN, LOW);
  delay(5);                       // biarkan pin settle sebelum tombol dibaca
  // Kalau switch sudah tertutup saat start, abaikan sampai dilepas dulu
  // (supaya tidak langsung mengetik password 4 / password 1 tanpa sengaja).
  if (digitalRead(BUTTON_PIN) == LOW) {
    statusTombolSebelumnya = LOW;
    sedangDitekanTahan = true;
  }

  delay(200);   // beri waktu tegangan USB stabil sebelum mengakses DataFlash
  muatDariFlash();
}

// =========================================================================
// LOOP
// =========================================================================
void loop() {
  unsigned long waktuSekarang = millis();
  bool statusTombolSekarang = digitalRead(BUTTON_PIN);

  // =========================================================================
  // PIPELINE A: SERIAL (protokol sama dengan versi lama)
  //   '#' + len1 len2 len3 len4 + data  -> simpan
  //   '?'                               -> kirim isi
  // =========================================================================
  if (USBSerial_available() > 0) {
    char serialChar = USBSerial_read();

    if (serialChar == '#') {
      uint8_t lenBaru[4];
      uint16_t total = 0;
      bool ok = true;

      for (uint8_t i = 0; i < 4; i++) {
        int16_t b = readSerialTimeout();
        if (b < 0) { ok = false; break; }
        lenBaru[i] = (uint8_t)b;
        total += lenBaru[i];
      }

      if (!ok) {
        USBSerial_println("ERROR_TIMEOUT"); USBSerial_flush();
      } else if (total > MAX_TOTAL) {
        USBSerial_println("ERROR_LIMIT"); USBSerial_flush();
      } else {
        // Tampung SEMUA data di RAM dulu; flash belum disentuh sama sekali.
        bool karakterOk = true;
        for (uint8_t i = 0; i < total; i++) {
          int16_t b = readSerialTimeout();
          if (b < 0) { ok = false; break; }
          bufBaru[i] = (uint8_t)b;
          if (!karakterBisaDiketik(bufBaru[i])) karakterOk = false;
        }

        if (!ok) {
          USBSerial_println("ERROR_TIMEOUT");
        } else if (!karakterOk) {
          USBSerial_println("ERROR_CHAR");
        } else if (simpanSemua(lenBaru)) {
          dataSiap = true;
          USBSerial_println("OK");
        } else {
          dataSiap = false;
          USBSerial_println("ERROR_WRITE");
        }
        USBSerial_flush();
      }
    }

    if (serialChar == '?') {
      if (dataSiap) {
        for (uint8_t i = 0; i < 4; i++) USBSerial_write(lenPass[i]);
        for (uint8_t i = 0; i < 4; i++) {
          for (uint8_t j = 0; j < lenPass[i]; j++) {
            USBSerial_write(eeprom_read_byte(ptrPass[i] + j));
          }
        }
      } else {
        for (uint8_t i = 0; i < 4; i++) USBSerial_write(0);
      }
      USBSerial_flush();
    }
  }

  // =========================================================================
  // PIPELINE B: TIMING INTERCEPT (SINGLE, DOUBLE, TRIPLE, AND HOLD)
  // =========================================================================
  if (statusTombolSekarang == LOW && statusTombolSebelumnya == HIGH) {
    delay(25); // Debounce
    if (digitalRead(BUTTON_PIN) == LOW) {
      waktuTombolDitekan = waktuSekarang;
      sedangDitekanTahan = false;
      if (jumlahKlik == 0) waktuTerakhirKlik = waktuSekarang;
    }
  }

  // Klik-tahan (Hold) -> Password 4
  if (statusTombolSekarang == LOW && !sedangDitekanTahan) {
    if (waktuSekarang - waktuTombolDitekan >= jedaLongPress) {
      sedangDitekanTahan = true;
      jumlahKlik = 0;
      digitalWrite(LED_BUILTIN, HIGH);
      ketikPassword(3);
    }
  }

  if (statusTombolSekarang == HIGH && statusTombolSebelumnya == LOW) {
    if (!sedangDitekanTahan) {
      jumlahKlik++;
      waktuTerakhirKlik = waktuSekarang;
    }
  }
  statusTombolSebelumnya = statusTombolSekarang;

  if (jumlahKlik > 0 && (waktuSekarang - waktuTerakhirKlik > jedaDouble)) {
    if (digitalRead(BUTTON_PIN) == HIGH) {
      if (jumlahKlik == 1)      ketikPassword(0);
      else if (jumlahKlik == 2) ketikPassword(1);
      else                      ketikPassword(2);
      jumlahKlik = 0;
    }
  }

  // Heartbeat LED: lambat (1 dtk) = normal, cepat = data tidak valid
  unsigned long intervalBlink = dataSiap ? 1000 : 150;
  if (waktuSekarang - waktuTerakhirBlink >= intervalBlink) {
    waktuTerakhirBlink = waktuSekarang;
    statusLEDBlink = !statusLEDBlink;
    digitalWrite(LED_BUILTIN, statusLEDBlink);
  }
}

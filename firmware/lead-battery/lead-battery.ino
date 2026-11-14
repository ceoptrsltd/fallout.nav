// fallout.nav — третья «станция»: свинцовый аккумулятор 12 V (docs/contracts/power.md «Третья станция»).
// Seeed XIAO ESP32-C6. Меряет напряжение аккумулятора (A0) и входа 12 V (A1) через делители 100k/20k; есть 12 V на
// входе — замыкает RC-реле (D3), и зарядное устройство заряжает аккумулятор. Отдаёт показания по Bluetooth LE
// (только чтение): fnav-core (fnav/core/leadbatt.py) подключается и раз в 10 с пишет байт в CMD — модуль сразу
// присылает STATUS (и сам — раз в 2 с, пока подключены).
//
// STATUS, 14 байт, little-endian: 'F' 'L' ver(1) flags  vbat_mV(u16) vin_mV(u16) uptime_s(u32) crc16(u16)
//   flags: bit0 — на входе есть 12 V, bit1 — реле (зарядка) включено. CRC16/Modbus первых 12 байт.
//
// Сборка: arduino-cli compile --fqbn esp32:esp32:XIAO_ESP32C6 firmware/lead-battery

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLE2902.h>

// ── подключение ────────────────────────────────────────────────────────
static const int PIN_BAT = A0;    // делитель аккумулятора
static const int PIN_IN = A1;     // делитель входа 12 V
// RC-реле, как аварийка на Pi (docs/contracts/alarm.md «Аварийка»): сигнал сервопривода 50 Гц, импульс 2000 мкс —
// замкнуто, 1000 мкс — разомкнуто; импульсы «выкл» идут всё время. ШИМ аппаратный (LEDC) — ровный при любой загрузке
static const int PIN_RELAY = D3;  // сигнал (S) RC-реле; его «+» — 5 V от DC-DC, «−» — GND
static const uint32_t SERVO_HZ = 50;
static const uint8_t SERVO_BITS = 14;
static const uint32_t ON_US = 2000;
static const uint32_t OFF_US = 1000;

static const float DIVIDER = (100.0f + 20.0f) / 20.0f;  // 100k сверху, 20k снизу
static const float CAL_BAT = 1.000f;  // поправка по мультиметру: показанное → настоящее
static const float CAL_IN = 1.000f;

// ── вход 12 V: включить зарядку, выключить — с запасом и выдержкой (не дёргать реле) ──
static const float IN_ON_V = 11.5f;
static const float IN_OFF_V = 10.5f;
static const uint32_t IN_ON_MS = 3000;
static const uint32_t IN_OFF_MS = 2000;

static const uint32_t SAMPLE_MS = 100;
static const float EMA = 0.1f;  // сглаживание ~1 с
static const uint32_t NOTIFY_MS = 2000;
static const char *NAME = "FNAV-LEAD";

#define SERVICE_UUID "6e4c0001-f4a5-4c3a-9f39-1d2b5f0c7a01"
#define STATUS_UUID  "6e4c0002-f4a5-4c3a-9f39-1d2b5f0c7a01"
#define CMD_UUID     "6e4c0003-f4a5-4c3a-9f39-1d2b5f0c7a01"

static BLECharacteristic *statusChr = nullptr;
static volatile bool connected = false;
static volatile bool wantNotify = false;
static bool restartAdv = false;

static float vBat = 0, vIn = 0;
static bool inputOn = false;
static uint32_t inSince = 0;  // с какого момента вход «другой», чем inputOn

static uint16_t crc16(const uint8_t *p, size_t n) {
  uint16_t c = 0xFFFF;
  for (size_t i = 0; i < n; i++) {
    c ^= p[i];
    for (int b = 0; b < 8; b++) c = (c & 1) ? (c >> 1) ^ 0xA001 : c >> 1;
  }
  return c;
}

static float readVolts(int pin, float cal) {
  uint32_t mv = 0;
  for (int i = 0; i < 8; i++) mv += analogReadMilliVolts(pin);
  return mv / 8.0f / 1000.0f * DIVIDER * cal;
}

static void setRelay(bool on) {
  const uint32_t period = 1000000 / SERVO_HZ;
  ledcWrite(PIN_RELAY, (on ? ON_US : OFF_US) * ((1u << SERVO_BITS) - 1) / period);
}

static void fillStatus(uint8_t *b) {
  uint16_t vb = (uint16_t)constrain(lroundf(vBat * 1000), 0, 65535);
  uint16_t vi = (uint16_t)constrain(lroundf(vIn * 1000), 0, 65535);
  uint32_t up = millis() / 1000;
  b[0] = 'F';
  b[1] = 'L';
  b[2] = 1;
  b[3] = inputOn ? 0x03 : 0x00;  // реле всегда следует за входом: оба бита вместе
  b[4] = vb & 0xFF;
  b[5] = vb >> 8;
  b[6] = vi & 0xFF;
  b[7] = vi >> 8;
  for (int i = 0; i < 4; i++) b[8 + i] = (up >> (8 * i)) & 0xFF;
  uint16_t c = crc16(b, 12);
  b[12] = c & 0xFF;
  b[13] = c >> 8;
}

class ServerCb : public BLEServerCallbacks {
  void onConnect(BLEServer *) override { connected = true; }
  void onDisconnect(BLEServer *) override {
    connected = false;
    restartAdv = true;  // снова видны — fnav-core переподключится
  }
};

class CmdCb : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *) override { wantNotify = true; }  // любой байт — «пришли сейчас»
};

void setup() {
  Serial.begin(115200);
#ifdef WIFI_ENABLE
  pinMode(WIFI_ENABLE, OUTPUT);  // питание антенного переключателя XIAO ESP32-C6
  digitalWrite(WIFI_ENABLE, LOW);
#endif
#ifdef WIFI_ANT_CONFIG
  pinMode(WIFI_ANT_CONFIG, OUTPUT);  // встроенная керамическая антенна (HIGH — внешняя, UFL)
  digitalWrite(WIFI_ANT_CONFIG, LOW);
#endif
  ledcAttach(PIN_RELAY, SERVO_HZ, SERVO_BITS);
  setRelay(false);
  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);  // до ~3.1 V на входе АЦП
  vBat = readVolts(PIN_BAT, CAL_BAT);
  vIn = readVolts(PIN_IN, CAL_IN);

  BLEDevice::init(NAME);
  BLEServer *server = BLEDevice::createServer();
  server->setCallbacks(new ServerCb());
  BLEService *svc = server->createService(SERVICE_UUID);
  statusChr = svc->createCharacteristic(STATUS_UUID, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  statusChr->addDescriptor(new BLE2902());
  BLECharacteristic *cmd = svc->createCharacteristic(CMD_UUID, BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  cmd->setCallbacks(new CmdCb());
  svc->start();
  BLEAdvertising *adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(SERVICE_UUID);
  adv->setScanResponse(true);
  BLEDevice::startAdvertising();
  Serial.printf("FNAV-LEAD started, BLE %s\n", BLEDevice::getAddress().toString().c_str());
}

void loop() {
  static uint32_t lastSample = 0, lastNotify = 0, lastLog = 0;
  uint32_t now = millis();

  if (now - lastSample >= SAMPLE_MS) {
    lastSample = now;
    vBat += EMA * (readVolts(PIN_BAT, CAL_BAT) - vBat);
    vIn += EMA * (readVolts(PIN_IN, CAL_IN) - vIn);
    bool want = inputOn ? vIn > IN_OFF_V : vIn >= IN_ON_V;  // «как сейчас» с учётом гистерезиса
    if (want == inputOn) {
      inSince = now;
    } else if (now - inSince >= (inputOn ? IN_OFF_MS : IN_ON_MS)) {
      inputOn = want;
      inSince = now;
      setRelay(inputOn);
      wantNotify = true;
    }
  }

  uint8_t buf[14];
  if (connected && (wantNotify || now - lastNotify >= NOTIFY_MS)) {
    wantNotify = false;
    lastNotify = now;
    fillStatus(buf);
    statusChr->setValue(buf, sizeof buf);
    statusChr->notify();
  } else if (!connected && now - lastNotify >= NOTIFY_MS) {
    lastNotify = now;
    fillStatus(buf);
    statusChr->setValue(buf, sizeof buf);  // для чтения без подписки
  }
  if (restartAdv) {
    restartAdv = false;
    delay(200);
    BLEDevice::startAdvertising();
  }
  if (now - lastLog >= 2000) {
    lastLog = now;
    Serial.printf("bat %.2f V  in %.2f V  charger %s  ble %s\n", vBat, vIn, inputOn ? "ON" : "off",
                  connected ? "connected" : "advertising");
  }
  delay(5);
}

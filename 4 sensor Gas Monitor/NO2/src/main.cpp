// ================================================================
//  MICS-6814 Triple Gas Sensor – ESP32 Analog Driver
//  Gases  : CO (Carbon Monoxide), NO2 (Nitrogen Dioxide),
//           NH3 (Ammonia)
//  Output : Raw ADC | Voltage | RS | RS/R0 | ppm | % volume
// ================================================================
#include <Arduino.h>

// ── GPIO Pins ────────────────────────────────────────────────────
const int CO_PIN  = 33; 
const int NO2_PIN = 35;   // Input-only ADC1 pin
const int NH3_PIN = 32;

// ── ADC & Reference Voltage ──────────────────────────────────────
const float VREF = 3.3f;  // ESP32 supply / ADC reference (V)

// ── Load Resistors (RL) ──────────────────────────────────────────
// These are the fixed resistors on your MICS-6814 breakout board.
// Check the schematic of your specific board and adjust accordingly.
// Common values: CO = 10kΩ, NO2 = 22kΩ, NH3 = 10kΩ
const float RL_CO  = 10000.0f;   // 10 kΩ
const float RL_NO2 = 22000.0f;   // 22 kΩ
const float RL_NH3 = 10000.0f;   // 10 kΩ

// ── Baseline Resistance R0 (Ω) in Clean Air ─────────────────────
// HOW TO CALIBRATE:
//   1. Place the sensor outdoors or in clean, fresh air.
//   2. Power it on and let it warm up for at least 24 hours.
//   3. Enable CALIBRATION_MODE = true and observe the RS values.
//   4. Once RS values have stabilised, copy them here as R0_xx.
//   5. Set CALIBRATION_MODE = false for normal operation.
//
// NOTE: These defaults are placeholders – accuracy requires
//       running calibration with your specific hardware.
float R0_CO  = 10000.0f;   // <-- Replace after calibration
float R0_NO2 = 10000.0f;   // <-- Replace after calibration
float R0_NH3 = 10000.0f;   // <-- Replace after calibration

// Set true during first-time calibration in clean air
const bool CALIBRATION_MODE = false;

// ── Sensitivity Curves (MICS-6814 Datasheet – Power-Law Fit) ────
//
// The sensor resistance ratio (RS/R0) follows a power-law curve:
//   RS/R0 = a * ppm^b
//
// Rearranged to solve for ppm:
//   ppm = (RS/R0 / a)^(1/b)
//
// Behaviour:
//   Reducing gases (CO, NH3) : RS decreases as concentration rises → b < 0
//   Oxidising gas  (NO2)     : RS increases as concentration rises → b > 0
//
//  Gas   Valid Range    a        b       Source
//  CO    1 – 1000 ppm   4.40    -0.336  MICS-6814 datasheet
//  NO2   0.05 – 10 ppm  0.1516   0.979  MICS-6814 datasheet
//  NH3   1 – 500 ppm    1.50    -0.394  MICS-6814 datasheet
struct GasCurve { float a, b; };

const GasCurve CURVE_CO  = {  4.40f,  -0.336f };
const GasCurve CURVE_NO2 = {  0.1516f, 0.9790f };
const GasCurve CURVE_NH3 = {  1.50f,  -0.394f };

// ── Safety / Hazard Thresholds ───────────────────────────────────
// Based on OSHA 8-hour Time-Weighted Average (TWA) limits
const float LIMIT_CO  =  35.0f;   // ppm – OSHA TWA
const float LIMIT_NO2 =   3.0f;   // ppm – OSHA TWA
const float LIMIT_NH3 =  25.0f;   // ppm – OSHA TWA

// ================================================================
//  calcRS: Convert raw ADC reading to sensor resistance RS (Ω)
//
//  Circuit: VCC ──[ RS (sensor) ]── VOUT(pin) ──[ RL ]── GND
//           Vout = VREF * RL / (RS + RL)
//     =>    RS   = RL  * (VREF – Vout) / Vout
// ================================================================
float calcRS(int raw, float RL) {
  float Vout = (raw / 4095.0f) * VREF;
  if (Vout < 0.001f) Vout = 0.001f;   // prevent division by zero
  return RL * (VREF - Vout) / Vout;
}

// ================================================================
//  calcPPM: Convert the RS/R0 ratio to gas concentration in ppm
//           ppm = (RS/R0 / a)^(1/b)
//  Returns -1.0 if the ratio is zero or the result is invalid.
// ================================================================
float calcPPM(float RS, float R0, const GasCurve& c) {
  if (RS <= 0.0f || R0 <= 0.0f) return -1.0f;
  float ratio = RS / R0;
  float ppm   = powf(ratio / c.a, 1.0f / c.b);
  return (ppm > 0.0f) ? ppm : -1.0f;
}

// ppm → % volume  (1 % volume = 10,000 ppm)
float ppmToPercent(float ppm) {
  return ppm / 10000.0f;
}

// ── Sensor result bundle ─────────────────────────────────────────
struct GasReading {
  const char* label;
  int         pin;
  int         raw;
  float       voltage;
  float       RS;
  float       RS_R0;
  float       ppm;
  float       percent;
  float       limit;
};

// ================================================================
//  readGas: Perform ADC reading and compute all derived values
// ================================================================
GasReading readGas(const char* label, int pin,
                   float RL,    float R0,
                   const GasCurve& curve, float limit)
{
  int   raw  = analogRead(pin);
  float volt = (raw / 4095.0f) * VREF;
  float RS   = calcRS(raw, RL);
  float ppm  = calcPPM(RS, R0, curve);
  return { label, pin, raw, volt, RS, RS / R0,
           ppm, ppmToPercent(ppm), limit };
}

// ================================================================
//  printReading: Output formatted results to the Serial Monitor
// ================================================================
void printReading(const GasReading& g) {
  Serial.printf("\n  [%s]  GPIO %d\n", g.label, g.pin);
  Serial.printf("    Raw ADC  : %d\n",     g.raw);
  Serial.printf("    Voltage  : %.3f V\n", g.voltage);
  Serial.printf("    RS       : %.1f Ohm\n", g.RS);
  Serial.printf("    RS / R0  : %.4f\n",   g.RS_R0);

  if (g.ppm >= 0.0f) {
    Serial.printf("    Conc.    : %.4f ppm\n",    g.ppm);
    Serial.printf("    Conc.    : %.6f %% vol\n", g.percent);

    if (g.ppm >= g.limit)
      Serial.printf("    !! WARNING: %.4f ppm exceeds OSHA limit (%.1f ppm)!\n",
                    g.ppm, g.limit);
    else
      Serial.printf("    Status   : Safe (OSHA limit: %.1f ppm)\n", g.limit);
  } else {
    Serial.println("    Conc.    : Out of sensor measurement range");
  }
}

// ================================================================
void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println("\n===========================================");
  Serial.println("   MICS-6814 Triple Gas Sensor – ESP32");
  Serial.println("===========================================");

  pinMode(CO_PIN,  INPUT);
  pinMode(NO2_PIN, INPUT);
  pinMode(NH3_PIN, INPUT);

  // ADC_11db allows reading voltages up to ~3.1–3.3 V
  // Note: newer ESP32 Arduino cores use ADC_ATTEN_DB_11 or DB_12
  analogSetAttenuation(ADC_11db);

  if (CALIBRATION_MODE) {
    Serial.println("MODE: CALIBRATION");
    Serial.println(">> Expose sensor to CLEAN AIR and wait 24 h.");
    Serial.println(">> Copy the stable RS values into R0_CO, R0_NO2, R0_NH3.");
  } else {
    Serial.println("MODE: MEASUREMENT");
    Serial.println("Sensor warming up...");
    Serial.println("(For best accuracy run warm-up for at least 1 hour)\n");
  }
}

// ================================================================
void loop() {
  GasReading co  = readGas("CO ",  CO_PIN,  RL_CO,  R0_CO,  CURVE_CO,  LIMIT_CO);
  GasReading no2 = readGas("NO2",  NO2_PIN, RL_NO2, R0_NO2, CURVE_NO2, LIMIT_NO2);
  GasReading nh3 = readGas("NH3",  NH3_PIN, RL_NH3, R0_NH3, CURVE_NH3, LIMIT_NH3);

  Serial.println("===========================================");
  Serial.printf("  Timestamp: %lu ms\n", millis());

  if (CALIBRATION_MODE) {
    // Print only RS values to identify stable R0 in clean air
    Serial.println("\n  CALIBRATION – RS values in clean air:");
    Serial.printf("    R0_CO  candidate : %.1f Ohm\n", co.RS);
    Serial.printf("    R0_NO2 candidate : %.1f Ohm\n", no2.RS);
    Serial.printf("    R0_NH3 candidate : %.1f Ohm\n", nh3.RS);
    Serial.println("\n  Copy the stable values above into the R0_xx constants.");
  } else {
    printReading(co);
    printReading(no2);
    printReading(nh3);
  }

  Serial.println("\n===========================================\n");
  delay(2000);
}
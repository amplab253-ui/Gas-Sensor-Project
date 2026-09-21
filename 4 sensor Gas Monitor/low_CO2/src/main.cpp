#include <Arduino.h>
#include "soc/soc.h"           // for brownout disable
#include "soc/rtc_cntl_reg.h"  // for brownout disable

const int sensorPin = 34; // ESP32 ADC pin connected to the MG-811 sensor

// ============================================================
//  CALIBRATION CONSTANTS  –  Adjust these for your sensor
// ============================================================
// VZERO: The sensor output voltage measured in fresh outdoor air
// (~400 PPM CO2). Run the calibration sketch first to find this
// value, then replace the default below with your actual reading.
#define VZERO        2.602f  // Volts at CO2_REF_PPM (your calibrated value)

// SENSITIVITY: How many volts the output drops per 10× increase
// in CO2 concentration (one decade on a log10 scale).
// MG-811 datasheet typical value ≈ –0.06 V/decade.
// Negative because output voltage FALLS as CO2 RISES.
#define SENSITIVITY  -0.06f  // V per decade

// The known CO2 concentration used when measuring VZERO (fresh air)
#define CO2_REF_PPM  400.0f  // PPM
// ============================================================

void setup() {
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0); // disable brownout detector

  Serial.begin(115200);
  analogReadResolution(12); // ESP32 12-bit ADC (0 – 4095)

  Serial.println("=========================================");
  Serial.println("  MG-811 CO2 Sensor – ESP32 Monitor");
  Serial.println("=========================================");
  Serial.print("  Zero-point (VZERO)  : ");
  Serial.print(VZERO, 3);
  Serial.println(" V");
  Serial.print("  Sensitivity         : ");
  Serial.print(SENSITIVITY, 3);
  Serial.println(" V/decade");
  Serial.print("  Reference CO2       : ");
  Serial.print(CO2_REF_PPM, 0);
  Serial.println(" PPM");
  Serial.println("=========================================");
}

void loop() {
  long totalRaw  = 0;
  const int numSamples = 100;

  // Average 100 readings to reduce electrical noise
  for (int i = 0; i < numSamples; i++) {
    totalRaw += analogRead(sensorPin);
    delay(50); // 50 ms between samples
  }

  // Step 1 – Average raw ADC value
  float avgRaw = totalRaw / (float)numSamples;

  // Step 2 – Convert ADC value to voltage at the ESP32 pin (0–5 V range)
  float pinVoltage = (avgRaw / 4095.0f) * 5.0f;

  /* ── Voltage-Divider Correction ──────────────────────────────────
   * If you used a resistor divider to drop the sensor voltage before
   * feeding it into the ESP32, undo that scaling here to recover the
   * true sensor output voltage.
   *
   * Formula:  sensorVoltage = pinVoltage * (R1 + R2) / R2
   *
   * Example for R1 = 10 kΩ, R2 = 20 kΩ:
   *   float sensorVoltage = pinVoltage * ((10000.0f + 20000.0f) / 20000.0f);
   *
   * If NO divider is used, keep the line below as-is.
   * ────────────────────────────────────────────────────────────────*/
  float sensorVoltage = pinVoltage; // No divider — change if needed

  // Step 3 – Convert voltage to CO2 PPM using the log-linear model:
  //
  //   The MG-811 output follows: V = VZERO + SENSITIVITY × log10(ppm / CO2_REF_PPM)
  //
  //   Rearranging to solve for ppm:
  //   ppm = CO2_REF_PPM × 10 ^ ( (V – VZERO) / SENSITIVITY )
  //
  float ppm = CO2_REF_PPM * pow(10.0f, (sensorVoltage - VZERO) / SENSITIVITY);

  // Clamp to a physically meaningful range (MG-811: 350 – 10,000 PPM)
  ppm = constrain(ppm, 0.0f, 10000.0f);

  // Step 4 – Print both voltage and concentration
  Serial.print("Voltage: ");
  Serial.print(sensorVoltage, 3);
  Serial.print(" V  |  CO2: ");
  Serial.print(ppm, 1);
  Serial.println(" PPM");

  delay(3000); // Wait 3 seconds before next reading
}
// Calibration

#include <Arduino.h>
#include <Wire.h>
#include "SparkFun_STC3x_Arduino_Library.h"

// Custom I2C Pins
#define I2C_SDA 25
#define I2C_SCL 26

STC3x mySensor;

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("SparkFun STC3x Calibration Sequence");

  // Initialize I2C with custom pins
  Wire.begin(I2C_SDA, I2C_SCL);

  if (mySensor.begin() == false) {
    Serial.println("Sensor not detected. Freezing...");
    while (1);
  }

  // Set binary gas to 0-25% CO2 in Air to give us the best resolution at the low end
  if (mySensor.setBinaryGas(STC3X_BINARY_GAS_CO2_AIR_25) == false) {
      Serial.println("Could not set the binary gas configuration!");
      while (1);
  }

  Serial.println("Sensor ready. Attempting Forced Recalibration...");
  Serial.println("NOTE: Ensure the sensor is currently in fresh, ambient air.");
  
  // Force a recalibration with a concentration of 0.04% (Standard Ambient Air)
  // If you are calibrating with pure Nitrogen, you would use 0.0 instead.
  if (mySensor.forcedRecalibration(0.04) == true) {
    Serial.println("SUCCESS: Sensor calibrated to 0.04% CO2.");
  } else {
    Serial.println("FAILED: Recalibration failed. Check wiring and power.");
  }

  // Note: The STC3x also has an Automatic Self-Calibration (ASC) feature for environments 
  // that occasionally drop to 0%. You can enable it with:
  // mySensor.enableAutomaticSelfCalibration();

  Serial.println("Starting measurements...");
  Serial.println("-------------------------");
}

void loop() {
  if (mySensor.measureGasConcentration()) {
    Serial.print("CO2 Concentration: ");
    Serial.print(mySensor.getCO2(), 3); // Print with 3 decimal places
    Serial.println(" %");

    Serial.print("Temperature: ");
    Serial.print(mySensor.getTemperature(), 2);
    Serial.println(" C");
    Serial.println("---");
  } else {
    Serial.println("Failed to read data from the sensor.");
  }

  delay(1000);
}
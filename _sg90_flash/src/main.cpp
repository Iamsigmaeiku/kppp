#include <ESP32Servo.h>

Servo myServo;
// 訊號 → GPIO18（DevKit 絲印標 18）
const int pinSignal = 18;

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("SG90 boot");

  // ESP32Servo 3.x：不 allocate + setPeriodHertz 常會沒 PWM
  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  ESP32PWM::allocateTimer(2);
  ESP32PWM::allocateTimer(3);
  myServo.setPeriodHertz(50);
  myServo.attach(pinSignal, 500, 2400);  // us：常見 SG90 脈寬

  Serial.printf("attached GPIO%d\n", pinSignal);
  myServo.write(90);
  delay(1000);
}

void loop() {
  Serial.println("-> 180");
  for (int pos = 0; pos <= 180; pos += 1) {
    myServo.write(pos);
    delay(15);
  }
  Serial.println("-> 0");
  for (int pos = 180; pos >= 0; pos -= 1) {
    myServo.write(pos);
    delay(15);
  }
}

"""Hardware verification script for esp32_face_os over USB serial (COM3)."""

import json
import sys
import time
import serial


def send_and_wait_res(ser: serial.Serial, payload: dict, timeout: float = 3.0) -> dict:
    req_id = payload.get("id")
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    ser.write(line.encode("utf-8"))
    ser.flush()

    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = ser.readline().decode("utf-8", errors="replace").strip()
        if not raw.startswith("{"):
            if raw:
                print(f"  [LOG] {raw}")
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        print(f"  [RX] {msg}")
        if req_id and msg.get("t") == "res" and msg.get("id") == req_id:
            return msg
    raise TimeoutError(f"Timed out waiting for response to {payload}")


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    print(f"Opening {port} @ 115200...")
    with serial.Serial(port, 115200, timeout=0.5) as ser:
        ser.dtr = False
        ser.rts = False
        time.sleep(2.5)
        ser.reset_input_buffer()

        print("\n1. Testing ping...")
        res = send_and_wait_res(ser, {"t": "ping", "id": "p1"})
        assert res["ok"] == 1

        print("\n2. Testing QMI8658 IMU...")
        imu = send_and_wait_res(ser, {"t": "imu_get", "id": "i1"})
        assert imu["ok"] == 1, f"IMU not ready: {imu}"

        print("\n3. Testing BLE Passive Observer...")
        time.sleep(1.0)
        ble = send_and_wait_res(ser, {"t": "ble_get", "id": "b1"})
        assert ble["ok"] == 1
        print(f"  BLE devices seen: {ble['cnt']}")

        print("\n4. Testing Dynamic Agent GPIO 16...")
        gcfg = send_and_wait_res(
            ser, {"t": "gpio_cfg", "pin": 16, "dir": "out", "pull": "none", "val": 1, "id": "g1"}
        )
        assert gcfg["ok"] == 1
        gget = send_and_wait_res(ser, {"t": "gpio_get", "pin": 16, "id": "g2"})
        assert gget["ok"] == 1 and gget["val"] == 1

        # Also test reserved pin rejection (GPIO 2 backlight must be denied)
        gdeny = send_and_wait_res(
            ser, {"t": "gpio_cfg", "pin": 2, "dir": "out", "pull": "none", "val": 0, "id": "g3"}
        )
        assert gdeny["ok"] == 0, "Reserved pin 2 should have been rejected!"

        print("\n5. Testing LVGL Synced Face + Subtitle + Visemes...")
        ser.write(
            (
                json.dumps(
                    {
                        "t": "dbg",
                        "quixi": "ONLINE",
                        "voip": "IN_CALL",
                        "srv_ms": 42,
                        "bt_spk": "OK",
                    }
                )
                + "\n"
            ).encode("utf-8")
        )
        ser.write(
            (
                json.dumps(
                    {
                        "t": "sync",
                        "emo": "SPEAKING",
                        "sub": "Hello! QuIXI + Zephyr OS is alive!",
                        "mouth": 85,
                    }
                )
                + "\n"
            ).encode("utf-8")
        )
        for v in [90, 40, 80, 20, 65, 10, 0]:
            ser.write((json.dumps({"t": "mouth", "v": v}) + "\n").encode("utf-8"))
            time.sleep(0.08)

        ser.write(
            (
                json.dumps(
                    {
                        "t": "state",
                        "emo": "HAPPY",
                        "sub": "Ready for RPi Zero 2 W!",
                    }
                )
                + "\n"
            ).encode("utf-8")
        )

    print("\nALL HARDWARE SERIAL TESTS PASSED!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

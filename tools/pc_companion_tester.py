"""
Interactive PC Companion Tester for Waveshare ESP32-S3-Touch-LCD-1.28-B.
Simulates the Raspberry Pi Zero 2 W USB link directly on Windows/Linux.
"""

import argparse
import json
import threading
import time
import serial


def is_clean_text(s: str) -> bool:
    return "\ufffd" not in s and all(ord(c) >= 0x20 or c in "\t" for c in s)


def reader_loop(ser: serial.Serial, stop_evt: threading.Event) -> None:
    while not stop_evt.is_set():
        try:
            raw_bytes = ser.readline()
            if not raw_bytes:
                continue
            raw = raw_bytes.decode("utf-8", errors="replace").strip()
            if not raw:
                continue
            if raw.startswith("{"):
                print(f"\n[ESP32 -> PC] {raw}")
            elif is_clean_text(raw):
                print(f"\n[BOOT/LOG] {raw}")
        except Exception:
            break


def send_json(ser: serial.Serial, payload: dict) -> None:
    line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    ser.write(line.encode("utf-8"))
    ser.flush()


def simulate_speech(ser: serial.Serial) -> None:
    phrases = [
        ("Hey there!", [30, 80, 60, 90, 40, 20]),
        ("I am your AI companion.", [50, 90, 40, 85, 70, 30, 75, 20]),
        ("Touch my face or shake me anytime!", [45, 80, 50, 95, 65, 35, 85, 40, 10]),
    ]
    send_json(ser, {"t": "dbg", "quixi": "ONLINE", "voip": "IN_CALL", "srv_ms": 58, "bt_spk": "OK"})
    for text, env in phrases:
        send_json(ser, {"t": "sync", "emo": "SPEAKING", "sub": text, "mouth": env[0]})
        for v in env:
            send_json(ser, {"t": "mouth", "v": v})
            time.sleep(0.08)
        time.sleep(0.25)
    send_json(ser, {"t": "state", "emo": "HAPPY", "sub": "Ready for commands!"})
    time.sleep(1.5)
    send_json(ser, {"t": "state", "emo": "IDLE", "sub": "Tap face to speak"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive PC Companion Tester for esp32_face_os")
    parser.add_argument("port", nargs="?", default="COM3", help="Serial port (default: COM3)")
    parser.add_argument("--baud", type=int, default=921600, help="Baud rate (default: 921600)")
    args = parser.parse_args()

    print(f"Connecting to {args.port} @ {args.baud}...")
    ser = serial.Serial()
    ser.port = args.port
    ser.baudrate = args.baud
    ser.timeout = 0.2
    ser.dtr = False
    ser.rts = False
    ser.open()
    time.sleep(0.2)
    ser.reset_input_buffer()

    stop_evt = threading.Event()
    t = threading.Thread(target=reader_loop, args=(ser, stop_evt), daemon=True)
    t.start()

    print(
        "\nCommands:\n"
        "  speak                                 - Simulate AI voice response with subtitles & lip-sync\n"
        "  emo <NAME>                            - Set emotion (IDLE, LISTENING, THINKING, SPEAKING, HAPPY, SAD, SURPRISED, SLEEPING, ALERT)\n"
        "  imu                                   - Read QMI8658 6-axis IMU & temperature\n"
        "  ble                                   - Dump BLE nearby devices\n"
        "  gpio <pin> <0|1>                      - Configure GPIO as output + set initial val\n"
        "  gcfg <pin> <in|out> [none|up|down] [0|1] - Configure GPIO direction & pull\n"
        "  gset <pin> <0|1>                      - Set output GPIO value\n"
        "  gread <pin>                           - Read GPIO + Battery voltage\n"
        "  bright <10..100>                      - Set LCD backlight brightness (%)\n"
        "  dbg <quixi> <voip> <ms> <bt>          - Update debug telemetry link banner\n"
        "  ping                                  - Send serial ping\n"
        "  quit                                  - Exit\n"
    )

    try:
        while True:
            try:
                cmd = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not cmd:
                continue
            parts = cmd.split()
            op = parts[0].lower()

            try:
                if op in ("quit", "exit", "q"):
                    break
                elif op == "speak":
                    simulate_speech(ser)
                elif op == "emo" and len(parts) >= 2:
                    send_json(ser, {"t": "state", "emo": parts[1].upper(), "sub": f"Emotion: {parts[1].upper()}"})
                elif op == "imu":
                    send_json(ser, {"t": "imu_get", "id": "imu_cli"})
                elif op == "ble":
                    send_json(ser, {"t": "ble_get", "id": "ble_cli"})
                elif op == "gpio" and len(parts) >= 3:
                    send_json(
                        ser,
                        {
                            "t": "gpio_cfg",
                            "pin": int(parts[1]),
                            "dir": "out",
                            "pull": "none",
                            "val": int(parts[2]),
                            "id": "gpio_cli",
                        },
                    )
                elif op == "gcfg" and len(parts) >= 3:
                    pull = parts[3] if len(parts) >= 4 else "none"
                    val = int(parts[4]) if len(parts) >= 5 else 0
                    send_json(
                        ser,
                        {
                            "t": "gpio_cfg",
                            "pin": int(parts[1]),
                            "dir": parts[2].lower(),
                            "pull": pull.lower(),
                            "val": val,
                            "id": "gcfg_cli",
                        },
                    )
                elif op == "gset" and len(parts) >= 3:
                    send_json(ser, {"t": "gpio_set", "pin": int(parts[1]), "val": int(parts[2]), "id": "gset_cli"})
                elif op == "gread" and len(parts) >= 2:
                    send_json(ser, {"t": "gpio_get", "pin": int(parts[1]), "id": "gread_cli"})
                elif op == "bright" and len(parts) >= 2:
                    send_json(ser, {"t": "cfg", "bright": int(parts[1]), "id": "cfg_cli"})
                elif op == "dbg" and len(parts) >= 5:
                    send_json(
                        ser,
                        {
                            "t": "dbg",
                            "quixi": parts[1],
                            "voip": parts[2],
                            "srv_ms": int(parts[3]),
                            "bt_spk": parts[4],
                        },
                    )
                elif op == "ping":
                    send_json(ser, {"t": "ping", "id": "ping_cli"})
                else:
                    print("Unknown command or missing arguments.")
            except ValueError:
                print("Invalid integer argument. Usage examples: gpio <pin> <0|1>, bright <10..100>, dbg <q> <v> <ms> <bt>")
    finally:
        stop_evt.set()
        ser.close()


if __name__ == "__main__":
    main()

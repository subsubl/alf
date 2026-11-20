"""Hardware verification script for esp32_face_os over USB serial (COM3 @ 921600)."""

import argparse
import json
import time
import serial


def _read_json_line(ser: serial.Serial) -> dict | None:
    raw_bytes = ser.readline()
    if not raw_bytes:
        return None
    raw = raw_bytes.decode("utf-8", errors="replace").strip()
    if not raw.startswith("{"):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def open_serial_clean(port: str, baud: int, timeout: float = 0.35) -> serial.Serial:
    """Open serial port with DTR/RTS disabled pre-open to avoid CH343 reset/download glitches."""
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = timeout
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def wait_for_ready(ser: serial.Serial, timeout: float = 10.0) -> None:
    """Drain 115200-baud ESP-ROM boot noise and confirm clean 921600 NDJSON readiness."""
    time.sleep(0.2)
    ser.reset_input_buffer()

    start = time.time()
    deadline = start + timeout
    last_ping = 0.0
    ping_seq = 0
    did_rts_reset = False
    while time.time() < deadline:
        now = time.time()
        if not did_rts_reset and (now - start) >= 1.5:
            did_rts_reset = True
            ser.rts = True
            time.sleep(0.05)
            ser.rts = False
            last_ping = now

        if now - last_ping >= 0.45:
            ping_seq += 1
            pid = f"boot_ping_{ping_seq}"
            ser.write((json.dumps({"t": "ping", "id": pid}, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
            ser.flush()
            last_ping = now

        msg = _read_json_line(ser)
        if not msg:
            continue
        print(f"  [RX] {msg}")
        if msg.get("t") == "evt" and msg.get("src") == "sys" and msg.get("act") == "boot":
            return
        if msg.get("t") == "res" and str(msg.get("id", "")).startswith("boot_ping_") and msg.get("ok") == 1:
            return
    raise TimeoutError("Timed out waiting for ESP32-S3 NDJSON readiness at 921600 baud")


def send_and_wait_res(ser: serial.Serial, payload: dict, timeout: float = 3.0) -> dict:
    req_id = payload.get("id")
    line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    ser.write(line.encode("utf-8"))
    ser.flush()

    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = _read_json_line(ser)
        if not msg:
            continue
        print(f"  [RX] {msg}")
        if req_id and msg.get("t") == "res" and msg.get("id") == req_id:
            return msg
    raise TimeoutError(f"Timed out waiting for response to {payload}")


def send_fire_and_forget(ser: serial.Serial, payload: dict) -> None:
    ser.write((json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
    ser.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify esp32_face_os USB NDJSON streams")
    parser.add_argument("port", nargs="?", default="COM3", help="Serial port (default: COM3)")
    parser.add_argument("--baud", type=int, default=921600, help="Baud rate (default: 921600)")
    args = parser.parse_args()

    print(f"Opening {args.port} @ {args.baud}...")
    with open_serial_clean(args.port, args.baud, timeout=0.35) as ser:
        wait_for_ready(ser)

        try:
            print("\n1. Testing ping...")
            res = send_and_wait_res(ser, {"t": "ping", "id": "p1"})
            assert res["ok"] == 1
            assert isinstance(res.get("up"), int) and res["up"] >= 0, f"Invalid uptime: {res}"

            print("\n2. Testing QMI8658 IMU...")
            imu = send_and_wait_res(ser, {"t": "imu_get", "id": "i1"})
            assert imu["ok"] == 1, f"IMU not ready: {imu}"
            for k in ("ax", "ay", "az", "gx", "gy", "gz", "temp"):
                assert isinstance(imu.get(k), (int, float)), f"Missing/non-numeric IMU key {k}: {imu}"
            assert imu.get("gest") in {"none", "shake", "tilt", "pickup", "double_tap"}, f"Bad gest: {imu}"
            assert imu.get("angle") in {0, 90, 180, 270}, f"Bad angle: {imu}"

            print("\n3. Testing BLE Passive Observer...")
            time.sleep(1.0)
            ble = send_and_wait_res(ser, {"t": "ble_get", "id": "b1"})
            assert ble["ok"] == 1
            assert isinstance(ble.get("cnt"), int) and 0 <= ble["cnt"] <= 32, f"Invalid BLE cnt: {ble}"
            assert isinstance(ble.get("ble"), list) and len(ble["ble"]) == ble["cnt"], f"BLE list/cnt mismatch: {ble}"
            for dev in ble["ble"]:
                assert isinstance(dev.get("mac"), str), f"Missing mac: {dev}"
                assert isinstance(dev.get("type"), int), f"Missing addr type: {dev}"
                assert isinstance(dev.get("name"), str), f"Missing name: {dev}"
                assert isinstance(dev.get("rssi"), int), f"Missing rssi: {dev}"
            print(f"  BLE devices seen: {ble['cnt']}")

            print("\n4. Testing Dynamic Agent GPIO 16 & Vbat ADC...")
            gcfg = send_and_wait_res(
                ser, {"t": "gpio_cfg", "pin": 16, "dir": "out", "pull": "none", "val": 1, "id": "g1"}
            )
            assert gcfg["ok"] == 1 and gcfg.get("err") == 0, f"gpio_cfg out=1 failed: {gcfg}"

            gget = send_and_wait_res(ser, {"t": "gpio_get", "pin": 16, "id": "g2"})
            assert gget["ok"] == 1 and gget["val"] == 1 and gget.get("err") == 0, f"gpio_get!=1: {gget}"
            assert isinstance(gget.get("vbat_mv"), int) and gget["vbat_mv"] > 0, f"Invalid vbat_mv: {gget}"

            gset0 = send_and_wait_res(ser, {"t": "gpio_set", "pin": 16, "val": 0, "id": "g3"})
            assert gset0["ok"] == 1 and gset0["val"] == 0 and gset0.get("err") == 0, f"gpio_set 0 failed: {gset0}"
            gget0 = send_and_wait_res(ser, {"t": "gpio_get", "pin": 16, "id": "g4"})
            assert gget0["ok"] == 1 and gget0["val"] == 0, f"gpio_get!=0 after gpio_set 0: {gget0}"

            gset1 = send_and_wait_res(ser, {"t": "gpio_set", "pin": 16, "val": 1, "id": "g5"})
            assert gset1["ok"] == 1 and gset1["val"] == 1 and gset1.get("err") == 0, f"gpio_set 1 failed: {gset1}"
            gget1 = send_and_wait_res(ser, {"t": "gpio_get", "pin": 16, "id": "g6"})
            assert gget1["ok"] == 1 and gget1["val"] == 1, f"gpio_get!=1 after gpio_set 1: {gget1}"

            gup = send_and_wait_res(
                ser, {"t": "gpio_cfg", "pin": 16, "dir": "in", "pull": "up", "id": "g_up"}
            )
            assert gup["ok"] == 1 and gup.get("err") == 0, f"gpio_cfg in/up failed: {gup}"
            gget_up = send_and_wait_res(ser, {"t": "gpio_get", "pin": 16, "id": "g_up_r"})
            assert gget_up["ok"] == 1 and gget_up.get("err") == 0

            gdn = send_and_wait_res(
                ser, {"t": "gpio_cfg", "pin": 16, "dir": "in", "pull": "down", "id": "g_dn"}
            )
            assert gdn["ok"] == 1 and gdn.get("err") == 0, f"gpio_cfg in/down failed: {gdn}"
            gget_dn = send_and_wait_res(ser, {"t": "gpio_get", "pin": 16, "id": "g_dn_r"})
            assert gget_dn["ok"] == 1 and gget_dn.get("err") == 0

            print("\n5. Testing Protected & Invalid GPIO Rejection...")
            for rpin in (2, 4, 5, 43):
                gdeny = send_and_wait_res(
                    ser, {"t": "gpio_cfg", "pin": rpin, "dir": "out", "pull": "none", "val": 0, "id": f"deny_{rpin}"}
                )
                assert gdeny["ok"] == 0 and gdeny.get("err") == -13, f"Reserved pin {rpin} not -13: {gdeny}"

            gdeny_get = send_and_wait_res(ser, {"t": "gpio_get", "pin": 2, "id": "deny_get_2"})
            assert gdeny_get["ok"] == 0 and gdeny_get.get("err") == -13, f"Reserved pin 2 get not -13: {gdeny_get}"

            for bad_id, bad_cmd in (
                ("bad_cfg_neg", {"t": "gpio_cfg", "pin": -1, "dir": "in", "id": "bad_cfg_neg"}),
                ("bad_cfg_miss", {"t": "gpio_cfg", "dir": "in", "id": "bad_cfg_miss"}),
                ("bad_cfg_dir", {"t": "gpio_cfg", "pin": 16, "dir": "OUT", "id": "bad_cfg_dir"}),
                ("bad_cfg_pull", {"t": "gpio_cfg", "pin": 16, "dir": "in", "pull": "bogus", "id": "bad_cfg_pull"}),
                ("bad_set_neg", {"t": "gpio_set", "pin": -1, "val": 1, "id": "bad_set_neg"}),
                ("bad_set_miss", {"t": "gpio_set", "val": 1, "id": "bad_set_miss"}),
                ("bad_set_val_miss", {"t": "gpio_set", "pin": 16, "id": "bad_set_val_miss"}),
                ("bad_set_val_str", {"t": "gpio_set", "pin": 16, "val": "high", "id": "bad_set_val_str"}),
                ("bad_get_neg", {"t": "gpio_get", "pin": -1, "id": "bad_get_neg"}),
                ("bad_get_miss", {"t": "gpio_get", "id": "bad_get_miss"}),
            ):
                bres = send_and_wait_res(ser, bad_cmd)
                assert bres["ok"] == 0 and bres.get("err") == -22, f"{bad_id} expected -22: {bres}"

            # Invalid integer token ("16foo") -> -EINVAL (-22)
            ser.write(b'{"t":"gpio_get","pin":16foo,"id":"bad_int_tok"}\n')
            ser.flush()
            deadline = time.time() + 3.0
            tok_res = None
            while time.time() < deadline:
                msg = _read_json_line(ser)
                if not msg:
                    continue
                print(f"  [RX] {msg}")
                if msg.get("t") == "res" and msg.get("id") == "bad_int_tok":
                    tok_res = msg
                    break
            assert tok_res is not None and tok_res["ok"] == 0 and tok_res.get("err") == -22, (
                f"bad_int_tok expected -22: {tok_res}"
            )

            # Truncated NDJSON line must be dropped without executing gpio_get
            ser.write(b'{"t":"gpio_get","pin":16\n')
            ser.write((json.dumps({"t": "ping", "id": "p_trunc_check"}, separators=(",", ":")) + "\n").encode("utf-8"))
            ser.flush()
            deadline = time.time() + 3.0
            trunc_res = None
            while time.time() < deadline:
                msg = _read_json_line(ser)
                if not msg:
                    continue
                print(f"  [RX] {msg}")
                if msg.get("t") == "res":
                    assert msg.get("id") == "p_trunc_check", f"Truncated frame produced unexpected res: {msg}"
                    trunc_res = msg
                    break
            assert trunc_res is not None and trunc_res["ok"] == 1, f"p_trunc_check failed: {trunc_res}"

            print("\n6. Testing Inbound PWM Brightness Config (cfg) & Network Identity Screen (net)...")
            cres = send_and_wait_res(ser, {"t": "cfg", "bright": 45, "id": "c1"})
            assert cres["ok"] == 1 and cres.get("bright") == 45, f"cfg bright failed: {cres}"
            nres = send_and_wait_res(
                ser,
                {
                    "t": "net",
                    "ip": "192.168.1.42",
                    "ixi": "1TamagciPiZero2WIxianWalletAddressTest",
                    "id": "n1",
                },
            )
            assert nres["ok"] == 1, f"net info command failed: {nres}"

            print("\n7. Testing LVGL Synced Face, Escaped Quotes, Subtitle & Visemes...")
            send_fire_and_forget(
                ser, {"t": "dbg", "quixi": "ONLINE", "voip": "IN_CALL", "srv_ms": 42, "bt_spk": "OK"}
            )
            send_fire_and_forget(
                ser,
                {
                    "t": "sync",
                    "emo": "SPEAKING",
                    "sub": 'Test "v":99 "mouth":0\\ok — QuIXI + Zephyr OS is alive!',
                    "mouth": 85,
                },
            )
            for v in [90, 40, 80, 20, 65, 10, 0]:
                send_fire_and_forget(ser, {"t": "mouth", "v": v})
                time.sleep(0.08)

            send_fire_and_forget(
                ser, {"t": "state", "emo": "HAPPY", "sub": "Ready for RPi Zero 2 W!"}
            )
            res_end = send_and_wait_res(ser, {"t": "ping", "id": "p_end"})
            assert res_end["ok"] == 1
        finally:
            print("\n8. Restoring GPIO 16, Brightness 100%, and IDLE Face...")
            try:
                send_and_wait_res(
                    ser, {"t": "gpio_cfg", "pin": 16, "dir": "in", "pull": "none", "id": "g_rst"}
                )
                send_and_wait_res(ser, {"t": "cfg", "bright": 100, "id": "c_rst"})
                send_fire_and_forget(
                    ser, {"t": "dbg", "quixi": "WAIT", "voip": "IDLE", "srv_ms": -1, "bt_spk": "WAIT"}
                )
                send_fire_and_forget(
                    ser, {"t": "state", "emo": "IDLE", "sub": "Tap face to speak", "mic": 0}
                )
            except Exception as exc:
                print(f"  [WARN] Cleanup warning: {exc}")

    print("\nALL HARDWARE SERIAL TESTS PASSED!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

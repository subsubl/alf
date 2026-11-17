cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(esp32_face_os)

target_sources(app PRIVATE
    src/main.c
    src/ui_screens.c
    src/imu_qmi8658.c
    src/ble_scanner.c
    src/gpio_agent.c
    src/serial_proto.c
)

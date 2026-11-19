#ifndef SERIAL_PROTO_H
#define SERIAL_PROTO_H

/**
 * @brief Initialize interrupt-driven UART0 NDJSON serial handler.
 */
int serial_proto_init(void);

/**
 * @brief Send a newline-terminated JSON string safely over UART0.
 */
void serial_send_json(const char *json_line);

/**
 * @brief Drain queued NDJSON commands and dispatch to UI / GPIO / BLE / IMU.
 */
void serial_proto_poll(void);

/**
 * @brief Format and transmit an asynchronous BLE device list event.
 */
void serial_send_ble_evt(void);

#endif /* SERIAL_PROTO_H */

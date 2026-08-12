# Raw WebSocket frame schema v1

Файл имеет расширение `.frames.gz`, использует gzip level 1, вращается по UTC
hour и после lossless decompression начинается bytes:

```text
POLYMATH-FRAMES-v1\n
```

Далее без separators идут records. Header — big-endian struct `>QQQBBHI`:

| Поле | Type | Смысл |
|---|---:|---|
| `sequence` | `uint64` | непрерывный local sequence внутри stream/run |
| `receive_timestamp_ns` | `uint64` | UTC wall clock в момент receive/send |
| `receive_monotonic_ns` | `uint64` | monotonic clock для gap/latency checks |
| `direction` | `uint8` | `0` inbound, `1` outbound |
| `connection_id_length` | `uint8` | число следующих ASCII bytes |
| `reserved` | `uint16` | всегда zero в v1 |
| `payload_length` | `uint32` | число raw UTF-8 payload bytes |

После header идут `connection_id_length` ASCII bytes и затем ровно
`payload_length` raw UTF-8 bytes. WebSocket payload не нормализуется и не
пересериализуется. Новая hourly file продолжает sequence предыдущей.

Compression выбрана после representative benchmark: 50 MiB production frames
сжались до 9.8 MiB за 0.31 секунды. Это уменьшает projected daily storage
примерно в пять раз без изменения payload или replay semantics.

Reference decoder: `polymath_1M.collector.storage.iter_raw_frames`.
Replay/continuity check: `polymath_1M collector-replay --run-dir <run-dir>`.

Incomplete header/payload, unknown direction/reserved value, invalid UTF-8 или
sequence gap делают replay invalid; повреждённый хвост не игнорируется.

# Load test of the service layer

Real HTTP server on localhost with the model replaced by a stub. It measures limits and queueing, not model speed, and was never run against the live service or Groq.

| scenario | requests | status codes | requests/s | median ms | p95 ms |
|---|---|---|---|---|---|
| A: 200 visitors, 20 at once, instant model | 200 | {200: 200} | 1089.4 | 16 | 21 |
| B: one visitor sends 60 requests | 60 | {200: 10, 429: 50} | 1323.4 | 7 | 8 |
| C: 60 visitors, daily cap 25 | 60 | {200: 25, 429: 35} | 1364.0 | 7 | 9 |
| D: 12 simultaneous visitors, 0.5 s model | 12 | {200: 12} | 2.0 | 3252 | 5502 |

Expected: A all 200 OK; B 10 OK and 50 rate-limited (429); C 25 OK and 35 refused (429); D about 12 x 0.5 s = 6 s wall time because one question runs at a time, so the last visitor waits for everyone ahead.

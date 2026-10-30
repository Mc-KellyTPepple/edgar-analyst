| system | n | tier 1 | tier 2 | tier 3 | tier 4 (text) | answerable (1-3) | refusals | false refusals | citation recall | citation precision | retrieval hit | tool calls | tokens/q | p95 s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| closed_book | 164 | 10.0% | 8.3% | 40.0% | - | 17.3% | 71.4% | 37.3% | 0.0% | - | - | 0.0 | 267 | 1.49 |
| facts_agent | 187 | 100.0% | 100.0% | 100.0% | 0.0% | 100.0% | 100.0% | 0.0% | 100.0% | 100.0% | 0.0% | 1.93 | 2135 | 58.43 |
| full_agent | 170 | 100.0% | 100.0% | 100.0% | 50.0% | 100.0% | 100.0% | 0.0% | 99.8% | 100.0% | 100.0% | 2.04 | 2716 | 59.69 |

| system | model | answerable questions | accuracy |
|---|---|---|---|
| closed_book | openai/gpt-oss-120b | 150 | 17.3% |
| facts_agent | openai/gpt-oss-120b | 63 | 63.5% |
| facts_agent | openai/gpt-oss-20b | 110 | 100.0% |
| full_agent | openai/gpt-oss-120b | 121 | 100.0% |
| full_agent | openai/gpt-oss-20b | 33 | 97.0% |

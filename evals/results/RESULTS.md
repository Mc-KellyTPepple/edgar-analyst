| system | n | tier 1 | tier 2 | tier 3 | tier 4 (text) | answerable (1-3) | refusals | false refusals | citation recall | citation precision | retrieval hit | tool calls | tokens/q | p95 s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| closed_book | 164 | 10.0% | 8.3% | 40.0% | - | 17.3% | 71.4% | 37.3% | 0.0% | - | - | 0.0 | 267 | 1.49 |
| facts_agent | 130 | 100.0% | 100.0% | 50.0% | - | 97.4% | 100.0% | 1.7% | 97.4% | 100.0% | - | 2.06 | 2045 | 53.85 |
| full_agent | 30 | - | - | - | 87.5% | - | 100.0% | - | - | - | 100.0% | 1.27 | 3324 | 181.56 |

| system | model | answerable questions | accuracy |
|---|---|---|---|
| closed_book | openai/gpt-oss-120b | 150 | 17.3% |
| facts_agent | openai/gpt-oss-20b | 116 | 97.4% |
| full_agent | openai/gpt-oss-20b | 16 | 87.5% |

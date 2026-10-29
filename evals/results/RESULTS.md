| system | n | tier 1 | tier 2 | tier 3 | tier 4 (text) | answerable (1-3) | refusals | false refusals | citation recall | citation precision | retrieval hit | tool calls | tokens/q | p95 s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| closed_book | 164 | 10.0% | 8.3% | 40.0% | - | 17.3% | 71.4% | 37.3% | 0.0% | - | - | 0.0 | 267 | 1.49 |
| facts_agent | 187 | 100.0% | 100.0% | 95.0% | 0.0% | 98.7% | 100.0% | 0.7% | 99.3% | 100.0% | 0.0% | 1.87 | 2052 | 58.31 |
| full_agent | 187 | 100.0% | 100.0% | 100.0% | 82.6% | 100.0% | 100.0% | 0.0% | 99.8% | 100.0% | 91.3% | 2.07 | 3031 | 60.34 |

| system | model | answerable questions | accuracy |
|---|---|---|---|
| closed_book | openai/gpt-oss-120b | 150 | 17.3% |
| facts_agent | openai/gpt-oss-120b | 63 | 60.3% |
| facts_agent | openai/gpt-oss-20b | 110 | 100.0% |
| full_agent | openai/gpt-oss-120b | 125 | 99.2% |
| full_agent | openai/gpt-oss-20b | 48 | 93.8% |

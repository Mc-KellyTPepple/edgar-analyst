| system | n | tier 1 | tier 2 | tier 3 | tier 4 (text) | answerable (1-3) | refusals | false refusals | citation recall | citation precision | retrieval hit | tool calls | tokens/q | p95 s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| closed_book | 164 | 10.0% | 8.3% | 40.0% | - | 17.3% | 71.4% | 37.3% | 0.0% | - | - | 0.0 | 267 | 1.49 |
| facts_agent | 187 | 100.0% | 100.0% | 75.0% | 0.0% | 93.3% | 100.0% | 5.3% | 94.7% | 100.0% | 0.0% | 2.2 | 2238 | 58.2 |
| full_agent | 173 | 92.0% | 93.3% | 70.4% | 72.7% | 88.3% | 100.0% | 11.7% | 88.8% | 100.0% | 90.9% | 2.2 | 3051 | 60.51 |

| system | model | answerable questions | accuracy |
|---|---|---|---|
| closed_book | openai/gpt-oss-120b | 150 | 17.3% |
| facts_agent | openai/gpt-oss-120b | 54 | 50.0% |
| facts_agent | openai/gpt-oss-20b | 119 | 95.0% |
| full_agent | openai/gpt-oss-120b | 141 | 86.5% |
| full_agent | openai/gpt-oss-20b | 18 | 83.3% |

| system | n | tier 1 | tier 2 | tier 3 | tier 4 (text) | answerable (1-3) | refusals | false refusals | citation recall | citation precision | retrieval hit | tool calls | tokens/q | p95 s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| closed_book | 164 | 10.0% | 8.3% | 40.0% | - | 17.3% | 71.4% | 37.3% | 0.0% | - | - | 0.0 | 267 | 1.49 |
| facts_agent | 187 | 100.0% | 100.0% | 75.0% | 0.0% | 93.3% | 100.0% | 5.3% | 94.7% | 100.0% | 0.0% | 2.2 | 2238 | 58.2 |
| full_agent | 187 | 92.0% | 93.3% | 52.5% | 69.6% | 82.0% | 100.0% | 18.0% | 86.4% | 100.0% | 91.3% | 2.27 | 3118 | 60.39 |

| system | model | answerable questions | accuracy |
|---|---|---|---|
| closed_book | openai/gpt-oss-120b | 150 | 17.3% |
| facts_agent | openai/gpt-oss-120b | 54 | 50.0% |
| facts_agent | openai/gpt-oss-20b | 119 | 95.0% |
| full_agent | openai/gpt-oss-120b | 155 | 80.0% |
| full_agent | openai/gpt-oss-20b | 18 | 83.3% |

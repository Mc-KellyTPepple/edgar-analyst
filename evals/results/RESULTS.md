| system | n | tier 1 | tier 2 | tier 3 | tier 4 (text) | answerable (1-3) | refusals | false refusals | citation recall | citation precision | retrieval hit | tool calls | tokens/q | p95 s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| closed_book | 164 | 10.0% | 8.3% | 40.0% | - | 17.3% | 71.4% | 37.3% | 0.0% | - | - | 0.0 | 267 | 1.49 |
| facts_agent | 187 | 100.0% | 100.0% | 85.0% | 0.0% | 96.0% | 100.0% | 2.7% | 96.0% | 100.0% | 0.0% | 2.18 | 2227 | 58.2 |
| full_agent | 187 | 100.0% | 100.0% | 95.0% | 82.6% | 98.7% | 100.0% | 0.0% | 99.8% | 100.0% | 91.3% | 1.95 | 2818 | 60.34 |

| system | model | answerable questions | accuracy |
|---|---|---|---|
| closed_book | openai/gpt-oss-120b | 150 | 17.3% |
| facts_agent | openai/gpt-oss-120b | 58 | 53.4% |
| facts_agent | openai/gpt-oss-20b | 115 | 98.3% |
| full_agent | openai/gpt-oss-120b | 123 | 99.2% |
| full_agent | openai/gpt-oss-20b | 48 | 93.8% |

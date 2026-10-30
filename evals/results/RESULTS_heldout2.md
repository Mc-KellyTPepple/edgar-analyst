| system | n | tier 1 | tier 2 | tier 3 | tier 4 (text) | answerable (1-3) | refusals | false refusals | citation recall | citation precision | retrieval hit | tool calls | tokens/q | p95 s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| facts_agent | 89 | 100.0% | 100.0% | 100.0% | - | 100.0% | - | 0.0% | 100.0% | 100.0% | - | 1.45 | 2202 | 58.9 |
| full_agent | 89 | 100.0% | 100.0% | 100.0% | - | 100.0% | - | 0.0% | 98.3% | 100.0% | - | 1.48 | 2511 | 59.51 |

| system | model | answerable questions | accuracy |
|---|---|---|---|
| facts_agent | openai/gpt-oss-120b | 88 | 100.0% |
| facts_agent | openai/gpt-oss-20b | 1 | 100.0% |
| full_agent | openai/gpt-oss-120b | 57 | 100.0% |
| full_agent | openai/gpt-oss-20b | 32 | 100.0% |

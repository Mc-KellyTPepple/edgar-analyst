| system | n | tier 1 | tier 2 | tier 3 | tier 4 (text) | answerable (1-3) | refusals | false refusals | citation recall | citation precision | retrieval hit | tool calls | tokens/q | p95 s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| facts_agent | 66 | 100.0% | 93.1% | 100.0% | - | 97.0% | - | 0.0% | 93.4% | 100.0% | - | 1.97 | 2557 | 59.94 |
| full_agent | 66 | 100.0% | 100.0% | 100.0% | - | 100.0% | - | 0.0% | 97.2% | 100.0% | - | 1.83 | 2752 | 59.44 |

| system | model | answerable questions | accuracy |
|---|---|---|---|
| facts_agent | openai/gpt-oss-120b | 29 | 96.6% |
| facts_agent | openai/gpt-oss-20b | 37 | 97.3% |
| full_agent | openai/gpt-oss-120b | 3 | 100.0% |
| full_agent | openai/gpt-oss-20b | 63 | 100.0% |

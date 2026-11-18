# Cost and latency (from stored evaluation records)

Tokens are what the Groq API reported per question. Seconds include rate-limit waits and fallbacks, so p95 reflects the daily-quota regime, not a production deployment.

| set | system | questions | tokens/q | prompt | completion | tool calls/q | median s | p95 s | answered by 120b | errors |
|---|---|---|---|---|---|---|---|---|---|---|
| tuned set | closed_book | 164 | 267 | 174 | 93 | 0 | 0.5 | 1.51 | 100% | 0 |
| tuned set | facts_agent | 187 | 2135 | 1979 | 156 | 1.93 | 6.15 | 58.85 | 41% | 0 |
| tuned set | full_agent | 187 | 2676 | 2518 | 158 | 1.94 | 4.93 | 59.24 | 84% | 0 |
| held-out 1 | facts_agent_heldout | 66 | 2557 | 2390 | 167 | 1.97 | 3.675 | 60.0 | 44% | 0 |
| held-out 1 | full_agent_heldout | 66 | 2752 | 2595 | 157 | 1.83 | 4.855 | 59.86 | 4% | 0 |
| held-out 2 | facts_agent_heldout2 | 89 | 2202 | 2059 | 143 | 1.45 | 2.25 | 58.93 | 99% | 0 |
| held-out 2 | full_agent_heldout2 | 89 | 2511 | 2377 | 134 | 1.48 | 3.02 | 59.53 | 64% | 0 |

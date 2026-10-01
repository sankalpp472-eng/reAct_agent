Medians over runs with reward 1 (Troute/Twarm: over tool calls).

| Configuration | Model | Workload | Runs used | Turns | Te2e (s) | T_LLM (s) | of which rate-limit wait (s) | Torch / turn (s) | Troute / tool call (ms) | Twarm / tool call (ms) | Rfriction | Rfriction_net |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Conductor + faasd | gpt-oss-120b | all | 10/10 | 2.0 | 49.9 | 45.9 | 33.5 | 1.62 | 22.7 | 0.12 | 0.08 | 0.35 |
| Conductor + faasd | gpt-oss-120b | airline-status | 5/5 | 2.0 | 51.3 | 47.4 | 35.0 | 1.65 | 23.3 | 0.13 | 0.08 | 0.34 |
| Conductor + faasd | gpt-oss-120b | retail-status | 5/5 | 2.0 | 48.7 | 44.2 | 33.0 | 1.59 | 21.7 | 0.09 | 0.09 | 0.35 |
| Argo + Knative, 2 s requeue | gpt-oss-120b | all | 10/10 | 2.0 | 61.9 | 32.4 | 23.0 | 18.48 | 11.6 | 0.12 | 1.05 | 3.88 |
| Argo + Knative, 2 s requeue | gpt-oss-120b | airline-status | 5/5 | 2.0 | 62.8 | 34.5 | 24.0 | 17.93 | 11.4 | 0.12 | 1.09 | 3.33 |
| Argo + Knative, 2 s requeue | gpt-oss-120b | retail-status | 5/5 | 2.0 | 61.1 | 30.3 | 22.0 | 19.17 | 11.6 | 0.11 | 1.02 | 3.91 |
| Argo + Knative, 10 s requeue | gpt-oss-20b | all | 10/10 | 1.5 | 100.2 | 5.8 | 0.0 | 60.92 | 11.8 | 0.13 | 15.12 | 15.12 |
| Argo + Knative, 10 s requeue | gpt-oss-20b | airline-status | 5/5 | 1.0 | 69.3 | 3.9 | 0.0 | 65.56 | 11.7 | 0.13 | 16.27 | 16.27 |
| Argo + Knative, 10 s requeue | gpt-oss-20b | retail-status | 5/5 | 2.0 | 130.6 | 8.7 | 0.0 | 60.73 | 11.8 | 0.11 | 14.04 | 14.04 |

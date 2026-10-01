Medians over runs with reward 1 (Troute/Twarm: over tool calls).

| Configuration | Model | Workload | Runs used | Turns | Te2e (s) | T_LLM (s) | of which rate-limit wait (s) | Torch / turn (s) | Troute / tool call (ms) | Twarm / tool call (ms) | Rfriction | Rfriction_net |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Conductor + faasd | gpt-oss-20b | all | 10/10 | 1.5 | 34.1 | 29.4 | 23.0 | 1.43 | 23.5 | 0.11 | 0.20 | 0.36 |
| Conductor + faasd | gpt-oss-20b | airline-status | 5/5 | 1.0 | 5.9 | 3.9 | 0.0 | 1.72 | 25.0 | 0.11 | 0.52 | 0.52 |
| Conductor + faasd | gpt-oss-20b | retail-status | 5/5 | 2.0 | 69.1 | 66.7 | 58.0 | 1.28 | 22.6 | 0.11 | 0.05 | 0.33 |
| Argo + Knative, 2 s requeue | gpt-oss-20b | all | 9/10 | 1.4 | 29.1 | 4.6 | 0.0 | 20.94 | 11.6 | 0.13 | 5.34 | 5.34 |
| Argo + Knative, 2 s requeue | gpt-oss-20b | airline-status | 5/5 | 1.0 | 26.9 | 3.7 | 0.0 | 23.07 | 11.9 | 0.13 | 5.61 | 5.61 |
| Argo + Knative, 2 s requeue | gpt-oss-20b | retail-status | 4/5 | 2.0 | 56.4 | 19.1 | 9.0 | 17.53 | 11.3 | 0.10 | 1.96 | 4.29 |
| Argo + Knative, 10 s requeue | gpt-oss-20b | all | 10/10 | 1.5 | 100.2 | 5.8 | 0.0 | 60.92 | 11.8 | 0.13 | 15.12 | 15.12 |
| Argo + Knative, 10 s requeue | gpt-oss-20b | airline-status | 5/5 | 1.0 | 69.3 | 3.9 | 0.0 | 65.56 | 11.7 | 0.13 | 16.27 | 16.27 |
| Argo + Knative, 10 s requeue | gpt-oss-20b | retail-status | 5/5 | 2.0 | 130.6 | 8.7 | 0.0 | 60.73 | 11.8 | 0.11 | 14.04 | 14.04 |

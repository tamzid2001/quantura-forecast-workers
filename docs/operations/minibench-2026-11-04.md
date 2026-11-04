# MiniBench delivery audit and independent free routes

Metaculus account-history audit `41931959638` confirms bot 310211 submitted 56 of 60 questions in round 33135 (November 2), with four closed without a forecast. This is 93.33% submission coverage, not 100%. The October 19 round (33132) reports zero submissions. Closed windows cannot be repaired by late forecasts.

`METACULUS_LLM_PROVIDER=direct` selects NVIDIA Build and approved zero-price OpenRouter routes without constructing a Vercel AI Gateway client. Hosting, database and website availability are not in the LLM-to-Metaculus submission path. Endpoint catalog/approval and reported-cost checks remain enabled. Fallback occurs only after rejected requests or configured no-answer transport failures; it does not regenerate an accepted answer to select a preferred forecast. Requested high reasoning effort remains model-specific. No paid fallback is introduced.

Private reasoning is mandatory before a forecast write. Notes are created with `is_private=true`, contain the model's bounded explanation and an answer hash, and survive uncertain requests through encrypted checkpoints. New note metadata records the selected provider/model and free-endpoint policy; missing provider pricing is never invented as a measured zero cost.

The independent coverage audit now checks authenticated private comments separately from forecast receipts. It exports only counts, question IDs, and bounded failure codes from encrypted pending state. It does not export private comment bodies, probabilities, source excerpts, tokens or model responses. Legacy notes without explicit cost-policy metadata are reported as such. Missing forecasts/comments produce visible warnings. The report distinguishes actual account history from successful scheduler execution.

Commands: `python -m pytest -q metaculus_bot/tests`; `python -m metaculus_bot.minibench --checkpoint artifacts/metaculus-checkpoint.enc`. The latter is read-only and needs the bot credential in its environment, not in source code. The manual `metaculus-coverage.yml` workflow obtains the production checkpoint and publishes the redacted report.

The future round date is an estimate unless Metaculus publishes it. Scheduler uptime cannot guarantee all 60 accepted forecasts: API permission, model quota, valid output, private-comment capacity and closing times remain real constraints. Sixty daily private-note slots remain reserved for MiniBench ahead of other tournaments.

A follow-up audit (`41932861757`) confirms all 56 submitted questions have authenticated private reasoning comments. The four missed question IDs are 46602 (unverified source identifiers), 46611 (invalid distribution), 46610 and 46636 (saved model abstentions). Legacy comments do not contain the new structured free-provider attestation field; their absence is not represented as a verified billing amount.

The empty-evidence source schema previously advertised the literal `none`, while validation rejected it. The schema now requires an empty list when there are no sources, and the old no-citation sentinel is normalized only for an empty evidence set. Arbitrary source IDs still fail. Numeric instructions now state the adapter's actual strict value-order requirement. These changes preserve accepted probabilities and never regenerate a closed-round answer. Full local regression: 264 tests passed.

# Embedding runtime

`pipeline.py` executes inference batches; `contracts.py` defines inputs/progress/cancellation.
`artifacts.py` owns chunk manifests and output payloads, `checkpoints.py` records
resume state, and `readers.py` loads stored embeddings. `process_runner.py`
provides isolated execution.

Inference batch size is independent of checkpoint size. The run dialog offers
**Auto, High, Low, 4, 8, 16, 32, 64**. Auto is the default. Numeric choices remain
fixed requests subject to model and resource limits; older saved numeric values
remain readable and editable without silent conversion.

`batching.py` owns three adaptive policies:

| Policy | Candidates | Prefer smaller within best throughput | Batch-time ceiling | Worker RAM budget |
| --- | --- | --- | --- | --- |
| Auto | 8, 16, 32 | 2% | 2 seconds | 70% of initially available RAM |
| High | 16, 32, 64, 128 | 2% | 3 seconds | 80% of initially available RAM |
| Low | 4, 8, 16 | 10% | 1 second | 50% of initially available RAM |

Candidates are capped by the model and prepared-input budget. Each gets two
warmup batches, then at least eight timed preparation/inference batches, targeting
two seconds (at most 64 batches). Reserve enough real inputs for all candidates;
short runs use a cached or policy starting size. Intermediate plateaus never skip
a larger permitted candidate. Successful inputs are never rerun for tuning.
Allocation errors halve the failed batch and retry the same inputs in adaptive
modes, retaining a hard ceiling for that run. Fixed requests retain existing
error/fallback behavior. IDs and final partial batches remain aligned.

`batch_profiles.py` keys preferences by policy, model, preparation, device, system
and runtime. Only complete comparisons or successfully exercised allocation
ceilings update them. Temporary throttling never overwrites a preference. Older
policy revisions are ignored. CoreML uses real fixed shapes up to 128, padding
only the final tensor and discarding padded outputs. Shape changes release the
old session before compilation; shape caches are bounded to 32 entries / 512 MiB.

`resources.py` samples RAM, RSS, disk and supported thermal signals once per
second. No clock, voltage, fan or power settings change. All modes pause for
serious/critical heat, available RAM below max(512 MiB, 5% total), GPU memory below
256 MiB, low disk space, or exceeding the worker budget. The parent uses the same
policy and can stop an isolated native worker after three seconds without a safe
checkpoint. Fixed requests use Auto's resource policy.

For mild thermal warnings, Auto holds the current size, High permits continued
comparison, and Low halves once. Available-RAM soft reserves are 10%/7.5%/20%
for Auto/High/Low (at least 1 GiB); worker soft limits are 85%/90%/70% of the
respective budgets. Low reserves 1 GiB of GPU memory and other policies 512 MiB.
Soft memory pressure halves once per continuous pressure episode and blocks
recovery until it clears. There is no fixed per-batch sleep. Missing thermal
monitoring still caps sizes at 16; missing CUDA memory monitoring caps at eight.

`BatchThrottle` separates temporary limits from preferred sizes. Recovery doubles
at most once per ten seconds of stable permitted headroom, avoiding rapidly
alternating CoreML shapes. Interrupted comparisons retain completed candidates,
discard partial timing windows, restart warmups, and rebudget remaining inputs.
The run report distinguishes the policy, preferred and effective sizes, and stores
the last 32 resource transitions with readings on completion. Live status explains
limits and measured choices. Telemetry is not universally available; these checks
do not guarantee against hardware/cooling faults. Existing admission serializes
embedding jobs and system-wide readings account for competing work.

Completed batches are checkpointed at a 4,096-row target or after ten seconds,
whichever comes first at a batch boundary. A 64 MiB buffer cap bounds wide-vector
memory use. Disk headroom is checked before chunks and final consolidation. A pause flushes
the completed buffer before returning. Cancellation never publishes that buffer.
Live embedding telemetry is limited to four updates per second before process
delivery, with immediate final counts and stage transitions. Durable checkpoint
callbacks remain separate from live progress. Native phase messages remain intact
for crash diagnosis; persisted pause/cancel state is polled at bounded intervals.
The ONNX adapter exposes its shape/input-memory limit, so telemetry and pause checks follow
actual inference batches rather than larger requests split inside the adapter.

Keep IDs, vectors, and identifiers aligned. Recovery counts only complete,
committed triples and checks their identities. Summaries must not load full vector
payloads. Preserve input/model fingerprints when resuming a run.

Add backends through [adapters](adapters/README.md). Keep runtime work Qt-free and
report completion only after artifacts are finalized.

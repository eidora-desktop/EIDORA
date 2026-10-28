# Technical ONNX package requirements — schema 3

## Authority and workflow

Desktop's `EmbeddingsArea/model_package.py`, `preparation_contract.py`, runtime
preparers and ONNX adapter own executable semantics. Public documentation and
`onnx-contract.json` live in Desktop's `automation/onnx_contract/`. The pipeline
keeps a checksum-identical snapshot and documentation; its generator must not
invent a competing runtime manifest. Synchronize explicitly, then check for drift:

```
python automation/onnx_contract.py --pipeline-root ../ONNX_pipelines --write
python automation/onnx_contract.py --pipeline-root ../ONNX_pipelines
```

The model-zoo authoring spec retains source identity, `export`, `inputs`,
`preprocess`, `embedding`, `assets` and `validation`. Its `eidora` section declares
`execution_providers` and exact Desktop `preparation` (per component for multimodal
models). The generator assembles schema-v3 `config.yaml` and preflights it with
the selected Desktop parser before export. Build all graph,
external-weight, tokenizer and real-media fixture assets; stage before replacing
an existing package. Require `citation.bibtex` in the authoring spec and copy it
into top-level `config.yaml` `bibtex` and the generated model card. Validate the
citation with the Desktop parser before export; model-card-only citations do not
satisfy the contract. Use complete self-contained records as described in
ONNX_CONFIG_SCHEMA.md. Store additional attribution and training information under
metadata.provenance and describe it in README.md. Desktop freezes config BibTeX
in embedding artifacts so reports do not depend on installed model cards.

## Runtime contract

Follow ONNX_CONFIG_SCHEMA.md and ONNX_PREPARATION_OPTIONS.md exactly. Export
portable CPU-compatible ONNX operations; Desktop installs no custom operator
libraries, Python model code or per-model dependencies. Choose IR/opset versions
supported by the actual bundled ONNX Runtime. Internal quantization is permitted,
but inputs are float32/int32/int64 and the final output is float32 [B,D]. Dynamic
batch or batch 1 only. Output must be finite; normalized declarations are checked
with rtol=1e-4, atol=1e-5. Pooling and projection belong inside the graph.

Decode and prepare before inference with the selected Desktop interpreter. Match
resizing, rounding, interpolation, alpha background, normalization, tokenization,
resampling and excerpts against the source model. Defaults must reproduce the
intended reference pipeline. Alternative exposed options are deliberate experiments.
For image/video pipelines that truncate aspect-preserving resize dimensions,
declare `preparation.resize_rounding: truncate` (per component for multimodal
packages). Omission defaults to `round`; do not change existing packages unless
their reference preprocessing requires it. Crop offsets and interpolation remain
independent, so revalidate the complete input tensors and vectors. Use matching
Pillow versions when comparing preprocessing.
Do not normalize twice: new AlexNet exports expect externally normalized tensors;
imported graphs that normalize internally use identity external normalization.

## Provider validation

CPU is mandatory. Explicit provider certification must use that provider's session
chain without full-session fallback. CPU support for individual unsupported nodes
is allowed; do not claim every operation ran on GPU. Missing hardware is unverified.
Certify each declared provider using real-media fixtures, valid output checks,
batch 1 and a multi-item batch where supported. Each modality requires at least one reference fixture covering every graph input
and the final vector; compare these source-model tensors and vectors. Reports identify config hash, contract hash, Desktop version, runtime
version and host platform. Reports from different hosts may be combined only for
the same package and contract. Metadata-only checks cannot certify publication.

Desktop failure recovery is separate from certification: terminate/join the failed
accelerator worker, discard every partial vector from that attempt and restart
once on CPU. Preserve both errors if CPU fails. Do not retry invalid manifests,
preparation/decoding failures, storage errors or cancellation. CPU retries survive
pause/resume without returning to the failed accelerator. Persist effective
preparation and execution history with the resulting embeddings.

## Regression checks

Validate unknown/missing fields, provider lists, parameter subsets/bounds, tensor
shape/dtype, hashes, safe paths and required tokenizer files. Test real image/text/
audio/video inputs, padding and truncation, EXIF/alpha/crops, short-edge sizing,
channel resampling, sample-accurate audio boundaries and video EOF repetition.

Keep one item → one vector. Dynamic spatial sizes, custom processors, multi-crop,
multi-clip and document chunking/aggregation are not part of this contract.

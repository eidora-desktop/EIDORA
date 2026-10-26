# EIDORA ONNX package schema 3

Desktop owns this contract. The pipeline must use the matching versioned
`onnx-contract.json` snapshot and validate with the selected Desktop interpreter.
Schema 1 and 2 packages require repackaging; completed vectors remain readable.

## Package manifest

`config.yaml` is a UTF-8 mapping. Unknown executable fields are rejected.
Required root keys: `schema_version: 3`, nonempty `id`, `name`, `version`,
`modality`, `license`, `bibtex`, `compatibility`, `artifacts`, `runtime`, `embedding`, and
`preparation` for single encoders. `metadata` is the only open descriptive mapping.

- `bibtex`: required nonempty string containing one or more complete BibTeX
  records with unique keys and a nonempty `title` per record. Use self-contained
  braced, quoted, or numeric field values; expand string macros/concatenation
  before packaging. Additional fields (authors, year, venue, DOI, URL) should
  describe the actual model reference. Desktop preserves each original record
  for export and reads its fields for captions. A README or metadata citation
  does not replace this field. Existing configs without it require repackaging;
  completed embedding datasets remain readable.
- `modality`: image, text, audio, video, or multimodal.
- `compatibility.min_eidora_version`: minimum Desktop version, as a dotted string.
- `artifacts`: list of `{path, sha256, role}`. Paths are package-relative and
  cannot escape the package. Include every graph, external weight and tokenizer
  file. A root README.md is required.
- `runtime.execution_providers`: unique list of canonical provider names;
  CPUExecutionProvider is mandatory. CUDAExecutionProvider and
  CoreMLExecutionProvider are the only optional providers currently recognized.
- Single-encoder runtime: `graph`, `inputs`, `output`, `execution_providers`.
- Input: `{name, source, dtype, shape}`. Names must exactly match graph inputs;
  dtype is float32, int32, or int64. Batch is `batch`, `dynamic`, or integer 1.
- Output: `{name, dtype: float32, shape: [batch, D]}`. One finite vector per item.
- Embedding: positive `dimensions`, boolean `normalized`, `similarity` in cosine,
  dot, euclidean; optional `shared_space: {id, revision}` with both strings.
- Internal quantization is allowed; boundary tensor types remain constrained.

Input sources: image `pixels`; text `token_ids`, `attention_mask`,
`token_type_ids`; audio `waveform`, `valid_samples`, `sample_mask`; video `frames`.

## Fixed preparation fields

| Modality | Fields |
| --- | --- |
| Image | width, height, layout (NCHW/NHWC), resize, interpolation, rescale, mean[3], std[3], optional alpha_background[3], crop_position, short_edge, resize_rounding |
| Text | tokenizer_path, max_length, padding_side, truncation_side, add_special_tokens; optional prefix/suffix |
| Audio | sample_rate, sample_count, channels; optional excerpt/start_seconds |
| Video | width, height, frame_count, duration_seconds, layout (BTCHW/BCTHW/BTHWC), resize, interpolation, rescale, mean[3], std[3]; optional alpha_background, crop_position, short_edge, resize_rounding, excerpt/start_seconds |

`preparation.resize_rounding` is optional and accepts `round` or `truncate`.
If omitted, EIDORA uses `round` (Python rounding, with ties to even), preserving
existing resize behavior. Set `resize_rounding: truncate` to discard fractional
pixels when calculating aspect-preserving resize dimensions. This applies to
`fill_crop`, `fit_pad`, and `short_edge_crop`, including image/video components of
multimodal packages. It does not affect `stretch`, crop offsets, interpolation,
or normalization. For example, a 640×480 image with `short_edge: 224` resizes to
299×224 with `round`, or 298×224 with `truncate`, before cropping.
Unknown values are rejected. This option requires preparation implementation 2;
the package schema remains version 3.
Existing unfinished ONNX runs must be re-prepared because saved preparation
signatures include the implementation version. Completed embedding datasets
remain readable and unchanged.

Dimensions/counts must be positive integers. Numeric values must be finite;
standard deviations must be nonzero. Audio waveforms are [B,S] or [B,1,S],
valid counts [B], masks [B,S]. Text tensors are [B,max_length]. Image/video
shapes must match fixed dimensions and layout. One prepared item must fit the
256 MiB input budget; batching is split as needed. Decoding/model memory is extra.

## Editable parameters

`preparation.parameters` maps IDs from [the global catalog](ONNX_PREPARATION_OPTIONS.md)
to `{default, choices?, minimum?, maximum?}`. The default is required. Choices
and bounds may narrow the global catalog, never extend it. Types, labels,
conditions and operation order belong to Desktop; packages cannot supply hooks,
UI expressions or custom operations. Numeric controls can also use finite choices.

An exposed parameter's default overrides its same-named fixed preparation field.
A saved run selection overrides that default. Unexposed fields retain their fixed
values, including documented optional defaults. Unknown run overrides fail.
The resolved recipe is saved with its implementation version and run signature.

```yaml
schema_version: 3
id: example/image-encoder
name: Example Image Encoder
version: "1.0.0"
modality: image
license: MIT
bibtex: |
  @misc{example_model,
    title={Example Image Encoder},
    author={Example, Ada},
    year={2026}
  }
compatibility: {min_eidora_version: "0.1.0"}
artifacts:
  - {path: model.onnx, sha256: "<64 hexadecimal characters>", role: graph}
runtime:
  execution_providers: [CPUExecutionProvider]
  graph: model.onnx
  inputs:
    - {name: pixel_values, source: pixels, dtype: float32, shape: [batch, 3, 224, 224]}
  output: {name: embedding, dtype: float32, shape: [batch, 512]}
preparation:
  width: 224
  height: 224
  layout: NCHW
  resize: short_edge_crop
  resize_rounding: round  # Optional; use truncate when required by the reference preprocessing.
  short_edge: 256
  crop_position: center
  interpolation: bicubic
  rescale: 0.00392156862745098
  mean: [0.485, 0.456, 0.406]
  std: [0.229, 0.224, 0.225]
  alpha_background: [0, 0, 0]
  parameters:
    resize: {default: short_edge_crop, choices: [short_edge_crop, fill_crop, fit_pad, stretch]}
    short_edge: {default: 256, minimum: 224, maximum: 512}
    interpolation: {default: bicubic, choices: [nearest, bilinear, bicubic, lanczos]}
embedding: {dimensions: 512, normalized: true, similarity: cosine}
```

## Multimodal packages

Use `modality: multimodal`, omit root preparation, and set runtime to
`{execution_providers, fusion: normalized_mean, components}`. Components are
keyed by modality and contain `{runtime: {graph, inputs, output}, preparation}`.
The package provider list applies to every component. Encoders share the root
embedding declaration, which requires normalized output and a shared-space ID
and revision. Select one or more modalities; pair by permanent item ID, keep the
intersection, normalize each vector, sum equally and normalize the result.
Parameters are stored in runs as `modality.parameter` to avoid collisions.

## Execution and certification

Desktop offers declared, installed providers allowed on the current platform.
CoreML requires native Apple Silicon execution on macOS. Intel Macs (and x86
processes under Rosetta) use CPU only, including saved accelerator requests.
Auto prefers CUDA, then CoreML, then CPU among eligible providers. Accelerated
sessions may use CPU for individual unsupported graph nodes; configured providers are not proof of GPU utilization.
A GPU setup/inference error, invalid output or native inference crash causes one
full restart on CPU. Input/preparation/storage errors and cancellation do not.

Saved provenance records the requested device, configured session providers,
attempts/errors, fallback reason, preparation version and final execution mode.
CPU retry discards all vectors from the GPU attempt. CPU failure reports both
errors. A saved but unavailable accelerator starts on CPU with an explanation.

Publication requires real-media validation through Desktop. Certification reports
identify the exact config, contract, Desktop/runtime environment and providers.
Missing hardware is unverified, not success. CPU certification is mandatory;
optional provider claims require corresponding certification. Metadata-only
validation does not certify a package for publication.

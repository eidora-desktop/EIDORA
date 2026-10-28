# ONNX embedding models for EIDORA

Ship a schema-v3 `config.yaml`, README.md, graph(s), external weights, and packaged
tokenizers. Declare hashes for every runtime asset. Old packages require explicit
repackaging. Saved embedding datasets remain readable.

Every config must include a nonempty top-level `bibtex` string containing the
model reference(s), including titles and unique citation keys. The pipeline
requires `citation.bibtex` in its authoring spec and copies it into the config
and generated model card. References only in README.md are insufficient.
Desktop saves config BibTeX with each embedding run and uses it for Methods and
Citations, snapshot reports, figure captions, and direct BibTeX export.

EIDORA handles media decoding and preparation. The package defines fixed input
requirements, recommended defaults and optional user controls from the
[global catalog](ONNX_PREPARATION_OPTIONS.md). Resizing, interpolation, text
truncation/prefixes and temporal excerpts run in EIDORA's background worker.
Pooling, model-specific feature extraction and output normalization run in ONNX.
Return one finite float32 [batch, dimensions] vector per item.

Image and video preparation may declare `resize_rounding: truncate` when the
reference model discards fractional resize dimensions. Omitting this field uses
`round`, preserving existing behavior. See the schema for the exact scope and
defaults, and verify the complete prepared tensors against the reference model.

Every package must explicitly declare CPUExecutionProvider. Advertise CUDA or
CoreML only after validation with the selected Desktop runtime on suitable
hardware. EIDORA offers declared, installed providers allowed on the platform.
CoreML is limited to native Apple Silicon on macOS; Intel Macs use CPU only.
Accelerator failure starts a fresh CPU attempt from item zero, with visible
reporting and saved provenance.

Multimodal packages contain separate encoders with an explicit normalized_mean
fusion rule and one declared shared embedding space. EIDORA pairs selected
modalities by permanent item ID and creates one vector per eligible item.

Read the [schema](ONNX_CONFIG_SCHEMA.md), [technical requirements](ONNX_MODEL_REQUIREMENTS_AGENTS.md)
and [option catalog](ONNX_PREPARATION_OPTIONS.md) together. Source-model tensor and
vector comparisons are required to establish numerical fidelity; graph loading
or successful output shape checks alone cannot establish it.

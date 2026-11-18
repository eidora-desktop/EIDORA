# Projection methods

`base.py` defines the algorithm contract. `methods/<id>/config.yaml` declares
method metadata, parameters and execution mode; its `method.py` implements the
algorithm. `DiscoveryArea/method_catalog.py` discovers these packages.

Return coordinate rows in input order and validate dimensionality. Keep numerical
dependencies lazy. Declare transform support only when implemented. Long-running
methods use `process_runner.py` so cancellation can terminate the child process.

When adding a method, update its config, focused method tests, and packaging
inventory together. Run the public source gate to validate dynamic method entries.
The individual algorithm folders follow this same contract.

New Projection lists **UMAP 0.6** (`umap`) first and **UMAP 0.5** (`umap_05`)
second. Both use the installed umap-learn 0.6 library; the latter explicitly
selects its 0.5 compatibility pipeline, not a separate 0.5 installation.
The YAML schemas contain all 52 constructor parameters for 0.6 and all 39 for
0.5.12. Defaults match those library versions, except that the 0.6 entry fixes
`compatibility_layout=False` to select the new pipeline. `init=auto` and
`optimizer=None` then resolve to recursive initialization and Adam. The 0.5
entry defaults to spectral initialization and the compatibility optimizer.

Both default to Euclidean distance, `random_state=None`, and `n_epochs=None`.
A numeric random seed enables repeatability within the same inputs, settings,
platform and dependency versions. Seeded compatibility runs use one worker;
new-pipeline runs remain parallel. `n_jobs=-1` uses EIDORA's existing bounded
projection CPU budget; larger explicit requests are also capped to that budget.
Saved coordinates are never recalculated. Earlier `epochs` and `reproducible`
settings are translated at the parameter boundary; an old compatibility-mode
request selects `umap_05`. Provenance records the library version separately
from the method name, plus effective initialization, optimizer and worker count.

Advanced parameters expands the complete parameter table for the selected
method. Parameters that
require supervised labels, fitted-model transforms, precomputed neighbor arrays,
density-radius output or non-2D output stay visible but fixed at their defaults,
with explanatory tooltips. Named metrics and named initializers are supported;
Python callables and custom initialization arrays are not accepted. Dictionary
options use JSON; nullable controls accept `None`, `null`, or an empty field.
Epoch counts may be an integer or a JSON list; Discovery saves final coordinates.

Parameter schemas may use `label` for display text. All visible parameters share
the Advanced parameters section. Collapsing preserves their values; submission
always uses each schema's stable parameter key.
`readonly` fixes unsupported options to their declared defaults; `nullable`
preserves automatic library values. JSON schemas may restrict inputs to objects
or epoch counts. Catalog discovery must remain free from UMAP/Numba imports.

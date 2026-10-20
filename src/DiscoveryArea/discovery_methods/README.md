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

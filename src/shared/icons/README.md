# EIDORA Icons

Icons are referenced by semantic names from the UI files, for example `metadata` or `filter`.
The current starter set is vendored from Material Symbols through the local `qt_material_icons` package.

UI files point directly at SVG files in `material/`.
To replace an icon later, keep the same semantic filename in `material/`.
Source SVGs should use a black fill (`#000000`); the Qt loader tints icons from the active application palette at runtime.

Material Symbols are provided by Google under the Apache License 2.0.

# Canvas QML

The native form composes the renderer and overlays. The other form supports the
fallback backend. Python owns interaction/workflow decisions; QML owns composition.

Keep polygon fills below points, with outlines and edit handles above them.
Do not create a QML item per data point. Preserve explicit layer roles and native
host exposure behavior when changing either form.

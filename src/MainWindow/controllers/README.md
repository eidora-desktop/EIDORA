# Shell controllers

Read `main_controller.py` for area composition and cross-area notifications.
The startup project list lives in `project_chooser_controller.py`; release lookup and About
live in `help_panel_controller.py`.
`loading_panel_controller.py` runs project opening off-thread and advances hidden
workspace setup on the GUI thread between animation frames.

Route project writes through `ProjectService`. Preserve shutdown ordering and
Discovery activation when changing navigation. See the [area guide](../README.md).

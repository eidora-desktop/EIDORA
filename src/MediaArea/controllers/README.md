# Media workspace controllers

The main controller composes Media Sources and Preview Assets side by side and
publishes narrow change callbacks to the main window. The thumbnail controller owns its saved
sets, creation dialog, progress, cancellation, and unfinished-job recovery.
The preview row controller binds a Designer-owned card with header, inline
details table and progress controls. Keep those widgets stable through progress
and completion; retire the card on refresh or project changes. Native tree expansion remains
available from the row toggle and keyboard.
Source rows launch creation with a fixed input; availability callbacks update
their actions after link refreshes and preview job transitions. Text excerpts,
audio/video clips, and GIFs use the same job lifecycle.
The text inspector loads one saved excerpt at a time in a background worker;
plain-text rendering preserves literal markup and does not require the source.
The clip inspector owns one active player or GIF movie and stops it on close.
`preview_parameters_panel_controller.py` binds the basic/advanced tables,
conditional rows, reset action, and effective-settings summary. Validation and
encoding stay in the preview option/generation modules.
Link opens one exact-key preview dialog. Its replacement checkbox starts
unchecked on each visit and controls whether unmatched existing source links
are removed. The preview reports added, kept, and removed asset/Item pairs.
Changing the checkbox invalidates pending previews and suggestions. Unsupported
saved rules are never silently translated into new choices.

Keep scanning, resizing, decoding, persistence, and worker orchestration outside
the area-level controller. Stop active jobs before replacing a project and
reject callbacks from an earlier project generation.

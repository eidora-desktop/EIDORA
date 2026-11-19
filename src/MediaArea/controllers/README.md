# Media workspace controllers

The main controller composes Media Sources and Preview Assets side by side and
publishes narrow change callbacks to the main window. The thumbnail controller owns its saved
sets, creation dialog, progress, cancellation, and unfinished-job recovery.
The preview row controller binds a Designer-owned card with header, inline
details table and progress controls. Keep those widgets stable through progress
and completion; retire the card on refresh or project changes. Native tree expansion remains
available from the row toggle and keyboard.
Source rows launch creation with a fixed input; availability callbacks update
their actions after link refreshes and preview job transitions. Text and Audio
rows open informational placeholders without entering the processing pipeline.
Link opens one exact-key preview dialog. Its replacement checkbox starts
unchecked on each visit and controls whether unmatched existing source links
are removed. The preview reports added, kept, and removed asset/Item pairs.
Changing the checkbox invalidates pending previews and suggestions. Unsupported
saved rules are never silently translated into new choices.

Keep scanning, resizing, decoding, persistence, and worker orchestration outside
the area-level controller. Stop active jobs before replacing a project and
reject callbacks from an earlier project generation.

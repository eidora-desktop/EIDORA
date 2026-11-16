# Assets workspace controllers

The main controller composes Media Sources and Thumbnails side by side and
publishes narrow change callbacks to the main window. Thumbnails owns its saved
sets, creation dialog, progress, cancellation, and unfinished-job recovery.
Link opens one exact-key preview dialog. Its replacement checkbox starts
unchecked on each visit and controls whether unmatched existing source links
are removed. The preview reports added, kept, and removed asset/Item pairs.
Changing the checkbox invalidates pending previews and suggestions. Unsupported
saved rules are never silently translated into new choices.

Keep scanning, resizing, decoding, persistence, and worker orchestration outside
the area-level controller. Stop active jobs before replacing a project and
reject callbacks from an earlier project generation.

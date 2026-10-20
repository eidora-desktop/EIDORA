# Application information

`service.py` reads the platform recommendation from
`https://eidora.org/updates.json`. `domain.py` contains the release value object.
`release_client.py` performs asynchronous Qt network lookup only after an explicit
update action; it does not install or execute downloads. About/help UI lives in
MainWindow, with update wiring in `controllers/update_panel_controller.py`.

Keep release parsing independent of Qt. Preserve URL/platform validation, request
timeouts, and cancellation when changing lookup behavior. The application version
comes from `eidora_version.py`.

The website owns publication and validates available artifacts. The app compares
numeric versions, accepts explicitly recommended prototype/preview releases,
and opens fixed eidora.org download and release-note URLs. It does not open
arbitrary URLs supplied by the response. A null recommendation means no public
release is available, not that the installed version is current.

See [the website update contract](../../packaging/UPDATES.md).

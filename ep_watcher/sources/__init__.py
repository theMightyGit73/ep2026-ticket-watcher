"""Ways of answering "is there a ticket". See each module for its trade-offs.

Nothing is imported eagerly here on purpose. `browser` pulls in Playwright,
which is a heavy dependency and deliberately absent wherever the watcher runs
in API-only mode (EP_USE_BROWSER=0) — GitHub Actions, or any host without a
display. Importing it here would make that mode impossible: the process would
die on ModuleNotFoundError before reaching the sources it *can* use.

Import the module you actually need, at the point you need it.
"""

# webapp/downloads/

Drop the packed Claude Desktop extension here as **`ts-debug-helper.mcpb`**.
The API Tokens tab then shows a *Download* button for it (version, size and
SHA-256 are read from the file itself). No restart needed: replace the file
and users get the new build on their next download.

On the VM:

    sudo install -m 644 -o ubuntu -g ubuntu ts-debug-helper.mcpb \
        /opt/ts-debug-helper/webapp/downloads/ts-debug-helper.mcpb

To serve it from somewhere else, set `TS_MCPB_PATH=/full/path/ts-debug-helper.mcpb`
in `/etc/ts-debug-helper.env` and restart the service (only that path is then used).

Build it from `desktop-extension/` with `npx @anthropic-ai/mcpb pack`.
`*.mcpb` in this folder is git-ignored.

# neocursor.nvim

Cursor's **Tab** — inline completions *and* diff-style rewrites — inside Neovim,
streamed from Cursor's own backend.

> Personal-use interop project. It talks to Cursor's `StreamCpp` service using
> your existing Cursor login, so a signed-in Cursor install is required.

## How it works

```
  NEOVIM (Lua)                 sidecar.py                 Cursor backend
  ┌───────────────┐  stdio     ┌──────────────┐  HTTPS    ┌──────────────┐
  │ autocmds +    │  (JSON)    │ reads token, │  (Connect │ api2.cursor  │
  │ ghost/diff    │ ─────────▶ │ forges the   │  protobuf │ .sh          │
  │ render + <Tab>│ ◀───────── │ checksum,    │  over h2) │ StreamCpp    │
  │ accept        │            │ streams edits│ ◀───────▶ │ (Copilot++)  │
  └───────────────┘            └──────────────┘           └──────────────┘
```

The plugin sends the current buffer + cursor; the backend streams back an edit
(`range_to_replace` + `text`). Two render modes:

- **append** at the cursor → inline ghost text (supermaven/copilot technique)
- **override** of existing lines → red→green diff overlay (via `vim.diff`)

`<Tab>` accepts; the edit is a range-replace either way.

## Install (lazy.nvim, local dev)

```lua
{
  dir = "/path/to/neocursor.nvim",
  name = "neocursor.nvim",
  event = "InsertEnter",
  config = function()
    require("neocursor").setup({ map_tab = false }) -- let your completion engine own <Tab>
  end,
}
```

If another plugin owns `<Tab>` (nvim-cmp, blink.cmp…), have its Tab handler call
`require("neocursor").accept()` first and fall through on `false`.

## Try it

```
nvim -u test/init.lua test/demo.py
```

- **Append:** cursor at a line end in section 1, insert, pause → ghost → `<Tab>`.
- **Rewrite:** cursor on `return a - b` in section 2, insert, nudge → red/green diff → `<Tab>`.

Commands: `:NeocursorSuggest` (force a request) · `:NeocursorDebug` (diagnostics).

## Status

- [x] Reverse-engineered `StreamCpp` (auth token + `x-cursor-checksum`)
- [x] Inline ghost text + `<Tab>` accept
- [x] Diff overlay for rewrites
- [ ] `diff_history` (recent edits) for sharper, deterministic predictions
- [ ] NES jump (`cursor_prediction_target`) — the cross-file "tab-tab-tab"
- [ ] Char-level diff for single-character changes

See `NOTICE` for vendored rendering-technique attribution.

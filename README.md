# neocursor.nvim

**Cursor's Tab — the real one — inside Neovim.**

Not a Cursor-*like* completion model. Not another LLM wired to a prompt.
`neocursor.nvim` speaks Cursor's own `StreamCpp` service, using your existing
Cursor login, so the predictions you get in Neovim are the *same predictions, at
the same latency, with the same quality* you'd get inside Cursor itself.

> **The only maintained Neovim plugin that runs Cursor's actual Tab backend.**
> Every other option either autocompletes on a different model, or bolts
> Cursor-style UX onto a *substitute* engine. This one is the engine.

<!-- badges: fill in once the repo is public -->
<!-- ![Neovim](https://img.shields.io/badge/Neovim-0.10+-57A143?logo=neovim&logoColor=white) -->
<!-- ![Lua](https://img.shields.io/badge/Lua-000080?logo=lua&logoColor=white) -->
<!-- ![Status](https://img.shields.io/badge/status-alpha-orange) -->

```
        you type…                    …Cursor's model predicts your next edit
  ┌────────────────────┐            ┌────────────────────────────────────────┐
  │ def quicksort(arr): │           │ def quicksort(arr):                     │
  │     if len(arr)█     │  ── Tab → │     if len(arr) <= 1:                   │
  │                      │           │         return arr                      │
  │                      │           │     pivot = arr[len(arr) // 2]      ░░░ │
  └────────────────────┘            └────────────────────────────────────────┘
                                       ghost text · Tab accepts · Tab again jumps
```

---

## Why this is different

Every other AI plugin for Neovim gives you **a** model. This gives you
**Cursor's** model.

| | Most plugins | `neocursor.nvim` |
|---|---|---|
| **Model** | A generic LLM (Copilot, Codeium, Supermaven, or BYO) | Cursor's *actual* Tab model (`StreamCpp` / "Copilot++") |
| **Prediction** | Autocomplete at the cursor | Cursor's **next-edit** prediction + cursor **jumps** |
| **Context** | Current file, maybe a few lines | Full Cursor parity: nearby files, diff history, linter errors |
| **Quality/latency** | Depends on the model you picked | *Identical to Cursor* — because it **is** Cursor |
| **Auth** | A separate API key / subscription | Your existing Cursor login |

If you already pay for Cursor and live in Neovim, this is Cursor's headline
feature — **Tab, tab, tab** — without switching editors.

---

## Features

- **Inline ghost text.** Multi-line completions rendered as you type
  (the supermaven/copilot rendering technique — see [`NOTICE`](./NOTICE)).
- **Diff-style rewrites.** When Cursor wants to *change* existing lines, you get
  a red→green diff overlay, not just an append. `<Tab>` applies it.
- **Next-Edit-Suggestion jumps (the "tab-tab-tab").** Cursor doesn't just
  complete where you are — it predicts *where you'll edit next* and moves you
  there. `neocursor.nvim` renders the `Tab →` hint at the target and walks the
  chain, one `<Tab>` per edit, with no extra round-trips.
- **Full native context parity.** Every request carries what Cursor's own client
  sends: `additionalFiles` (nearby/open buffers), `fileDiffHistories` (your
  recent edits), and `linterErrors`. Same inputs → same predictions.
- **Faithful request gating.** Ports Cursor's client-side suppression: it only
  fires on real edits (line-change gate) and stays quiet while you're just
  reading/navigating (idle gate). No request churn, no wasted quota.
- **Newest-wins concurrency.** In-flight requests are aborted the instant you
  type again (Cursor's `cancelCpp`), with a late-reply rescue so a paused burst
  still resolves instead of flickering to nothing.
- **Live state dashboard.** `:NeocursorLog` opens a pane showing the state
  machine in real time — the current phase, what `<Tab>` would do right now, the
  debounce timer, the prediction ledger.
- **Plays nice with your completion stack.** Hand `<Tab>` to `nvim-cmp` /
  `blink.cmp` and have their handler fall through to
  `require("neocursor").accept()`.

---

## How it works

```
  NEOVIM (Lua)                  sidecar.py                  Cursor backend
  ┌────────────────┐  stdio     ┌───────────────┐  HTTPS    ┌───────────────┐
  │ autocmds +     │  (JSON)    │ reads your    │  (Connect │ api2.cursor   │
  │ ghost/diff     │ ─────────▶ │ Cursor token, │  protobuf │ .sh           │
  │ render + <Tab> │ ◀───────── │ forges the    │  over h2) │ StreamCpp     │
  │ accept + jump  │            │ checksum,     │ ◀───────▶ │ (Copilot++)   │
  └────────────────┘            │ streams edits │           └───────────────┘
                                └───────────────┘
```

1. **Neovim (Lua)** watches your buffer and cursor, gates requests the way
   Cursor does, and renders whatever comes back — ghost text, diff overlay, or a
   jump hint. `<Tab>` accepts / jumps / chains.
2. **`sidecar.py`** is a tiny persistent stdio bridge. It reads your
   `cursorAuth/accessToken` from Cursor's local state DB, forges the
   `x-cursor-checksum` header, frames the Connect protobuf, and streams
   `StreamCpp` over HTTP/2 — the exact call Cursor's own client makes.
3. **Cursor's backend** streams back a sequence of edits (each bracketed by
   `beginEdit`/`doneEdit`) plus a `cursorPredictionTarget` — the next place to
   jump. The sidecar splits them into an ordered list so the client can walk the
   whole chain locally.

The plugin fetches `CppConfig` at startup, so debounce timing, exclusion
patterns, and heuristics come straight from Cursor too.

> This is an interoperability project. It does not reimplement or retrain a
> model — it drives Cursor's service with your own credentials. See
> [Legal](#legal--disclaimer).

---

## Requirements

- **Neovim ≥ 0.10**
- **A signed-in Cursor install** (the desktop app) with an active Cursor plan.
  The sidecar reads your token from Cursor's local storage — currently wired for
  the macOS paths (`~/Library/Application Support/Cursor`).
- **[`uv`](https://github.com/astral-sh/uv)** on `PATH` (the sidecar runs via
  `uv run --with 'httpx[http2]'` — no venv to manage).

---

## Install

<details open>
<summary><b>lazy.nvim</b></summary>

```lua
{
  "teocns/neocursor.nvim",
  event = "InsertEnter",
  config = function()
    require("neocursor").setup({
      -- map_tab = false,   -- let nvim-cmp / blink.cmp own <Tab> (see below)
    })
  end,
}
```

</details>

<details>
<summary><b>Local dev (before it's published)</b></summary>

```lua
{
  dir = "/path/to/neocursor.nvim",
  name = "neocursor.nvim",
  event = "InsertEnter",
  config = function() require("neocursor").setup({}) end,
}
```

</details>

### Handing `<Tab>` to your completion engine

If `nvim-cmp` or `blink.cmp` already owns `<Tab>`, set `map_tab = false` and let
their handler fall through to neocursor:

```lua
require("neocursor").setup({ map_tab = false })

-- in your <Tab> keymap, try neocursor first, then fall through:
if require("neocursor").accept() then return end
-- …otherwise let cmp/blink do its thing
```

---

## Configuration

```lua
require("neocursor").setup({
  debounce    = 250,          -- ms; overridden by Cursor's CppConfig at startup
  map_tab     = true,         -- false → another plugin owns <Tab>
  map_partial = "<M-Right>",  -- accept the suggestion word-by-word
  filetypes   = nil,          -- allow-list, e.g. { "python", "lua" }; nil = all
  sidecar_cmd = { "uv", "run", "--with", "httpx[http2]" },
})
```

---

## Usage

| Key / Command | Does |
|---|---|
| `<Tab>` | Accept the suggestion · or **jump** to the predicted next edit · or chain to the next one |
| `<M-Right>` | Accept partially (word by word) |
| `<C-]>` | Dismiss the current suggestion |
| `:NeocursorSuggest` | Force a request right now |
| `:NeocursorLog` | Toggle the live state dashboard |
| `:NeocursorDebug` | Print diagnostics |

**The flow:** type → pause → ghost text appears → `<Tab>` accepts. If Cursor
predicts an edit elsewhere, the ghost is replaced by a `Tab →` hint: the next
`<Tab>` *jumps* your cursor there, and the one after *accepts*. That's the
tab-tab-tab rhythm, verbatim.

---

## Roadmap — what works, what doesn't yet

**Working**
- [x] Reverse-engineered `StreamCpp` (auth token + `x-cursor-checksum`)
- [x] Inline ghost text + `<Tab>` accept
- [x] Diff overlay for rewrites
- [x] `fileDiffHistories` (recent edits) for sharper, deterministic predictions
- [x] NES jump (`cursorPredictionTarget`) — the cross-line/cross-file tab-tab-tab
- [x] Full context parity (`additionalFiles`, `fileDiffHistories`, `linterErrors`)
- [x] Cursor-faithful request gating + newest-wins abort/rescue

**Not there yet**
- [ ] Character-level diffs for single-character changes
- [ ] Linux / Windows token paths (macOS-only today)
- [ ] Cross-file *apply* on jump targets in other files (jump lands; multi-file
      edit chains are partial)
- [ ] Packaging the sidecar so `uv` isn't a hard dependency

---

## How it compares

The Neovim AI-completion field splits three ways, and one slot was empty until now:

- **Single-spot inline** — Copilot, Codeium/Windsurf, Supermaven, minuet. Good
  ghost text, someone else's model, *no next-edit intelligence.*
- **Tab-*style* next-edit** — the active `cursortab.nvim`, `blink-edit.nvim`,
  Copilot's NES (`copilot-lsp`/`sidekick`). They nail the *UX* — cursor jumps,
  multi-line diffs — but run **substitute** models (Sweep, Zed Zeta-2, Mercury,
  or Copilot). Quality/latency ≠ Cursor.
- **Agentic** — avante.nvim. A Cursor *chat/Composer* analog, not Tab at all.

`neocursor.nvim` is the missing fourth: **Tab-style UX on Cursor's own model.**

| Plugin | Model | Next-edit + jumps | Cursor's real backend | Maintained |
|---|---|:---:|:---:|:---:|
| **neocursor.nvim** | **Cursor `StreamCpp`** | **✅** | **✅** | **✅** |
| copilot.vim / copilot.lua | GitHub Copilot | ➖ (NES add-on) | ❌ | ✅ |
| supermaven-nvim | Supermaven "Babble" | ❌ | ❌ | ❌ *discontinued* |
| windsurf.nvim (codeium) | Codeium / Windsurf | ❌ | ❌ | ✅ |
| minuet-ai.nvim | bring-your-own LLM | ❌ | ❌ | ✅ |
| avante.nvim | agentic (chat/apply) | n/a | ❌ | ✅ |
| cursortab.nvim *(active)* | Sweep / Zeta-2 / Mercury | ✅ | ❌ *(removed)* | ✅ |
| cursortab.nvim *(original)* | Cursor's real API | ✅ | ✅ | ❌ *archived* |

> **Prior art, credited honestly:** `reachingforthejack/cursortab.nvim` first
> reverse-engineered Cursor's Tab API — it's now archived, and its maintained
> successor swapped Cursor out for open models. `neocursor.nvim` is the only one
> still speaking Cursor's actual backend.

---

## Legal & disclaimer

`neocursor.nvim` is an independent, **personal-use interoperability** project. It
is not affiliated with, endorsed by, or connected to Anysphere / Cursor.

- It uses **your own** Cursor account and subscription — no credentials are
  stored, transmitted, or shared; the token never leaves your machine except in
  the requests Cursor's client would make anyway.
- It talks to Cursor's private API, which is **not a documented/public
  interface** and may change or break at any time. Using it may be inconsistent
  with Cursor's Terms of Service — that's between you and Cursor.
- No Cursor source code is redistributed. The protocol was observed from a
  signed-in client for interoperability purposes.

Use it on your own account, at your own risk.

## Attribution

The inline ghost-text rendering adapts an MIT-licensed technique from
[`supermaven-nvim`](https://github.com/supermaven-inc/supermaven-nvim) and
[`copilot.lua`](https://github.com/zbirenbaum/copilot.lua). No source was copied
verbatim; see [`NOTICE`](./NOTICE) for details.

---

<sub>Keywords: cursor tab neovim · cursor ai neovim plugin · next edit prediction ·
cursor prediction · copilot alternative · supermaven alternative · codeium
alternative · inline ai completion · ghost text · nvim autocomplete · copilot++ ·
lua neovim plugin</sub>

-- Ghost-text + diff renderer for neocursor.nvim.
--
-- Inline-suggestion technique adapted from supermaven-nvim / copilot.lua (NOTICE):
-- one extmark, first line as `virt_text`, rest as `virt_lines`.
--
-- The diff renderer uses Neovim's built-in `vim.diff` to line-diff the region
-- being replaced against the suggestion, then paints deletions in place (struck
-- through, DiffDelete tint) and additions as DiffAdd `virt_lines` beside them.

local M = {}

local ns = vim.api.nvim_create_namespace("neocursor")
-- own namespace so suggestion clears never kill a pending prediction hint
local ns_pred = vim.api.nvim_create_namespace("neocursor_pred")
local HL = "Comment"
local HINT_HL = "DiagnosticHint"
local HAS_INLINE = vim.fn.has("nvim-0.10") == 1

function M.namespace()
  return ns
end

function M.prediction_namespace()
  return ns_pred
end

function M.clear(bufnr)
  -- diff mode paints many extmarks, so clear the whole namespace, not one id
  pcall(vim.api.nvim_buf_clear_namespace, bufnr or 0, ns, 0, -1)
end

--- "Tab →" pill for the next cursor-prediction target (Cursor's hint widget).
function M.prediction(bufnr, row0, label)
  M.clear_prediction(bufnr)
  vim.api.nvim_buf_set_extmark(bufnr, ns_pred, row0, 0, {
    virt_text = { { "  ⟪" .. label .. "⟫", HINT_HL } },
    virt_text_pos = "eol",
  })
end

function M.clear_prediction(bufnr)
  pcall(vim.api.nvim_buf_clear_namespace, bufnr or 0, ns_pred, 0, -1)
end

-- Highlight groups: additions link to DiffAdd; deletions copy DiffDelete's
-- colors and add strikethrough. Refreshed on colorscheme change.
local function ensure_hl()
  local dd = vim.api.nvim_get_hl(0, { name = "DiffDelete", link = false })
  local del = vim.tbl_deep_extend("force", {}, dd)
  del.strikethrough = true
  vim.api.nvim_set_hl(0, "NeocursorDelete", del)
  vim.api.nvim_set_hl(0, "NeocursorAdd", { link = "DiffAdd", default = true })
end
ensure_hl()
vim.api.nvim_create_autocmd("ColorScheme", { callback = ensure_hl, desc = "neocursor diff highlights" })

---@return string first, table rest  -- rest = virt_lines chunks
local function split_first(text)
  local lines = vim.split(text, "\n", { plain = true })
  local first = table.remove(lines, 1) or ""
  local rest = {}
  for _, l in ipairs(lines) do
    rest[#rest + 1] = { { l == "" and " " or l, HL } }
  end
  return first, rest
end

--- Inline ghost text anchored at the cursor (clean append case).
function M.inline(bufnr, row0, col0, ghost)
  M.clear(bufnr)
  local first, rest = split_first(ghost)
  local opts = { hl_mode = "combine" }
  if first ~= "" then
    opts.virt_text = { { first, HL } }
    opts.virt_text_pos = (HAS_INLINE and first:find("%c") == nil) and "inline" or "eol"
  end
  if #rest > 0 then
    opts.virt_lines = rest
  end
  vim.api.nvim_buf_set_extmark(bufnr, ns, row0, col0, opts)
end

--- Diff overlay for edits that replace existing lines (overrides / rewrites).
--- old_lines = current content of [start0, start0+#old_lines); new_lines = suggestion.
--- hint = label for the discoverability pill, or nil to omit it (show_hints).
function M.diff(bufnr, start0, old_lines, new_lines, hint)
  M.clear(bufnr)
  local hunks = vim.diff(
    table.concat(old_lines, "\n"),
    table.concat(new_lines, "\n"),
    { result_type = "indices" }
  ) or {}
  local nbuf = vim.api.nvim_buf_line_count(bufnr)

  for _, h in ipairs(hunks) do
    local sa, ca, sb, cb = h[1], h[2], h[3], h[4]

    -- deletions: strike + tint the real buffer lines in place
    for i = 0, ca - 1 do
      local lnum = start0 + (sa - 1) + i
      if lnum >= 0 and lnum < nbuf then
        local content = old_lines[sa + i] or ""
        vim.api.nvim_buf_set_extmark(bufnr, ns, lnum, 0, {
          end_row = lnum,
          end_col = #content,
          hl_group = "NeocursorDelete",
          line_hl_group = "DiffDelete",
          hl_mode = "combine",
        })
      end
    end

    -- additions: suggested lines as green virt_lines next to the deletion
    if cb > 0 then
      local vlines = {}
      for i = 0, cb - 1 do
        local l = new_lines[sb + i] or ""
        vlines[#vlines + 1] = { { l == "" and " " or l, "NeocursorAdd" } }
      end
      local anchor, above
      if ca > 0 then
        anchor, above = start0 + (sa - 1) + (ca - 1), false -- below last deleted line
      elseif sa == 0 then
        anchor, above = start0, false -- [dizzi patch] below, not above
      else
        anchor, above = start0 + (sa - 1), false -- pure insert after line sa
      end
      anchor = math.max(0, math.min(anchor, nbuf - 1))
      vim.api.nvim_buf_set_extmark(bufnr, ns, anchor, 0, {
        virt_lines = vlines,
        virt_lines_above = false, -- [dizzi patch] always below
      })
    end
  end

  -- discoverability hint on the region's first line ("jump" vs "accept").
  -- nil when hints are off: the diff itself already shows what the edit does.
  if hint then
    local hint_line = math.max(0, math.min(start0, nbuf - 1))
    vim.api.nvim_buf_set_extmark(bufnr, ns, hint_line, 0, {
      virt_text = { { "  ⟪neocursor · " .. hint .. "⟫", HINT_HL } },
      virt_text_pos = "eol",
    })
  end
end

return M

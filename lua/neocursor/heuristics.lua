-- Client-side suppression, mirroring Cursor's CppConfigResponse.Heuristic set.
-- The server sends which of these to apply (state.cfg.heuristics); each returns
-- true when the suggestion should be withheld. Enum names and the active set are
-- taken from Cursor's config; the detection below is reconstructed from them.

local M = {}

local function line_after(bufnr, end0_excl)
  return vim.api.nvim_buf_get_lines(bufnr, end0_excl, end0_excl + 1, false)[1]
end

function M.HEURISTIC_DUPLICATING_LINE_AFTER_SUGGESTION(c)
  local last = c.lines[#c.lines]
  return last ~= nil and last ~= "" and last == line_after(c.bufnr, c.end0_excl)
end

function M.HEURISTIC_OUTPUT_EXTENDS_BEYOND_RANGE_AND_IS_REPEATED(c)
  local run, prev = 1, nil
  for _, l in ipairs(c.lines) do
    run = (l ~= "" and l == prev) and run + 1 or 1
    if run >= 3 then return true end
    prev = l
  end
  return false
end

function M.HEURISTIC_REVERTING_USER_CHANGE(c)
  if not c.recently_removed then return false end
  for _, l in ipairs(c.lines) do
    if l ~= "" and c.recently_removed[l] then return true end
  end
  return false
end

function M.HEURISTIC_SUGGESTING_RECENTLY_REJECTED_EDIT(c)
  return (c.rejections[c.key] or 0) >= c.hard_reject
end

function M.should_suppress(active, c)
  for _, name in ipairs(active or {}) do
    local check = M[name]
    if check and check(c) then return name end
  end
  return nil
end

return M

-- tabtab.nvim — Cursor Tab, in neovim.
-- Talks to the Python sidecar over stdio; renders the suggested edit as inline
-- ghost text (see preview.lua). <Tab> applies it.

local M = {}

local preview = require("tabtab.preview")
local uv = vim.uv or vim.loop

local state = {
  job = nil,
  ready = false,
  seq = 0,        -- request counter; replies with a stale id are ignored
  partial = "",   -- stdout line-reassembly buffer
  timer = nil,
  suggestion = nil, -- { bufnr, start0, end0_excl, lines, mode }
  cfg = nil,
  ignore_next_move = false, -- guard: accept moves the cursor; don't re-request
}

local function plugin_root()
  local src = debug.getinfo(1, "S").source:sub(2)
  return vim.fn.fnamemodify(src, ":h:h:h") -- .../lua/tabtab/init.lua -> root
end

local function clear_suggestion()
  if state.suggestion then
    preview.clear(state.suggestion.bufnr)
    state.suggestion = nil
  end
end

-- Decide how to show a range-replacement. If the suggested text simply extends
-- what the user has already typed (its prefix equals the buffer content from
-- the range start up to the cursor) and nothing meaningful follows the cursor,
-- we can render it as inline ghost text. Otherwise fall back to a block preview.
local function render_result(res)
  local text = res.text or ""
  if text == "" then return end

  local bufnr = vim.api.nvim_get_current_buf()
  local cur = vim.api.nvim_win_get_cursor(0) -- { row1, col0 }
  local row1, col0 = cur[1], cur[2]

  local start1 = (res.range and res.range.start) or row1
  local end1 = (res.range and res.range.endInclusive) or row1
  local start0 = math.max(0, start1 - 1)
  local end0_excl = end1

  local lines = vim.split(text, "\n", { plain = true })

  -- no-op guard: suggestion identical to what's already there
  local cur_range = vim.api.nvim_buf_get_lines(bufnr, start0, end0_excl, false)
  if table.concat(cur_range, "\n") == text then return end

  local mode, ghost = "diff", nil
  if start0 <= row1 - 1 then
    local ok, prefix_lines = pcall(vim.api.nvim_buf_get_text, bufnr, start0, 0, row1 - 1, col0, {})
    local after = vim.api.nvim_get_current_line():sub(col0 + 1)
    if ok and after:match("^%s*$") then
      local prefix = table.concat(prefix_lines, "\n")
      if text:sub(1, #prefix) == prefix then
        local g = text:sub(#prefix + 1)
        if g ~= "" then mode, ghost = "inline", g end
      end
    end
  end

  state.suggestion = {
    bufnr = bufnr, start0 = start0, end0_excl = end0_excl, lines = lines, mode = mode,
  }
  if mode == "inline" then
    preview.inline(bufnr, row1 - 1, col0, ghost)
  else
    preview.diff(bufnr, start0, cur_range, lines)
  end
end

local function handle_result(res)
  if res.error then
    state.last_error = res.error
    vim.schedule(function()
      vim.notify("tabtab: " .. tostring(res.error), vim.log.levels.WARN)
    end)
    return
  end
  state.last_ok_at = os.time()
  if res.id ~= state.seq then return end -- superseded by a newer keystroke
  vim.schedule(function()
    if vim.api.nvim_buf_is_valid(vim.api.nvim_get_current_buf()) then
      render_result(res)
    end
  end)
end

local function handle_line(line)
  local ok, res = pcall(vim.json.decode, line)
  if ok and type(res) == "table" then handle_result(res) end
end

local function on_stdout(_, data)
  if not data then return end
  data[1] = state.partial .. (data[1] or "")
  state.partial = table.remove(data) or ""
  for _, l in ipairs(data) do
    if l ~= "" then handle_line(l) end
  end
end

local function on_stderr(_, data)
  if not data then return end
  for _, l in ipairs(data) do
    if l ~= "" then
      if l:find("ready") then state.ready = true else state.last_stderr = l end
    end
  end
end

function M.start()
  if state.job then return end
  local cmd = vim.deepcopy(state.cfg.sidecar_cmd)
  table.insert(cmd, plugin_root() .. "/sidecar.py")
  local job = vim.fn.jobstart(cmd, {
    on_stdout = on_stdout,
    on_stderr = on_stderr,
    on_exit = function() state.job = nil; state.ready = false end,
  })
  if job <= 0 then
    vim.notify("tabtab: failed to launch sidecar", vim.log.levels.ERROR)
    return
  end
  state.job = job
end

local function send_request()
  if not state.job then return end
  local bufnr = vim.api.nvim_get_current_buf()
  local cur = vim.api.nvim_win_get_cursor(0)
  state.seq = state.seq + 1
  local name = vim.fn.expand("%:.")
  local req = {
    id = state.seq,
    path = name ~= "" and name or "untitled",
    content = table.concat(vim.api.nvim_buf_get_lines(bufnr, 0, -1, false), "\n"),
    line = cur[1] - 1,
    col = cur[2],
    language = vim.bo[bufnr].filetype ~= "" and vim.bo[bufnr].filetype or "plaintext",
  }
  vim.fn.chansend(state.job, vim.json.encode(req) .. "\n")
end

local function should_attach(bufnr)
  if vim.bo[bufnr].buftype ~= "" then return false end -- skip prompts/terminals/nofile
  local fts = state.cfg.filetypes
  if fts and #fts > 0 and not vim.tbl_contains(fts, vim.bo[bufnr].filetype) then
    return false
  end
  return true
end

local function schedule_request()
  if not should_attach(vim.api.nvim_get_current_buf()) then return end
  clear_suggestion()
  if state.timer then
    state.timer:stop(); state.timer:close(); state.timer = nil
  end
  state.timer = uv.new_timer()
  state.timer:start(state.cfg.debounce, 0, vim.schedule_wrap(function()
    if state.timer then state.timer:stop(); state.timer:close(); state.timer = nil end
    send_request()
  end))
end

function M.accept()
  local s = state.suggestion
  if not s then return false end
  clear_suggestion() -- remove the ghost now (extmark op, allowed under textlock)
  state.ignore_next_move = true
  -- Buffer edits are NOT allowed inside a keymap/expr callback (textlock → E565),
  -- so apply on the next tick. We still return true synchronously so the caller
  -- (blink / expr map) knows the key was consumed.
  vim.schedule(function()
    if not vim.api.nvim_buf_is_valid(s.bufnr) then return end
    vim.cmd("let &undolevels=&undolevels") -- one undo reverts the whole accept
    vim.api.nvim_buf_set_lines(s.bufnr, s.start0, s.end0_excl, false, s.lines)
    local last_row = s.start0 + #s.lines
    local last_col = #(s.lines[#s.lines] or "")
    pcall(vim.api.nvim_win_set_cursor, 0, { last_row, last_col })
  end)
  return true
end

function M.dismiss() clear_suggestion() end
function M.has_suggestion() return state.suggestion ~= nil end
function M.suggest() send_request() end -- manual trigger (:TabtabSuggest)

function M.setup(opts)
  opts = opts or {}
  state.cfg = {
    debounce = opts.debounce or 250,
    sidecar_cmd = opts.sidecar_cmd or { "uv", "run", "--with", "httpx[http2]" },
    map_tab = opts.map_tab ~= false, -- set false when another plugin (cmp) owns <Tab>
    filetypes = opts.filetypes,      -- optional allow-list; nil = all normal buffers
  }
  M.start()

  local grp = vim.api.nvim_create_augroup("tabtab", { clear = true })
  vim.api.nvim_create_autocmd({ "TextChangedI", "CursorMovedI" }, {
    group = grp,
    callback = function()
      if state.ignore_next_move then
        state.ignore_next_move = false
        return
      end
      schedule_request()
    end,
  })
  vim.api.nvim_create_autocmd({ "InsertLeave", "BufLeave" }, {
    group = grp, callback = clear_suggestion,
  })

  if state.cfg.map_tab then
    vim.keymap.set("i", "<Tab>", function()
      if M.accept() then return "" end
      return "<Tab>"
    end, { expr = true, replace_keycodes = true, desc = "tabtab: accept or tab" })
  end

  vim.keymap.set("i", "<C-]>", M.dismiss, { desc = "tabtab: dismiss" })
  vim.api.nvim_create_user_command("TabtabSuggest", M.suggest, { desc = "request a suggestion now" })

  vim.api.nvim_create_user_command("TabtabDebug", function()
    local s = state.suggestion
    local lines = {
      "── tabtab debug ──",
      "sidecar job : " .. tostring(state.job) .. (state.ready and "  READY" or "  (no ready signal)"),
      "requests    : seq=" .. tostring(state.seq) .. "  last_ok=" .. tostring(state.last_ok_at or "never"),
      "suggestion  : " .. (s and (s.mode .. "  lines=" .. #s.lines) or "none"),
      "buffer      : buftype='" .. vim.bo.buftype .. "'  filetype='" .. vim.bo.filetype .. "'",
      "attach ok   : " .. tostring(should_attach(vim.api.nvim_get_current_buf())),
      "last error  : " .. tostring(state.last_error or "none"),
      "last stderr : " .. tostring(state.last_stderr or "none"),
    }
    vim.notify(table.concat(lines, "\n"), vim.log.levels.INFO)
  end, { desc = "tabtab diagnostics" })

  vim.notify("tabtab ready — type in insert mode; <Tab> accepts", vim.log.levels.INFO)
end

return M

-- tabtab.nvim — Cursor Tab, in neovim.
-- Talks to the Python sidecar over stdio; renders the suggested edit as inline
-- ghost text (see preview.lua). <Tab> applies it.

local M = {}

local preview = require("tabtab.preview")
local heuristics = require("tabtab.heuristics")
local uv = vim.uv or vim.loop

local state = {
  job = nil,
  ready = false,
  seq = 0,        -- request counter; replies with a stale id are ignored
  partial = "",   -- stdout line-reassembly buffer
  timer = nil,
  suggestion = nil, -- { bufnr, start0, end0_excl, lines, mode }
  cfg = nil,
  walking = false, -- true while applying a chain locally; suppresses auto-requests
  queue = nil,     -- { list = {edit,...}, idx } multi-edit chain from one response
  viewed = {},    -- [bufnr] = ms of last BufEnter (recency for additionalFiles)
  dbase = {},     -- [bufnr] = { path, text } baseline snapshot for diffing
  dtraj = {},     -- [path]  = { {diff, ts}, ... } committed edit trajectory
  rejects = {},   -- [key]   = times the user dismissed this exact suggestion
  log = {},       -- ring buffer of event strings for :TabtabLog
  log_buf = nil,
  log_dirty = false,
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
  state.queue = nil -- abandon any pending multi-edit chain
end

-- Persistent event log rendered by :TabtabLog. Every request, response, render,
-- suppression, jump/accept, and sidecar event lands here, so the whole pipeline
-- is visible at all times rather than in a notification that vanishes.
local function log_refresh()
  state.log_dirty = false
  local buf = state.log_buf
  if not (buf and vim.api.nvim_buf_is_valid(buf)) then return end
  local c = state.cfg or {}
  local lines = {
    ("── tabtab ─ debounce=%sms · heuristics=%d · excludes=%d · sidecar=%s ──"):format(
      c.debounce or "?", c.heuristics and #c.heuristics or 0,
      c.exclude_patterns and #c.exclude_patterns or 0,
      state.ready and "ready" or (state.job and "starting" or "down")),
    ("seq=%d  chain=%s  last_error=%s"):format(
      state.seq, state.queue and (state.queue.idx .. "/" .. #state.queue.list) or "none",
      tostring(state.last_error or "none")),
    "",
  }
  for _, l in ipairs(state.log) do lines[#lines + 1] = l end
  vim.bo[buf].modifiable = true
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = false
  for _, win in ipairs(vim.api.nvim_list_wins()) do
    if vim.api.nvim_win_get_buf(win) == buf then
      pcall(vim.api.nvim_win_set_cursor, win, { #lines, 0 })
    end
  end
end

local function log(line)
  state.log[#state.log + 1] = os.date("%H:%M:%S") .. "  " .. line
  if #state.log > 500 then table.remove(state.log, 1) end
  if not state.log_dirty then
    state.log_dirty = true
    vim.schedule(log_refresh)
  end
end

-- Tunables the sidecar fetched from Cursor's CppConfig (debounce, context
-- exclude-list, active heuristics, rejection threshold). Nil fields are left
-- at their defaults so a failed fetch degrades gracefully.
local function apply_config(cfg)
  if type(cfg.debounce) == "number" and cfg.debounce > 0 then state.cfg.debounce = cfg.debounce end
  if type(cfg.exclude_patterns) == "table" then state.cfg.exclude_patterns = cfg.exclude_patterns end
  if type(cfg.heuristics) == "table" then state.cfg.heuristics = cfg.heuristics end
  if type(cfg.reject_hard) == "number" then state.cfg.reject_hard = cfg.reject_hard end
  log(("CONFIG  debounce=%sms heuristics=%d excludes=%d"):format(
    state.cfg.debounce, #state.cfg.heuristics, #state.cfg.exclude_patterns))
end

-- Is the cursor currently on the edit's target region? This is Cursor's
-- `cursorAtInlineEdit`: when false, <Tab> jumps here; when true, <Tab> accepts.
local function cursor_at(start0, end0_excl)
  local row0 = vim.api.nvim_win_get_cursor(0)[1] - 1
  if end0_excl <= start0 then return row0 == start0 end
  return row0 >= start0 and row0 < end0_excl
end

-- Render one edit. If its text simply extends what the user has already typed
-- (prefix equals buffer content from the range start up to the cursor, nothing
-- meaningful after), show it as inline ghost text; otherwise a block diff.
-- Returns false if the edit is a no-op (identical to what's already there).
local function show_edit(edit)
  local bufnr = edit.bufnr
  local start0, end0_excl, lines = edit.start0, edit.end0_excl, edit.lines
  local cur = vim.api.nvim_win_get_cursor(0) -- { row1, col0 }
  local row1, col0 = cur[1], cur[2]
  local text = table.concat(lines, "\n")

  local cur_range = vim.api.nvim_buf_get_lines(bufnr, start0, end0_excl, false)
  if table.concat(cur_range, "\n") == text then return false end -- no-op

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
    local at = cursor_at(start0, end0_excl)
    preview.diff(bufnr, start0, cur_range, lines, at and "<Tab> accept" or "<Tab> jump")
  end
  log(("SHOW    %-6s L%d  (%d ln)"):format(mode, start0 + 1, #lines))
  return true
end

-- After applying the current edit, walk to the next one in the chain — locally,
-- no network. Adjust the line numbers of edits below by the applied line delta,
-- jump the cursor there, and render it. This is the "tab, tab, tab" loop.
local function advance_after_apply(applied)
  local q = state.queue
  if not q then return end
  local delta = #applied.lines - (applied.end0_excl - applied.start0)
  q.idx = q.idx + 1
  if delta ~= 0 then
    for k = q.idx, #q.list do
      local e = q.list[k]
      if e.start0 >= applied.end0_excl then
        e.start0 = e.start0 + delta
        e.end0_excl = e.end0_excl + delta
      end
    end
  end
  -- Show the next edit in place; the cursor stays put, so the next <Tab> JUMPS
  -- to it (Cursor's cursorAtInlineEdit rule), and the <Tab> after that accepts.
  while q.idx <= #q.list do
    if show_edit(q.list[q.idx]) then return end
    q.idx = q.idx + 1 -- skip no-op edits
  end
  state.queue = nil -- chain exhausted
end

local function reject_key(path, edit)
  return path .. "@" .. edit.start0 .. ":" .. table.concat(edit.lines, "\n")
end

-- Lines the user just deleted (the newest diff's "-" lines), for the
-- reverting-user-change heuristic.
local function recently_removed(path)
  local traj = state.dtraj[path]
  if not traj or #traj == 0 then return nil end
  local set = {}
  for line in traj[#traj].diff:gmatch("[^\n]+") do
    if line:sub(1, 1) == "-" and line:sub(1, 3) ~= "---" then set[line:sub(2)] = true end
  end
  return set
end

-- Build the edit queue from a sidecar result and show the first showable edit.
local function render_result(res)
  local bufnr = vim.api.nvim_get_current_buf()
  local edits_in = res.edits
  if not edits_in or #edits_in == 0 then
    if (res.text or "") == "" then log("NOOP    empty response"); return end
    edits_in = { { text = res.text, range = res.range } } -- back-compat
  end
  local row1 = vim.api.nvim_win_get_cursor(0)[1]
  local list = {}
  for _, e in ipairs(edits_in) do
    local r = e.range
    local start1 = (r and r.start) or row1
    local end1 = (r and r.endInclusive) or row1
    list[#list + 1] = {
      bufnr = bufnr,
      start0 = math.max(0, start1 - 1),
      end0_excl = end1,
      lines = vim.split(e.text or "", "\n", { plain = true }),
    }
  end

  local path = vim.fn.expand("%:.")
  local first = list[1]
  local suppressed = heuristics.should_suppress(state.cfg.heuristics, {
    bufnr = bufnr, start0 = first.start0, end0_excl = first.end0_excl, lines = first.lines,
    recently_removed = recently_removed(path),
    rejections = state.rejects, key = reject_key(path, first), hard_reject = state.cfg.reject_hard,
  })
  if suppressed then
    state.last_suppressed = suppressed
    state.queue = nil
    log("SUPPRESS " .. suppressed)
    return
  end

  state.queue = { list = list, idx = 1 }
  while state.queue.idx <= #list do
    if show_edit(list[state.queue.idx]) then return end
    state.queue.idx = state.queue.idx + 1
  end
  state.queue = nil
  log("NOOP    suggestion matches buffer")
end

local function result_summary(res)
  local parts = {}
  for _, e in ipairs(res.edits or {}) do
    parts[#parts + 1] = e.range and ("L" .. e.range.start .. "-" .. e.range.endInclusive) or "L?"
  end
  local pred = res.prediction and ("  pred=" .. tostring(res.prediction.path) .. ":" .. tostring(res.prediction.line)) or ""
  return ("edits=%d  [%s]%s"):format(#(res.edits or {}), table.concat(parts, ", "), pred)
end

local function handle_result(res)
  if res.error then
    state.last_error = res.error
    log("ERR     #" .. tostring(res.id) .. "  " .. tostring(res.error))
    vim.schedule(function()
      vim.notify("tabtab: " .. tostring(res.error), vim.log.levels.WARN)
    end)
    return
  end
  state.last_ok_at = os.time()
  if res.id ~= state.seq then
    log(("RES     #%s  (stale, seq=%d)  %s"):format(tostring(res.id), state.seq, result_summary(res)))
    return
  end
  log(("RES     #%s  %s"):format(tostring(res.id), result_summary(res)))
  vim.schedule(function()
    if vim.api.nvim_buf_is_valid(vim.api.nvim_get_current_buf()) then
      render_result(res)
    end
  end)
end

local function handle_line(line)
  -- luanil so JSON null decodes to nil, not vim.NIL (which is a truthy userdata
  -- and would crash `res.prediction`/`e.range` guards).
  local ok, res = pcall(vim.json.decode, line, { luanil = { object = true, array = true } })
  if not (ok and type(res) == "table") then return end
  if res.config then apply_config(res.config) else handle_result(res) end
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
      if l:find("ready") then state.ready = true; log("SIDECAR ready") else state.last_stderr = l; log("SIDECAR " .. l) end
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
    on_exit = function() state.job = nil; state.ready = false; log("SIDECAR exited") end,
  })
  if job <= 0 then
    vim.notify("tabtab: failed to launch sidecar", vim.log.levels.ERROR)
    log("SIDECAR launch failed")
    return
  end
  state.job = job
  log("SIDECAR launching")
end

-- Gather the proximity context Cursor's native Tab sends as `additionalFiles`:
-- other visible splits (isOpen=true) plus the most-recently-visited buffers
-- (isOpen=false), each reduced to a bounded on-screen/near-cursor slice.
local function collect_additional_files(cur_buf)
  local MAX_FILES, MAX_LINES = 8, 200
  local files, seen = {}, {}

  local function relpath(b)
    local n = vim.api.nvim_buf_get_name(b)
    if n == "" then return nil end
    return vim.fn.fnamemodify(n, ":.")
  end
  local function usable(b)
    return vim.api.nvim_buf_is_loaded(b)
      and vim.bo[b].buftype == ""
      and vim.api.nvim_buf_get_name(b) ~= ""
  end
  local function excluded(path)
    for _, pat in ipairs(state.cfg.exclude_patterns or {}) do
      if path:find(pat, 1, true) then return true end
    end
    return false
  end
  local function push(b, is_open, top, bot, ts)
    local path = relpath(b)
    if not path or excluded(path) then return end
    local lines = vim.api.nvim_buf_get_lines(b, top - 1, bot, false)
    if #lines == 0 then return end
    seen[b] = true
    files[#files + 1] = {
      path = path, is_open = is_open, last_viewed_at = ts,
      ranges = { { start = top, stop = top + #lines - 1, content = table.concat(lines, "\n") } },
    }
  end

  -- 1) other visible splits → exactly what's on screen there
  for _, win in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
    local b = vim.api.nvim_win_get_buf(win)
    if b ~= cur_buf and not seen[b] and usable(b) then
      push(b, true, math.max(1, vim.fn.line("w0", win)), vim.fn.line("w$", win), state.viewed[b])
    end
  end

  -- 2) recently-visited buffers not on screen → a slice around their last cursor
  local mru = {}
  for b, t in pairs(state.viewed) do
    if b ~= cur_buf and not seen[b] and usable(b) then mru[#mru + 1] = { b = b, t = t } end
  end
  table.sort(mru, function(x, y) return x.t > y.t end)
  for _, e in ipairs(mru) do
    if #files >= MAX_FILES then break end
    local total = vim.api.nvim_buf_line_count(e.b)
    local mark = vim.api.nvim_buf_get_mark(e.b, '"')
    local center = (mark[1] > 0) and mark[1] or 1
    local top = math.max(1, center - math.floor(MAX_LINES / 2))
    push(e.b, false, top, math.min(total, top + MAX_LINES - 1), e.t)
  end

  return files
end

local function buf_relpath(b)
  local n = vim.api.nvim_buf_get_name(b)
  if n == "" then return nil end
  return vim.fn.fnamemodify(n, ":.")
end

local function buf_text(b)
  return table.concat(vim.api.nvim_buf_get_lines(b, 0, -1, false), "\n")
end

local function cap(s, n)
  if #s > n then return s:sub(1, n) .. "\n… (truncated)" end
  return s
end

-- linterErrors: current-buffer diagnostics → native LinterError shape (0-indexed).
-- vim.diagnostic.severity (ERROR/WARN/INFO/HINT = 1/2/3/4) matches Cursor's enum 1:1.
local SEV = {
  [vim.diagnostic.severity.ERROR] = 1,
  [vim.diagnostic.severity.WARN] = 2,
  [vim.diagnostic.severity.INFO] = 3,
  [vim.diagnostic.severity.HINT] = 4,
}
local function collect_linter_errors(buf, path)
  if not path then return nil end
  local diags = vim.diagnostic.get(buf)
  if #diags == 0 then return nil end
  local errors = {}
  for _, d in ipairs(diags) do
    if #errors >= 30 then break end
    errors[#errors + 1] = {
      message = d.message or "",
      source = d.source,
      severity = SEV[d.severity] or 1,
      range = {
        sl = d.lnum or 0, sc = d.col or 0,
        el = d.end_lnum or d.lnum or 0, ec = d.end_col or d.col or 0,
      },
    }
  end
  return { path = path, errors = errors }
end

-- diff trajectory: baseline snapshot per buffer; unified diff baseline→current is
-- the edit. commit_diff() coalesces at logical boundaries (InsertLeave/BufLeave).
local MAX_TRAJ, DIFF_CAP = 6, 4000

local function ensure_baseline(buf, path)
  if path and not state.dbase[buf] then
    state.dbase[buf] = { path = path, text = buf_text(buf) }
  end
end

-- returns (diff_string, current_text) or nil if unchanged
local function buf_diff(buf)
  local b = state.dbase[buf]
  if not b then return nil end
  local cur = buf_text(buf)
  if cur == b.text then return nil end
  local d = vim.diff(b.text .. "\n", cur .. "\n", { result_type = "unified", ctxlen = 3 })
  if type(d) ~= "string" or d == "" then return nil end
  return cap(d, DIFF_CAP), cur
end

local function commit_diff(buf)
  local path = buf_relpath(buf)
  if not path then return end
  local d, cur = buf_diff(buf)
  if not d then return end
  local traj = state.dtraj[path]
  if not traj then traj = {}; state.dtraj[path] = traj end
  traj[#traj + 1] = { diff = d, ts = os.time() * 1000 }
  while #traj > MAX_TRAJ do table.remove(traj, 1) end
  state.dbase[buf] = { path = path, text = cur } -- advance baseline past this edit
end

local function collect_file_diff_histories(cur_buf, cur_path)
  local by_path = {}
  for path, traj in pairs(state.dtraj) do
    if #traj > 0 then
      local diffs, ts = {}, {}
      for _, e in ipairs(traj) do diffs[#diffs + 1] = e.diff; ts[#ts + 1] = e.ts end
      by_path[path] = { file_name = path, diff_history = diffs, diff_history_timestamps = ts }
    end
  end
  -- append the uncommitted in-progress edit of the current file as the newest step
  local d = buf_diff(cur_buf)
  if d and cur_path then
    local rec = by_path[cur_path]
    if not rec then
      rec = { file_name = cur_path, diff_history = {}, diff_history_timestamps = {} }
      by_path[cur_path] = rec
    end
    rec.diff_history[#rec.diff_history + 1] = d
    rec.diff_history_timestamps[#rec.diff_history_timestamps + 1] = os.time() * 1000
  end
  local arr = {}
  for _, rec in pairs(by_path) do arr[#arr + 1] = rec end
  if #arr == 0 then return nil end
  return arr
end

local function send_request()
  if not state.job then return end
  local bufnr = vim.api.nvim_get_current_buf()
  local cur = vim.api.nvim_win_get_cursor(0)
  state.seq = state.seq + 1
  local name = vim.fn.expand("%:.")
  local path = name ~= "" and name or "untitled"
  ensure_baseline(bufnr, buf_relpath(bufnr))
  local rp = buf_relpath(bufnr)
  local adds = collect_additional_files(bufnr)
  local lint = collect_linter_errors(bufnr, rp)
  local fdh = collect_file_diff_histories(bufnr, rp)
  local req = {
    id = state.seq,
    path = path,
    content = buf_text(bufnr),
    line = cur[1] - 1,
    col = cur[2],
    language = vim.bo[bufnr].filetype ~= "" and vim.bo[bufnr].filetype or "plaintext",
    additional_files = adds,
    linter_errors = lint,
    file_diff_histories = fdh,
  }
  vim.fn.chansend(state.job, vim.json.encode(req) .. "\n")
  log(("REQ     #%d  %s %d:%d  ctx=%d diffs=%d lint=%d"):format(
    state.seq, path, cur[1] - 1, cur[2], #adds, fdh and #fdh or 0, lint and #lint.errors or 0))
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

-- Apply the current edit, then reveal the next one (cursor stays put — the user
-- <Tab>s to jump to it). Buffer edits are illegal under textlock (E565) so defer
-- to the next tick. state.walking gates our own churn from triggering requests.
local function accept_current(s)
  local q = state.queue
  preview.clear(s.bufnr)
  state.suggestion = nil
  vim.schedule(function()
    if not vim.api.nvim_buf_is_valid(s.bufnr) then return end
    state.walking = true
    vim.cmd("let &undolevels=&undolevels") -- one undo reverts the whole accept
    vim.api.nvim_buf_set_lines(s.bufnr, s.start0, s.end0_excl, false, s.lines)
    pcall(vim.api.nvim_win_set_cursor, 0, { s.start0 + #s.lines, #(s.lines[#s.lines] or "") })
    state.queue = q
    advance_after_apply(s) -- local; no network
    state.walking = false
  end)
end

-- Move the cursor onto the edit (local, instant). This flips cursorAtInlineEdit
-- true, so the NEXT <Tab> accepts. Preview stays; its hint refreshes to "accept".
local function jump_to(s)
  vim.schedule(function()
    if not vim.api.nvim_buf_is_valid(s.bufnr) then return end
    state.walking = true
    local lc = vim.api.nvim_buf_line_count(s.bufnr)
    pcall(vim.api.nvim_win_set_cursor, 0, { math.min(s.start0 + 1, lc), 0 })
    show_edit(s)
    state.walking = false
  end)
end

-- <Tab> handler: jump-first, accept-second — Cursor's two-phase feel. Returns
-- true synchronously so the caller (blink / expr map) knows Tab was consumed.
function M.accept()
  local s = state.suggestion
  if not s then return false end
  if cursor_at(s.start0, s.end0_excl) then
    log("ACCEPT  L" .. (s.start0 + 1))
    accept_current(s)
  else
    log("JUMP    L" .. (s.start0 + 1))
    jump_to(s)
  end
  return true
end

function M.dismiss()
  local s = state.suggestion
  if s then
    local key = reject_key(vim.fn.expand("%:."), s)
    state.rejects[key] = (state.rejects[key] or 0) + 1
    log(("DISMISS L%d  (rejected ×%d)"):format(s.start0 + 1, state.rejects[key]))
  end
  clear_suggestion()
end

function M.log()
  if not (state.log_buf and vim.api.nvim_buf_is_valid(state.log_buf)) then
    state.log_buf = vim.api.nvim_create_buf(false, true)
    vim.bo[state.log_buf].bufhidden = "hide"
    vim.bo[state.log_buf].filetype = "tabtablog"
    pcall(vim.api.nvim_buf_set_name, state.log_buf, "tabtab://log")
  end
  for _, win in ipairs(vim.api.nvim_list_wins()) do
    if vim.api.nvim_win_get_buf(win) == state.log_buf then
      vim.api.nvim_win_close(win, true) -- toggle off if already open
      return
    end
  end
  vim.cmd("botright 14split")
  local win = vim.api.nvim_get_current_win()
  vim.api.nvim_win_set_buf(win, state.log_buf)
  vim.wo[win].number, vim.wo[win].relativenumber, vim.wo[win].wrap = false, false, false
  log_refresh()
  vim.cmd("wincmd p") -- keep focus where the user was typing
end
function M.has_suggestion() return state.suggestion ~= nil end
function M.suggest() send_request() end -- manual trigger (:TabtabSuggest)

function M.setup(opts)
  opts = opts or {}
  state.cfg = {
    debounce = opts.debounce or 250,
    sidecar_cmd = opts.sidecar_cmd or { "uv", "run", "--with", "httpx[http2]" },
    map_tab = opts.map_tab ~= false, -- set false when another plugin (cmp) owns <Tab>
    filetypes = opts.filetypes,      -- optional allow-list; nil = all normal buffers
    exclude_patterns = {},           -- filled from CppConfig (skip .env/.pem/... as context)
    heuristics = {},                 -- filled from CppConfig (active suppression rules)
    reject_hard = 2,
  }
  M.start()

  local grp = vim.api.nvim_create_augroup("tabtab", { clear = true })
  vim.api.nvim_create_autocmd({ "TextChangedI", "CursorMovedI" }, {
    group = grp,
    callback = function()
      if state.walking then return end -- our own edits/jumps while walking a chain
      schedule_request()
    end,
  })
  vim.api.nvim_create_autocmd({ "InsertLeave", "BufLeave" }, {
    group = grp,
    callback = function(args)
      commit_diff(args.buf) -- coalesce the just-finished edit into the trajectory
      clear_suggestion()
    end,
  })
  vim.api.nvim_create_autocmd("BufEnter", {
    group = grp,
    callback = function(args)
      if vim.bo[args.buf].buftype == "" and vim.api.nvim_buf_get_name(args.buf) ~= "" then
        state.viewed[args.buf] = os.time() * 1000
        ensure_baseline(args.buf, buf_relpath(args.buf))
      end
    end,
  })

  if state.cfg.map_tab then
    vim.keymap.set("i", "<Tab>", function()
      if M.accept() then return "" end
      return "<Tab>"
    end, { expr = true, replace_keycodes = true, desc = "tabtab: accept or tab" })
  end

  vim.keymap.set("i", "<C-]>", M.dismiss, { desc = "tabtab: dismiss" })
  vim.api.nvim_create_user_command("TabtabSuggest", M.suggest, { desc = "request a suggestion now" })
  vim.api.nvim_create_user_command("TabtabLog", M.log, { desc = "toggle the tabtab live log pane" })

  vim.api.nvim_create_user_command("TabtabDebug", function()
    local s = state.suggestion
    local dbuf = vim.api.nvim_get_current_buf()
    local dlint = collect_linter_errors(dbuf, buf_relpath(dbuf))
    local dfdh = collect_file_diff_histories(dbuf, buf_relpath(dbuf))
    local lines = {
      "── tabtab debug ──",
      "sidecar job : " .. tostring(state.job) .. (state.ready and "  READY" or "  (no ready signal)"),
      "requests    : seq=" .. tostring(state.seq) .. "  last_ok=" .. tostring(state.last_ok_at or "never"),
      "suggestion  : " .. (s and (s.mode .. "  lines=" .. #s.lines) or "none"),
      "chain       : " .. (state.queue and (state.queue.idx .. "/" .. #state.queue.list) or "none"),
      "config      : debounce=" .. state.cfg.debounce .. "ms  heuristics=" .. #state.cfg.heuristics
        .. "  excludes=" .. #state.cfg.exclude_patterns,
      "last suppress: " .. tostring(state.last_suppressed or "none"),
      "buffer      : buftype='" .. vim.bo.buftype .. "'  filetype='" .. vim.bo.filetype .. "'",
      "attach ok   : " .. tostring(should_attach(vim.api.nvim_get_current_buf())),
      "ctx files   : " .. tostring(#collect_additional_files(dbuf)),
      "linter errs : " .. tostring(dlint and #dlint.errors or 0),
      "diff files  : " .. tostring(dfdh and #dfdh or 0),
      "last error  : " .. tostring(state.last_error or "none"),
      "last stderr : " .. tostring(state.last_stderr or "none"),
    }
    vim.notify(table.concat(lines, "\n"), vim.log.levels.INFO)
  end, { desc = "tabtab diagnostics" })

  vim.notify("tabtab ready — type in insert mode; <Tab> accepts", vim.log.levels.INFO)
end

return M

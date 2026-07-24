-- Repeatability meter: drive the REAL sidecar through the "type one char of
-- intent, then pause" gesture N times and report how many trials actually
-- render a suggestion. A 1:1 Cursor port should be ~N/N; flaky is anything less.
--   nvim --headless -u NORC -S test/flaky_meter.lua
local root = vim.fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":h:h")
vim.opt.rtp:prepend(root)
vim.opt.swapfile = false
io.stdout:setvbuf("no")

-- a doc with an obvious pattern to continue: two converted rows, one bare URL
local SEED = {
  "## Repos",
  "",
  "[./alpha/](https://github.com/acme/alpha) | Alpha | ~/acme/alpha",
  "[./beta/](https://github.com/acme/beta) | Beta | ~/acme/beta",
  "https://github.com/acme/gamma",
  "",
}
local TARGET = 5           -- the bare-URL line (1-indexed)
local TRIALS = 8
local hits, done = 0, 0

require("neocursor").setup({ debounce = 250 })
local nc = require("neocursor")

local function fresh_buf()
  vim.cmd("enew!")
  vim.bo.filetype = "markdown"
  vim.api.nvim_buf_set_name(0, "/tmp/nc_flaky_" .. done .. ".md")
  vim.api.nvim_buf_set_lines(0, 0, -1, false, SEED)
end

local function feed(keys)
  vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes(keys, true, false, true), "n", false)
end

local function trial(fn)
  fresh_buf()
  -- place cursor at col 0 of the bare URL line, enter insert, type "[" (intent)
  vim.api.nvim_win_set_cursor(0, { TARGET, 0 })
  feed("i[")
  -- pause ~1.3s for the (debounce + round trip), then observe
  vim.defer_fn(function()
    local got = nc.has_suggestion()
    if got then hits = hits + 1 end
    io.stdout:write(("trial %d/%d  suggestion=%s\n"):format(done + 1, TRIALS, tostring(got)))
    feed("<Esc>")
    done = done + 1
    vim.defer_fn(fn, 150)
  end, 1400)
end

local function run(i)
  if i > TRIALS then
    io.stdout:write(("\nRESULT  %d/%d trials rendered a suggestion (%.0f%%)\n")
      :format(hits, TRIALS, 100 * hits / TRIALS))
    vim.cmd(hits == TRIALS and "qa!" or "cq!")
    return
  end
  trial(function() run(i + 1) end)
end

-- give the sidecar time to boot + fetch CppConfig before trial 1
vim.defer_fn(function() run(1) end, 2500)

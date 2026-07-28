-- Integration spec: the user's REAL Neovim config, with Tab owned by
-- blink.cmp's expr-mapped super-tab chain (map_tab = false) — the exact live
-- path. Only the sidecar is swapped for the canned one. Run:
--   nvim --headless -c "luafile <repo>/test/blink_spec.lua"
-- (no -u NONE: the point is loading the real init.lua + lazy plugins)
local root = vim.fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":h:h")
vim.opt.swapfile = false
io.stdout:setvbuf("no")

local failed = 0
local function check(desc, got, want)
  local ok = vim.deep_equal(got, want)
  if not ok then failed = failed + 1 end
  io.stdout:write(("%s %s%s\n"):format(ok and "ok  " or "FAIL", desc,
    ok and "" or ("  got=" .. vim.inspect(got) .. "  want=" .. vim.inspect(want))))
end

-- open a scratch file and force the InsertEnter lazy-load of blink + neocursor
vim.cmd("edit /tmp/nc_blink_spec.md")
vim.cmd("doautocmd InsertEnter")

local nc = require("neocursor")
nc.stop()
nc.setup({
  debounce = 30,
  -- "python3" doesn't exist on Windows; the canned sidecar is stdlib-only
  sidecar_cmd = { vim.fn.executable("python3") == 1 and "python3" or "python", root .. "/test/fake_sidecar.py" },
  map_tab = false, -- as in the user's config: blink's chain owns <Tab>
})

local seed = { "line1 = 1", "line2 = 2", "line3 = 3", "line4 = 4", "line5 = 5" }
vim.api.nvim_buf_set_lines(0, 0, -1, false, seed)

-- prove which mapping owns <Tab> in this buffer (buffer-local blink expr map)
local map = vim.fn.maparg("<Tab>", "i", false, true)
io.stdout:write((".. <Tab> owner: %s (expr=%s, buffer=%s)\n"):format(
  tostring(map.desc), tostring(map.expr), tostring(map.buffer)))

local function feed(keys)
  vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes(keys, true, false, true), "x!", false)
end
local function input(keys) vim.api.nvim_input(keys) end
local function later(ms, fn) vim.defer_fn(fn, ms) end
local function poll(cond, timeout_ms, on_ok, on_fail)
  local waited = 0
  local function tick()
    if cond() then return on_ok() end
    waited = waited + 20
    if waited >= timeout_ms then return on_fail() end
    later(20, tick)
  end
  tick()
end
local function line(n) return vim.api.nvim_buf_get_lines(0, n - 1, n, false)[1] end
local function buf_text() return table.concat(vim.api.nvim_buf_get_lines(0, 0, -1, false), "\n") end

local function round1()
  poll(nc.has_suggestion, 5000, function()
    check("suggestion arrives (real config)", true, true)
    input("<Tab>")
    later(150, function()
      check("Tab #1 accepts edit 1 via blink chain", line(1), "line1 = 100")
      check("chain edit 2 survives", nc.has_suggestion(), true)
      input("<Tab>")
      later(150, function()
        check("Tab #2 jumps to edit 2", vim.api.nvim_win_get_cursor(0)[1], 4)
        check("suggestion survives jump", nc.has_suggestion(), true)
        input("<Tab>")
        later(150, function()
          check("Tab #3 accepts edit 2", line(4), "line4 = 400")
          check("no literal <Tab> leaked", buf_text():find("\t") == nil, true)
          input("<Esc>")
        end)
      end)
    end)
  end, function()
    check("suggestion arrives (real config)", false, true)
    input("<Esc>")
  end)
end

vim.api.nvim_win_set_cursor(0, { 1, 0 })
later(50, round1)
feed("Ax") -- blocks while round1 drives the keys; returns on its <Esc>

-- dump the plugin's own event log (the :NeocursorLog ring) for diagnosis
nc.log()
for _, b in ipairs(vim.api.nvim_list_bufs()) do
  if vim.api.nvim_buf_get_name(b):find("neocursor://log", 1, true) then
    local ls = vim.api.nvim_buf_get_lines(b, 0, -1, false)
    for i = math.max(1, #ls - 40), #ls do io.stdout:write("LOG " .. ls[i] .. "\n") end
  end
end

io.stdout:write(failed == 0 and "ALL PASS\n" or (failed .. " FAILURES\n"))
vim.cmd(failed == 0 and "qall!" or "cquit!")

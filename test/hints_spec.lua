-- Spec for `show_hints`: the hint chrome is display-only. Run from the repo root:
--   nvim --headless -u NONE -c "luafile test/hints_spec.lua"
--
-- Two surfaces are covered: the ⟪neocursor · <Tab> …⟫ label painted beside a diff
-- (preview.diff) and the ⟪<Tab> → L42⟫ pill at a jump target (preview.prediction).
-- The property that matters is asymmetric: turning hints off must remove the
-- label and nothing else — the deletions, the additions, and (in init.lua) the
-- jump gating all stay exactly as they were.
local root = vim.fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":h:h")
vim.opt.rtp:prepend(root)
vim.opt.swapfile = false
io.stdout:setvbuf("no")

local failed = 0
local function check(desc, got, want)
  local ok = vim.deep_equal(got, want)
  if not ok then failed = failed + 1 end
  io.stdout:write(("%s %s%s\n"):format(ok and "ok  " or "FAIL", desc,
    ok and "" or ("  got=" .. vim.inspect(got) .. "  want=" .. vim.inspect(want))))
end

local preview = require("neocursor.preview")
local ns = preview.namespace()

local bufnr = vim.api.nvim_create_buf(false, true)
vim.api.nvim_set_current_buf(bufnr)
local seed = { "line1 = 1", "line2 = 2", "line3 = 3" }

local function reseed()
  vim.api.nvim_buf_set_lines(bufnr, 0, -1, false, seed)
  preview.clear(bufnr)
  preview.clear_prediction(bufnr)
end

-- every extmark in a namespace, with its virt_text flattened to a plain string
local function marks(namespace)
  local out = {}
  for _, m in ipairs(vim.api.nvim_buf_get_extmarks(bufnr, namespace, 0, -1, { details = true })) do
    local text = ""
    for _, chunk in ipairs(m[4].virt_text or {}) do text = text .. chunk[1] end
    out[#out + 1] = { row = m[2], text = text, details = m[4] }
  end
  return out
end

local function hint_marks()
  local n = 0
  for _, m in ipairs(marks(ns)) do
    if m.text:find("neocursor ·", 1, true) then n = n + 1 end
  end
  return n
end

-- a mark counts as diff body if it strikes a deletion or adds virt_lines
local function body_marks()
  local n = 0
  for _, m in ipairs(marks(ns)) do
    if m.details.hl_group == "NeocursorDelete" or m.details.virt_lines then n = n + 1 end
  end
  return n
end

-- ─── preview.diff ────────────────────────────────────────────────────────────
local old_lines = { "line1 = 1", "line2 = 2" }
local new_lines = { "line1 = 111", "line2 = 2" }

reseed()
preview.diff(bufnr, 0, old_lines, new_lines, "<Tab> accept")
local body_with_hint = body_marks()
check("hint shown when a label is passed", hint_marks(), 1)
check("diff body rendered alongside the hint", body_with_hint > 0, true)

reseed()
preview.diff(bufnr, 0, old_lines, new_lines, nil)
check("hint absent when the label is nil", hint_marks(), 0)
check("diff body identical without the hint", body_marks(), body_with_hint)

reseed()
preview.diff(bufnr, 0, old_lines, new_lines, "<Tab> jump")
local jump_label = ""
for _, m in ipairs(marks(ns)) do
  if m.text:find("neocursor ·", 1, true) then jump_label = m.text end
end
check("label text is the caller's, verbatim", jump_label, "  ⟪neocursor · <Tab> jump⟫")

-- ─── preview.prediction ──────────────────────────────────────────────────────
-- The pill lives in its own namespace so a suggestion clear never kills it.
local ns_pred = preview.prediction_namespace()
local function pred_marks()
  return #vim.api.nvim_buf_get_extmarks(bufnr, ns_pred, 0, -1, {})
end

reseed()
preview.prediction(bufnr, 1, "<Tab> → L2")
check("prediction pill painted", pred_marks(), 1)

preview.diff(bufnr, 0, old_lines, new_lines, "<Tab> accept")
check("pill survives a suggestion render (separate namespace)", pred_marks(), 1)
preview.clear(bufnr)
check("pill survives a suggestion clear", pred_marks(), 1)

preview.clear_prediction(bufnr)
check("prediction pill cleared on request", pred_marks(), 0)

-- ─── show_hints normalization ────────────────────────────────────────────────
-- Pure function, so assert it directly — no setup(), no sidecar spawn.
local normalize = require("neocursor")._normalize_hints

check("default (nil) shows both", normalize(nil), { edit = true, prediction = true })
check("true shows both", normalize(true), { edit = true, prediction = true })
check("false hides both", normalize(false), { edit = false, prediction = false })
check("granular: edit off leaves prediction on", normalize({ edit = false }),
  { edit = false, prediction = true })
check("granular: prediction off leaves edit on", normalize({ prediction = false }),
  { edit = true, prediction = false })
check("granular: both off", normalize({ edit = false, prediction = false }),
  { edit = false, prediction = false })
check("empty table = both on", normalize({}), { edit = true, prediction = true })

io.stdout:write(failed == 0 and "ALL PASS\n" or (failed .. " FAILURES\n"))
vim.cmd(failed == 0 and "qall!" or "cquit!")

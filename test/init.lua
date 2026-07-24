-- Minimal, isolated test config for neocursor.nvim.
-- Run from anywhere:  nvim -u <repo>/test/init.lua <repo>/test/demo.py
local root = vim.fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":h:h")

vim.opt.compatible = false
vim.opt.number = true
vim.opt.signcolumn = "yes"
vim.cmd("syntax on")

vim.opt.rtp:prepend(root)
require("neocursor").setup({ debounce = 250 })

-- tiny quality-of-life for testing
vim.opt.shiftwidth = 4
vim.opt.expandtab = true
print("neocursor test env — enter insert mode and start typing. <Tab> accepts, <C-]> dismisses.")

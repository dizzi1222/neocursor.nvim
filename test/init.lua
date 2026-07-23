-- Minimal, isolated test config for tabtab.nvim.
-- Run:  nvim -u /Users/beyond/tabtab.nvim/test/init.lua /Users/beyond/tabtab.nvim/test/demo.py
vim.opt.compatible = false
vim.opt.number = true
vim.opt.signcolumn = "yes"
vim.cmd("syntax on")

vim.opt.rtp:prepend("/Users/beyond/tabtab.nvim")
require("tabtab").setup({ debounce = 250 })

-- tiny quality-of-life for testing
vim.opt.shiftwidth = 4
vim.opt.expandtab = true
print("tabtab test env — enter insert mode and start typing. <Tab> accepts, <C-]> dismisses.")

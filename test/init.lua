-- Minimal, isolated test config for neocursor.nvim.
-- Run:  nvim -u /Users/beyond/neocursor.nvim/test/init.lua /Users/beyond/neocursor.nvim/test/demo.py
vim.opt.compatible = false
vim.opt.number = true
vim.opt.signcolumn = "yes"
vim.cmd("syntax on")

vim.opt.rtp:prepend("/Users/beyond/neocursor.nvim")
require("neocursor").setup({ debounce = 250 })

-- tiny quality-of-life for testing
vim.opt.shiftwidth = 4
vim.opt.expandtab = true
print("neocursor test env — enter insert mode and start typing. <Tab> accepts, <C-]> dismisses.")

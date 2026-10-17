" Filetype plugin for kif: '//' comments, 4-space indent, brace matching.
if exists('b:did_ftplugin')
  finish
endif
let b:did_ftplugin = 1

setlocal commentstring=//\ %s
setlocal comments=://
setlocal formatoptions-=t formatoptions+=croql
setlocal expandtab shiftwidth=4 softtabstop=4
setlocal iskeyword+=_
setlocal matchpairs+=<:>

let b:undo_ftplugin = 'setlocal commentstring< comments< formatoptions< expandtab< shiftwidth< softtabstop< iskeyword< matchpairs<'
